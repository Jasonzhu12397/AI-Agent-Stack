//! Operator probes and maintenance tasks.
//!
//! Each entry here replaces one Django management command: the same question,
//! the same audit trail, the same non-zero exit so a cron entry or a CI job
//! alarms without a human reading a table.

use std::collections::HashSet;

use serde_json::{json, Value};
use sqlx::{AnyPool, Row};

use crate::proxy::AppState;

const PROBLEM_EVENT: &str = "cluster.etcd_backup.problem";
const RECOVERED_EVENT: &str = "cluster.etcd_backup.recovered";
const SCHEDULER_SILENCE_HOURS: f64 = 2.0;
const DEFAULT_REPEAT_AFTER_HOURS: f64 = 6.0;
const ALERT_WEBHOOK_ENV: &str = "ETCD_BACKUP_ALERT_WEBHOOK";
const ALERT_TIMEOUT_ENV: &str = "ETCD_BACKUP_ALERT_TIMEOUT_SECONDS";
const DEFAULT_ALERT_TIMEOUT_SECONDS: f64 = 10.0;

pub fn is_ops_command(name: &str) -> bool {
    matches!(
        name,
        "check-backups"
            | "check-payments"
            | "queue-etcd-backups"
            | "expire-rollback-protection"
            | "install-builtin-connectors"
            | "sync-plugin-catalog"
            | "prune-qdrant-collections"
            | "reindex-knowledge"
    )
}

struct Flags {
    values: Vec<String>,
}

impl Flags {
    fn new(args: &[String]) -> Self {
        Self {
            values: args.to_vec(),
        }
    }

    fn has(&self, name: &str) -> bool {
        self.values.iter().any(|value| value == name)
    }

    fn value(&self, name: &str) -> Option<String> {
        let index = self.values.iter().position(|value| value == name)?;
        self.values.get(index + 1).cloned()
    }

    fn float(&self, name: &str, default: f64) -> f64 {
        match self.value(name).and_then(|raw| raw.parse::<f64>().ok()) {
            Some(value) if value >= 0.0 => value,
            _ => default,
        }
    }
}

/// Dispatch one operator command. Returns the process exit code.
pub async fn run(state: &AppState, name: &str, args: &[String]) -> Result<i32, String> {
    let flags = Flags::new(args);
    match name {
        "check-backups" => check_backups(state, &flags).await,
        "check-payments" => check_payments(state, &flags).await,
        "queue-etcd-backups" => queue_etcd_backups(state).await,
        "expire-rollback-protection" => expire_rollback_protection(state).await,
        "install-builtin-connectors" => install_builtin_connectors(state, &flags).await,
        "sync-plugin-catalog" => sync_plugin_catalog(state, &flags).await,
        "prune-qdrant-collections" => prune_qdrant_collections(state, &flags).await,
        "reindex-knowledge" => reindex_knowledge(state, &flags).await,
        other => Err(format!("unknown command: {other}")),
    }
}

fn pool_of(state: &AppState) -> Result<&AnyPool, String> {
    state
        .identity_pool
        .as_ref()
        .ok_or_else(|| "database unavailable".to_string())
}

fn database_url(state: &AppState) -> String {
    state.config.database_url.clone().unwrap_or_default()
}

fn postgres(state: &AppState) -> bool {
    database_url(state).starts_with("postgres")
}

fn created_column(state: &AppState, alias: &str) -> String {
    if postgres(state) {
        format!("CAST({alias}.created_at AS TEXT)")
    } else {
        format!("{alias}.created_at")
    }
}

/// The deployments that describe a cluster that exists right now: the newest
/// succeeded record per (tenant, project, node, cluster name).
async fn installed_deployments(state: &AppState) -> Result<Vec<Value>, String> {
    let pool = pool_of(state)?;
    let rows = sqlx::query(
        "SELECT CAST(d.id AS TEXT) AS id, d.cluster_name, \
         CAST(d.tenant_id AS TEXT) AS tenant_id, CAST(d.project_id AS TEXT) AS project_id, \
         CAST(d.target_node_id AS TEXT) AS target_node_id FROM core_clusterdeployment d \
         WHERE d.status = 'succeeded' AND d.target_node_id IS NOT NULL \
         ORDER BY d.created_at DESC, CAST(d.id AS TEXT) DESC",
    )
    .fetch_all(pool)
    .await
    .map_err(|error| error.to_string())?;
    let mut seen: HashSet<(String, String, String, String)> = HashSet::new();
    let mut chosen = Vec::new();
    for row in rows {
        let deployment_id: String = row.try_get("id").unwrap_or_default();
        let cluster_name: String = row.try_get("cluster_name").unwrap_or_default();
        let tenant_id: String = row.try_get("tenant_id").unwrap_or_default();
        let project_id: String = row.try_get("project_id").unwrap_or_default();
        let target_node_id: String = row.try_get("target_node_id").unwrap_or_default();
        let key = (
            tenant_id.clone(),
            project_id.clone(),
            target_node_id,
            cluster_name.clone(),
        );
        if !seen.insert(key) {
            continue;
        }
        chosen.push(json!({
            "deployment_id": deployment_id,
            "cluster_name": cluster_name,
            "tenant_id": tenant_id,
            "project_id": project_id,
        }));
    }
    Ok(chosen)
}

/// The last thing the watchdog said about a cluster: event type, code, when.
async fn last_report(
    state: &AppState,
    tenant_id: &str,
    deployment_id: &str,
) -> Result<(String, String, Option<chrono::NaiveDateTime>), String> {
    let pool = pool_of(state)?;
    let created = created_column(state, "event");
    let metadata = if postgres(state) {
        "CAST(event.metadata AS TEXT)"
    } else {
        "event.metadata"
    };
    let sql = format!(
        "SELECT event.event_type, {metadata} AS metadata_text, {created} AS created FROM core_auditevent event \
         WHERE CAST(event.tenant_id AS TEXT) = ? AND event.event_type IN (?, ?) \
         AND event.resource_type = 'cluster_deployment' AND CAST(event.resource_id AS TEXT) = ? \
         ORDER BY event.created_at DESC, CAST(event.id AS TEXT) DESC LIMIT 1"
    );
    let row = sqlx::query(&sql)
        .bind(tenant_id)
        .bind(PROBLEM_EVENT)
        .bind(RECOVERED_EVENT)
        .bind(deployment_id)
        .fetch_optional(pool)
        .await
        .map_err(|error| error.to_string())?;
    let Some(row) = row else {
        return Ok((String::new(), String::new(), None));
    };
    let kind: String = row.try_get("event_type").unwrap_or_default();
    let text: Option<String> = row.try_get("metadata_text").ok().flatten();
    let parsed: Value = text
        .as_deref()
        .and_then(|value| serde_json::from_str(value).ok())
        .unwrap_or_else(|| json!({}));
    let code = if kind == PROBLEM_EVENT {
        parsed
            .get("code")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string()
    } else {
        String::new()
    };
    let at = row
        .try_get::<Option<String>, _>("created")
        .ok()
        .flatten()
        .as_deref()
        .and_then(crate::backup_queue::parse_timestamp);
    Ok((kind, code, at))
}

async fn record(state: &AppState, event: crate::audit::AuditEvent<'_>) -> Result<(), String> {
    let pool = pool_of(state)?;
    let database_url = database_url(state);
    crate::audit::record_event(pool, &database_url, event).await
}

async fn check_backups(state: &AppState, flags: &Flags) -> Result<i32, String> {
    let dry_run = flags.has("--dry-run");
    let no_audit = flags.has("--no-audit");
    let no_alert = flags.has("--no-alert");
    let repeat_after = flags.float("--repeat-after-hours", DEFAULT_REPEAT_AFTER_HOURS);
    let now = chrono::Utc::now();
    let stamp = now.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string();
    let mut clusters = Vec::new();
    let mut problems = Vec::new();
    for entry in installed_deployments(state).await? {
        let deployment_id = entry
            .get("deployment_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let cluster_name = entry
            .get("cluster_name")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let tenant_id = entry
            .get("tenant_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let project_id = entry
            .get("project_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        let pool = pool_of(state)?;
        let state_value = match crate::etcd_api::cluster_state(state, pool, &deployment_id).await {
            Ok(value) => value,
            Err(message) => {
                clusters.push(json!({
                    "deployment_id": deployment_id,
                    "cluster_name": cluster_name,
                    "ok": false,
                    "code": message.split(':').next().unwrap_or("ETCD_BACKUP_INVALID"),
                }));
                continue;
            }
        };
        let policy = state_value.get("policy").cloned().unwrap_or(json!({}));
        let enabled = state_value
            .get("enabled")
            .and_then(Value::as_bool)
            .unwrap_or(true);
        let mut code = String::new();
        if policy.get("valid").and_then(Value::as_bool) == Some(false) {
            code = policy
                .get("error")
                .and_then(|error| error.get("code"))
                .and_then(Value::as_str)
                .unwrap_or("ETCD_BACKUP_INVALID")
                .to_string();
        } else if !enabled {
            code = "ETCD_BACKUP_DISABLED".into();
        } else {
            for finding in state_value
                .get("findings")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default()
            {
                if finding.get("ok").and_then(Value::as_bool) == Some(false) {
                    if let Some(code_ref) = finding.get("code").and_then(Value::as_str) {
                        if !code_ref.is_empty() {
                            code = code_ref.to_string();
                            break;
                        }
                    }
                }
            }
            let scheduler_age = state_value
                .get("scheduler_age_hours")
                .and_then(Value::as_f64);
            if (code == "ETCD_BACKUP_STALE" || code == "ETCD_BACKUP_NOT_RUN_YET")
                && scheduler_age.map(|age| age > SCHEDULER_SILENCE_HOURS) != Some(false)
            {
                // The backups are old *and* nothing is queuing new ones.
                code = "ETCD_BACKUP_SCHEDULER_SILENT".into();
            }
        }
        let newest = state_value
            .get("newest")
            .and_then(|value| value.get("name"))
            .cloned()
            .unwrap_or(Value::Null);
        let entry = json!({
            "deployment_id": deployment_id,
            "cluster_name": cluster_name,
            "enabled": enabled,
            "code": code,
            "ok": code.is_empty(),
            "newest": newest,
            "newest_age_hours": state_value.get("newest_age_hours").cloned().unwrap_or(Value::Null),
            "stale": state_value.get("stale").cloned().unwrap_or(Value::Null),
            "restore_point": state_value
                .get("restore_point")
                .and_then(|value| value.get("name"))
                .cloned()
                .unwrap_or(Value::Null),
            "scheduler_age_hours": state_value.get("scheduler_age_hours").cloned().unwrap_or(Value::Null),
            "last_run_status": state_value.get("last_run_status").cloned().unwrap_or(Value::Null),
        });
        if code.is_empty() || code == "ETCD_BACKUP_DISABLED" {
            // One event closes a cluster that used to be a problem, so the
            // chain records the recovery instead of leaving the last failure
            // standing forever.
            let (kind, previous, _) = last_report(state, &tenant_id, &deployment_id).await?;
            if code.is_empty() && kind == PROBLEM_EVENT && !no_audit && !dry_run {
                record(
                    state,
                    crate::audit::AuditEvent {
                        tenant_id: Some(tenant_id.as_str()),
                        project_id: Some(project_id.as_str()),
                        event_type: RECOVERED_EVENT,
                        resource_type: "cluster_deployment",
                        resource_id: deployment_id.as_str(),
                        outcome: "success",
                        actor_id: None,
                        request_id: "",
                        remote_addr: None,
                        user_agent: "",
                        metadata: json!({
                            "cluster_name": entry["cluster_name"],
                            "previous_code": previous,
                            "newest": entry["newest"],
                            "checked_at": stamp,
                        }),
                    },
                )
                .await?;
            }
            clusters.push(entry);
            continue;
        }
        let mut reported = entry.clone();
        if no_audit || dry_run {
            clusters.push(entry);
            problems.push(reported);
            continue;
        }
        let (kind, previous, previous_at) = last_report(state, &tenant_id, &deployment_id).await?;
        if kind == PROBLEM_EVENT
            && previous == code
            && repeat_after > 0.0
            && previous_at
                .map(|at| {
                    now.naive_utc() - at < chrono::Duration::minutes((repeat_after * 60.0) as i64)
                })
                .unwrap_or(false)
        {
            // A problem that persists is written once, not once per probe.
            reported["audited"] = json!(false);
        } else {
            reported["audited"] = json!(true);
            record(
                state,
                crate::audit::AuditEvent {
                    tenant_id: Some(tenant_id.as_str()),
                    project_id: Some(project_id.as_str()),
                    event_type: PROBLEM_EVENT,
                    resource_type: "cluster_deployment",
                    resource_id: deployment_id.as_str(),
                    outcome: "failure",
                    actor_id: None,
                    request_id: "",
                    remote_addr: None,
                    user_agent: "",
                    metadata: json!({
                        "code": code,
                        "cluster_name": entry["cluster_name"],
                        "newest": entry["newest"],
                        "newest_age_hours": entry["newest_age_hours"],
                        "checked_at": stamp,
                    }),
                },
            )
            .await?;
        }
        clusters.push(entry);
        problems.push(reported);
    }
    let mut report = json!({
        "checked_at": stamp,
        "dry_run": dry_run,
        "audited": !no_audit,
        "clusters": clusters,
        "problems": problems,
    });
    let alert = if no_alert || dry_run {
        json!({"configured": false, "delivered": false, "reason": "skipped"})
    } else {
        deliver_alert(&report).await
    };
    if let Some(object) = report.as_object_mut() {
        object.insert("alert".into(), alert);
    }
    println!(
        "{}",
        serde_json::to_string(&report).unwrap_or_else(|_| "{}".into())
    );
    if !report["problems"]
        .as_array()
        .map(|items| items.is_empty())
        .unwrap_or(true)
    {
        return Ok(1);
    }
    Ok(0)
}

fn alert_timeout() -> f64 {
    std::env::var(ALERT_TIMEOUT_ENV)
        .ok()
        .and_then(|raw| raw.trim().parse::<f64>().ok())
        .filter(|value| *value > 0.0)
        .unwrap_or(DEFAULT_ALERT_TIMEOUT_SECONDS)
}

fn alert_text(report: &Value) -> String {
    let problems = report
        .get("problems")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let checked = report
        .get("checked_at")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let listed: Vec<String> = problems
        .iter()
        .map(|item| {
            let cluster = item
                .get("cluster_name")
                .and_then(Value::as_str)
                .or_else(|| item.get("deployment_id").and_then(Value::as_str))
                .unwrap_or_default();
            let code = item.get("code").and_then(Value::as_str).unwrap_or_default();
            let detail = match item.get("newest_age_hours").and_then(Value::as_f64) {
                Some(age) => format!(" (newest backup {age:.1}h old)"),
                None => " (no backup ever reported)".into(),
            };
            format!("{cluster}: {code}{detail}")
        })
        .collect();
    format!(
        "{} cluster(s) without a usable recent etcd backup at {checked}: {}",
        problems.len(),
        listed.join("; ")
    )
}

/// One POST to the operator's webhook. Never echoes the target: its path may
/// carry a token.
async fn deliver_alert(report: &Value) -> Value {
    let target = std::env::var(ALERT_WEBHOOK_ENV)
        .unwrap_or_default()
        .trim()
        .to_string();
    if target.is_empty() {
        return json!({"configured": false, "delivered": false, "reason": "no_webhook"});
    }
    let problems = report
        .get("problems")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if problems.is_empty() {
        // A watchdog that pages on "all good" trains its reader to ignore it.
        return json!({"configured": true, "delivered": false, "reason": "nothing_to_report"});
    }
    let body = json!({
        "kind": "etcd_backup_problem",
        "checked_at": report.get("checked_at").cloned().unwrap_or(Value::Null),
        "problem_count": problems.len(),
        "text": alert_text(report),
        "problems": problems
            .iter()
            .map(|item| json!({
                "deployment_id": item.get("deployment_id").cloned().unwrap_or(Value::Null),
                "cluster_name": item.get("cluster_name").cloned().unwrap_or(Value::Null),
                "code": item.get("code").cloned().unwrap_or(Value::Null),
                "newest": item.get("newest").cloned().unwrap_or(Value::Null),
                "newest_age_hours": item.get("newest_age_hours").cloned().unwrap_or(Value::Null),
                "restore_point": item.get("restore_point").cloned().unwrap_or(Value::Null),
                "scheduler_age_hours": item.get("scheduler_age_hours").cloned().unwrap_or(Value::Null),
                "last_run_status": item.get("last_run_status").cloned().unwrap_or(Value::Null),
            }))
            .collect::<Vec<Value>>(),
    });
    let client = match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs_f64(alert_timeout()))
        .build()
    {
        Ok(client) => client,
        Err(error) => {
            return json!({"configured": true, "delivered": false, "reason": "send_failed", "detail": error.to_string()})
        }
    };
    match client
        .post(&target)
        .header("Content-Type", "application/json; charset=utf-8")
        .body(serde_json::to_vec(&body).unwrap_or_default())
        .send()
        .await
    {
        Ok(response) => {
            let status = response.status().as_u16();
            if (200..300).contains(&status) {
                json!({"configured": true, "delivered": true, "status": status})
            } else {
                json!({"configured": true, "delivered": false, "reason": "http_status", "status": status})
            }
        }
        Err(error) => json!({
            "configured": true,
            "delivered": false,
            "reason": "send_failed",
            "detail": error.to_string(),
        }),
    }
}

async fn check_payments(state: &AppState, flags: &Flags) -> Result<i32, String> {
    let allow_none = flags.has("--allow-none");
    let channels = crate::billing_api::payment_channel_diagnostics(state).await;
    let mut lines = Vec::new();
    for entry in &channels {
        let channel = entry
            .get("channel")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let configured = entry
            .get("configured")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let missing = entry
            .get("missing")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default()
            .iter()
            .filter_map(|value| value.as_str().map(str::to_string))
            .collect::<Vec<String>>();
        if configured {
            lines.push(format!("{channel}: configured"));
        } else {
            lines.push(format!(
                "{channel}: not configured, missing {}",
                missing.join(", ")
            ));
        }
    }
    for line in &lines {
        println!("{line}");
    }
    let usable: Vec<String> = channels
        .iter()
        .filter(|entry| entry.get("configured").and_then(Value::as_bool) == Some(true))
        .filter_map(|entry| {
            entry
                .get("channel")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .collect();
    println!(
        "usable channels: {}",
        if usable.is_empty() {
            "none".to_string()
        } else {
            usable.join(", ")
        }
    );
    let prefixes = crate::billing_api::payment_env_prefixes();
    if !prefixes.is_empty() {
        println!("environment prefixes: {}", prefixes.join(", "));
    }
    if usable.is_empty() && !allow_none {
        return Err(channels
            .iter()
            .map(|entry| {
                format!(
                    "{} missing {}",
                    entry
                        .get("channel")
                        .and_then(Value::as_str)
                        .unwrap_or_default(),
                    entry
                        .get("missing")
                        .and_then(Value::as_array)
                        .map(|items| items
                            .iter()
                            .filter_map(|value| value.as_str())
                            .collect::<Vec<&str>>()
                            .join(", "))
                        .unwrap_or_default()
                )
            })
            .collect::<Vec<String>>()
            .join("; "));
    }
    Ok(0)
}

async fn queue_etcd_backups(state: &AppState) -> Result<i32, String> {
    let pool = pool_of(state)?;
    let now = chrono::Utc::now().format("%Y-%m-%d %H:%M:%S").to_string();
    let queued =
        crate::backup_queue::dispatch_hourly_backups(pool, &database_url(state), &now).await?;
    println!("queued backups={queued}");
    Ok(0)
}

async fn expire_rollback_protection(state: &AppState) -> Result<i32, String> {
    let pool = pool_of(state)?;
    let now = chrono::Utc::now().format("%Y-%m-%d %H:%M:%S").to_string();
    let released =
        crate::backup_queue::release_expired_rollback_protection(pool, &database_url(state), &now)
            .await?;
    println!("released rollback protections={released}");
    Ok(0)
}

async fn install_builtin_connectors(state: &AppState, flags: &Flags) -> Result<i32, String> {
    let installed =
        crate::plugin_catalog::install_builtin_connectors(state, flags.value("--tenant")).await?;
    println!("builtin connectors installed={installed}");
    Ok(0)
}

async fn sync_plugin_catalog(state: &AppState, flags: &Flags) -> Result<i32, String> {
    let (created, enabled) = crate::plugin_catalog::sync_tenant_catalog(
        state,
        flags.value("--catalog"),
        flags.value("--tenant"),
    )
    .await?;
    println!("catalog entries={created} enabled={enabled}");
    Ok(0)
}

async fn prune_qdrant_collections(state: &AppState, flags: &Flags) -> Result<i32, String> {
    let report = crate::knowledge_ops::prune_qdrant_collections(
        state,
        flags.has("--apply"),
        flags.value("--prefix"),
    )
    .await?;
    for name in report
        .get("candidates")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
    {
        println!("candidate collection={}", name.as_str().unwrap_or_default());
    }
    for failure in report
        .get("failures")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
    {
        eprintln!(
            "failed collection={} error={}",
            failure
                .get("collection")
                .and_then(Value::as_str)
                .unwrap_or_default(),
            failure
                .get("error")
                .and_then(Value::as_str)
                .unwrap_or_default()
        );
    }
    println!(
        "total={} active={} candidates={} deleted={} mode={}",
        report
            .get("total")
            .and_then(Value::as_u64)
            .unwrap_or_default(),
        report
            .get("active")
            .and_then(Value::as_u64)
            .unwrap_or_default(),
        report
            .get("candidates")
            .and_then(Value::as_array)
            .map(|items| items.len())
            .unwrap_or_default(),
        report
            .get("deleted")
            .and_then(Value::as_u64)
            .unwrap_or_default(),
        if flags.has("--apply") {
            "apply"
        } else {
            "dry-run"
        },
    );
    if report
        .get("failures")
        .and_then(Value::as_array)
        .map(|items| !items.is_empty())
        .unwrap_or(false)
    {
        return Ok(1);
    }
    Ok(0)
}

async fn reindex_knowledge(state: &AppState, flags: &Flags) -> Result<i32, String> {
    let documents: Vec<String> = flags
        .values
        .iter()
        .enumerate()
        .filter(|(_, value)| *value == "--document")
        .filter_map(|(index, _)| flags.values.get(index + 1).cloned())
        .collect();
    let limit = flags
        .value("--limit")
        .and_then(|raw| raw.parse::<i64>().ok())
        .unwrap_or(0);
    let summary = crate::knowledge_ops::reindex_knowledge(
        state,
        documents,
        limit,
        flags.has("--force"),
        flags.has("--dry-run"),
    )
    .await?;
    println!(
        "{}",
        serde_json::to_string(&summary).unwrap_or_else(|_| "{}".into())
    );
    if summary
        .get("failed")
        .and_then(Value::as_array)
        .map(|items| !items.is_empty())
        .unwrap_or(false)
    {
        return Ok(1);
    }
    Ok(0)
}
