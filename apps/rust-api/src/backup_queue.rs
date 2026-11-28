//! Queue the etcd snapshot that follows a cluster mutation.
//!
//! The mutation command's result and its follow-up snapshot share one request
//! flow: after a real change every older snapshot describes a past state, so
//! one backup is queued immediately instead of waiting for the hourly slot.
//!
//! The payload is derived from the deployment's own records — the directory set
//! and the protect set included — so the hourly scheduler, the operator-run
//! backup and the post-change snapshot cannot drift apart.

use chrono::NaiveDateTime;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use sqlx::{AnyPool, Row};

pub const ACTION: &str = "cluster.kubernetes.backup.run";
pub const STATUS_ACTION: &str = "cluster.kubernetes.backup.status";
const ROLLBACK_ACTION: &str = "cluster.kubernetes.version.rollback";
const UPGRADE_ACTION: &str = "cluster.kubernetes.version.upgrade";
const POLICY_AUDIT_EVENT: &str = "cluster.etcd_backup.policy.updated";
const SCHEDULER_EVENT: &str = "cluster.etcd_backup.scheduler.run";
const SCHEDULER_RESOURCE: &str = "etcd_backup_scheduler";
const DEPLOYMENT_RESOURCE: &str = "cluster_deployment";

const DEFAULT_DIRECTORY: &str = "/var/lib/etcd/backups";
const DEFAULT_KEEP_COUNT: i64 = 2;
const DEFAULT_KEEP_MAX_AGE_HOURS: i64 = 8760;
const DEFAULT_PRE_UPGRADE_KEEP_MAX_AGE_HOURS: i64 = 168;
const DEFAULT_MAX_AGE_HOURS: i64 = 26;
const DEFAULT_MIN_FREE_BYTES: i64 = 512 * 1024 * 1024;

/// Retention bounds the backup contract refuses outside of.
const KEEP_COUNT_BOUNDS: (i64, i64) = (2, 168);
const KEEP_MAX_AGE_BOUNDS: (i64, i64) = (1, 8760);
const PRE_UPGRADE_AGE_BOUNDS: (i64, i64) = (1, 8760);
const MAX_AGE_BOUNDS: (i64, i64) = (1, 8760);
const MIN_FREE_BOUNDS: (i64, i64) = (0, 1024 * 1024 * 1024 * 1024);

/// Failure codes that prove the snapshot itself was unusable before anything
/// ran, so the file may be released even though the rollback failed.
const PROTECT_RELEASE_ERROR_CODES: [&str; 2] = [
    "ROLLBACK_SNAPSHOT_PATH_INVALID",
    "ROLLBACK_CLUSTER_NAME_MISMATCH",
];

fn hour_bucket(now: &str) -> String {
    // `now` is `YYYY-MM-DD HH:MM:SS` in UTC.
    let digits: String = now.chars().filter(char::is_ascii_digit).collect();
    if digits.len() < 10 {
        return "unknown".into();
    }
    format!("{}T{}", &digits[..8], &digits[8..10])
}

fn payload_hash(command_type: &str, payload: &Value) -> String {
    let canonical = json!({"command_type": command_type, "payload": payload});
    hex::encode(Sha256::digest(canonical.to_string().as_bytes()))
}

fn spec_kubernetes(spec: &Value) -> Map<String, Value> {
    spec.get("kubernetes")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

fn policy_int(
    kubernetes: &Map<String, Value>,
    key: &str,
    bounds: (i64, i64),
    default: i64,
    code: &'static str,
) -> Result<i64, String> {
    let Some(value) = kubernetes.get(key) else {
        return Ok(default);
    };
    if value.is_null() || value.as_str().is_some_and(|text| text.trim().is_empty()) {
        return Ok(default);
    }
    let Some(number) = value.as_i64() else {
        return Err(code.into());
    };
    if !(bounds.0..=bounds.1).contains(&number) {
        return Err(code.into());
    }
    Ok(number)
}

/// Normalize a backup directory, or `None` when it is outside the only place a
/// snapshot may live: an absolute path under `/var/lib/etcd`, no traversal.
fn normalize_directory(raw: &str) -> Option<String> {
    let candidate = raw.trim().trim_end_matches('/');
    if candidate.is_empty()
        || !candidate.starts_with("/var/lib/etcd")
        || candidate.contains("..")
        || candidate.contains("//")
    {
        return None;
    }
    if !candidate
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '/' | '-'))
    {
        return None;
    }
    Some(candidate.to_string())
}

/// Deduplicate and validate every location this cluster's snapshots may occupy.
fn candidate_directories(sources: Vec<String>) -> Result<Vec<String>, String> {
    let mut valid = Vec::new();
    for raw in sources {
        let Some(normalized) = normalize_directory(&raw) else {
            return Err("ETCD_BACKUP_DIRECTORY_INVALID".into());
        };
        if !valid.contains(&normalized) {
            valid.push(normalized);
        }
    }
    Ok(valid)
}

fn snapshot_stamp_ok(stamp: &str) -> bool {
    stamp.len() == 16
        && stamp.as_bytes()[8] == b'T'
        && stamp.as_bytes()[15] == b'Z'
        && NaiveDateTime::parse_from_str(stamp, "%Y%m%dT%H%M%SZ").is_ok()
}

fn is_run_id(run: &str) -> bool {
    run.len() == 8
        && run
            .chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
}

/// `<cluster>-<stamp>.db` or `<cluster>-<stamp>-<run>.db`.
fn is_backup_name(name: &str, cluster: &str) -> bool {
    let Some(stem) = name.strip_suffix(".db") else {
        return false;
    };
    let Some(rest) = stem.strip_prefix(&format!("{cluster}-")) else {
        return false;
    };
    if snapshot_stamp_ok(rest) {
        return true;
    }
    match rest.rsplit_once('-') {
        Some((stamp, run)) => snapshot_stamp_ok(stamp) && is_run_id(run),
        None => false,
    }
}

/// `snapshot-pre-<tail>-<stamp>.db`. Legacy files carry only the target
/// version, and are still this cluster's files because one directory belongs
/// to one Target node.
fn is_pre_upgrade_name(name: &str) -> bool {
    let Some(stem) = name.strip_suffix(".db") else {
        return false;
    };
    let Some(rest) = stem.strip_prefix("snapshot-pre-") else {
        return false;
    };
    match rest.rsplit_once('-') {
        Some((tail, stamp)) => !tail.is_empty() && snapshot_stamp_ok(stamp),
        None => false,
    }
}

fn protect_names(value: Option<&Value>, cluster: &str) -> Result<Vec<String>, String> {
    let Some(value) = value else {
        return Ok(Vec::new());
    };
    let Some(items) = value.as_array() else {
        return Err("ETCD_BACKUP_PROTECT_INVALID".into());
    };
    let mut names = Vec::new();
    for item in items {
        let name = item.as_str().unwrap_or_default().trim();
        if name.is_empty() || name.contains('/') || name.contains("..") {
            return Err("ETCD_BACKUP_PROTECT_INVALID".into());
        }
        if !is_backup_name(name, cluster) && !is_pre_upgrade_name(name) {
            return Err("ETCD_BACKUP_PROTECT_INVALID".into());
        }
        names.push(name.to_string());
    }
    names.sort();
    names.dedup();
    Ok(names)
}

/// Build the validated backup payload from a deployment spec. `Err` carries the
/// stable reason the deployment cannot be snapshotted.
pub fn backup_payload(spec: &Value, cluster_name: &str) -> Result<Value, String> {
    let kubernetes = spec_kubernetes(spec);
    match kubernetes.get("etcd_backup") {
        None => {}
        Some(Value::Bool(false)) => return Err("ETCD_BACKUP_DISABLED".into()),
        Some(Value::Bool(true)) => {}
        Some(_) => return Err("ETCD_BACKUP_ENABLED_INVALID".into()),
    }
    let cluster = cluster_name.trim();
    if cluster.is_empty()
        || cluster.len() > 63
        || !cluster
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-' || c == '.')
    {
        return Err("ETCD_BACKUP_CLUSTER_NAME_INVALID".into());
    }
    let directory = normalize_directory(
        kubernetes
            .get("etcd_backup_directory")
            .and_then(Value::as_str)
            .unwrap_or(DEFAULT_DIRECTORY),
    )
    .ok_or_else(|| "ETCD_BACKUP_DIRECTORY_INVALID".to_string())?;
    let directories = candidate_directories(vec![directory.clone()])?;
    let keep_count = policy_int(
        &kubernetes,
        "etcd_backup_keep_count",
        KEEP_COUNT_BOUNDS,
        DEFAULT_KEEP_COUNT,
        "ETCD_BACKUP_RETENTION_INVALID",
    )?;
    let keep_max_age_hours = policy_int(
        &kubernetes,
        "etcd_backup_keep_max_age_hours",
        KEEP_MAX_AGE_BOUNDS,
        DEFAULT_KEEP_MAX_AGE_HOURS,
        "ETCD_BACKUP_RETENTION_INVALID",
    )?;
    let pre_upgrade_keep_max_age_hours = policy_int(
        &kubernetes,
        "etcd_backup_pre_upgrade_keep_max_age_hours",
        PRE_UPGRADE_AGE_BOUNDS,
        DEFAULT_PRE_UPGRADE_KEEP_MAX_AGE_HOURS,
        "ETCD_BACKUP_RETENTION_INVALID",
    )?;
    let max_age_hours = policy_int(
        &kubernetes,
        "etcd_backup_max_age_hours",
        MAX_AGE_BOUNDS,
        DEFAULT_MAX_AGE_HOURS,
        "ETCD_BACKUP_STALENESS_INVALID",
    )?;
    let min_free_bytes = policy_int(
        &kubernetes,
        "etcd_backup_min_free_bytes",
        MIN_FREE_BOUNDS,
        DEFAULT_MIN_FREE_BYTES,
        "ETCD_BACKUP_DISK_POLICY_INVALID",
    )?;
    let protect = protect_names(kubernetes.get("protect"), cluster)?;
    let mut payload = Map::new();
    payload.insert("cluster_name".into(), json!(cluster));
    payload.insert("directory".into(), json!(directory));
    payload.insert("directories".into(), json!(directories));
    payload.insert("keep_count".into(), json!(keep_count));
    payload.insert("keep_max_age_hours".into(), json!(keep_max_age_hours));
    payload.insert(
        "pre_upgrade_keep_max_age_hours".into(),
        json!(pre_upgrade_keep_max_age_hours),
    );
    payload.insert("max_age_hours".into(), json!(max_age_hours));
    payload.insert("min_free_bytes".into(), json!(min_free_bytes));
    payload.insert("execution_mode".into(), json!("execute"));
    payload.insert("confirm_cluster_name".into(), json!(cluster));
    if !protect.is_empty() {
        payload.insert("protect".into(), json!(protect));
    }
    Ok(Value::Object(payload))
}

async fn command_values(
    pool: &AnyPool,
    deployment_id: &str,
    command_type: &str,
    column: &str,
) -> Result<Vec<Value>, String> {
    let sql = format!(
        "SELECT CAST({column} AS TEXT) AS value FROM core_agentcommand \
         WHERE CAST(deployment_id AS TEXT) = ? AND command_type = ?"
    );
    let rows = sqlx::query(&sql)
        .bind(deployment_id)
        .bind(command_type)
        .fetch_all(pool)
        .await
        .map_err(|error| error.to_string())?;
    Ok(rows
        .into_iter()
        .filter_map(|row| {
            row.try_get::<Option<String>, _>("value")
                .ok()
                .flatten()
                .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        })
        .collect())
}

/// Directory of the snapshot an upgrade reported — the only record of where a
/// rollback would look for it.
fn reported_snapshot_directory(result: &Value) -> Option<String> {
    let path = result
        .get("etcd_snapshot")
        .and_then(|snapshot| snapshot.get("path"))
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|path| !path.is_empty())?;
    path.rsplit_once('/')
        .map(|(directory, _)| directory.to_string())
}

/// Locations this cluster's snapshots may occupy, derived from its own records:
/// the declared directory, directories past backup/status commands reported, the
/// parent of an upgrade-reported snapshot, and directories a policy change left
/// in the audit trail.
async fn known_directories(
    pool: &AnyPool,
    deployment_id: &str,
    spec: &Value,
) -> Result<Vec<String>, String> {
    let kubernetes = spec_kubernetes(spec);
    let mut sources = Vec::new();
    if let Some(declared) = kubernetes
        .get("etcd_backup_directory")
        .and_then(Value::as_str)
    {
        if !declared.trim().is_empty() {
            sources.push(declared.to_string());
        }
    }
    for command_type in [ACTION, STATUS_ACTION] {
        for payload in command_values(pool, deployment_id, command_type, "payload").await? {
            if let Some(directory) = payload.get("directory").and_then(Value::as_str) {
                if !directory.trim().is_empty() {
                    sources.push(directory.to_string());
                }
            }
        }
    }
    for result in command_values(pool, deployment_id, UPGRADE_ACTION, "result").await? {
        if let Some(directory) = reported_snapshot_directory(&result) {
            sources.push(directory);
        }
    }
    let rows = sqlx::query(
        "SELECT CAST(metadata AS TEXT) AS value FROM core_auditevent \
         WHERE event_type = ? AND resource_type = ? AND CAST(resource_id AS TEXT) = ?",
    )
    .bind(POLICY_AUDIT_EVENT)
    .bind(DEPLOYMENT_RESOURCE)
    .bind(deployment_id)
    .fetch_all(pool)
    .await
    .map_err(|error| error.to_string())?;
    for row in rows {
        let Some(metadata) = row
            .try_get::<Option<String>, _>("value")
            .ok()
            .flatten()
            .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        else {
            continue;
        };
        if let Some(directory) = metadata
            .get("policy")
            .and_then(|policy| policy.get("etcd_backup_directory"))
            .and_then(Value::as_str)
        {
            if !directory.trim().is_empty() {
                sources.push(directory.to_string());
            }
        }
    }
    candidate_directories(sources)
}

fn failure_code(command: &Value) -> String {
    let code = command
        .get("error")
        .and_then(|error| error.get("code"))
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim();
    if !code.is_empty() {
        return code.to_string();
    }
    command
        .get("result")
        .and_then(|result| result.get("failure_code"))
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .to_string()
}

/// Whether this rollback still holds a snapshot retention must keep.
///
/// `succeeded`, `cancelled` and `expired` release: the snapshot has been used,
/// the operator aborted, or the lease horizon lapsed. A `failed` rollback
/// releases only when the restore actually consumed the snapshot or the failure
/// proves the file itself was unusable; an unknown outcome keeps protecting so a
/// manual reconciliation can still restore from it.
fn rollback_should_protect(command: &Value) -> bool {
    let status = command
        .get("status")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if matches!(status, "succeeded" | "cancelled" | "expired") {
        return false;
    }
    if status == "failed" {
        if command
            .get("result")
            .and_then(|result| result.get("rolled_back"))
            .and_then(Value::as_bool)
            == Some(true)
        {
            return false;
        }
        return !PROTECT_RELEASE_ERROR_CODES.contains(&failure_code(command).as_str());
    }
    true
}

async fn command_lifecycle(
    pool: &AnyPool,
    deployment_id: &str,
    command_type: &str,
) -> Result<Vec<Value>, String> {
    let rows = sqlx::query(
        "SELECT status, CAST(payload AS TEXT) AS payload_text, CAST(result AS TEXT) AS result_text \
         FROM core_agentcommand WHERE CAST(deployment_id AS TEXT) = ? AND command_type = ?",
    )
    .bind(deployment_id)
    .bind(command_type)
    .fetch_all(pool)
    .await
    .map_err(|error| error.to_string())?;
    Ok(rows
        .into_iter()
        .map(|row| {
            let payload: Value = row
                .try_get::<Option<String>, _>("payload_text")
                .ok()
                .flatten()
                .and_then(|text| serde_json::from_str(&text).ok())
                .unwrap_or_else(|| json!({}));
            let result: Value = row
                .try_get::<Option<String>, _>("result_text")
                .ok()
                .flatten()
                .and_then(|text| serde_json::from_str(&text).ok())
                .unwrap_or_else(|| json!({}));
            json!({
                "status": row.try_get::<String, _>("status").unwrap_or_default(),
                "payload": payload,
                "result": result,
            })
        })
        .collect())
}

/// Bare snapshot names the retention pass must not delete: snapshots a rollback
/// can still restore from, plus the verified pre-upgrade snapshot a settled
/// upgrade reported.
pub async fn protected_names(
    pool: &AnyPool,
    deployment_id: &str,
    cluster_name: &str,
) -> Result<Vec<String>, String> {
    let mut names: Vec<String> = Vec::new();
    for command in command_lifecycle(pool, deployment_id, ROLLBACK_ACTION).await? {
        if !rollback_should_protect(&command) {
            continue;
        }
        let path = command
            .get("payload")
            .and_then(|payload| payload.get("snapshot_path"))
            .and_then(Value::as_str)
            .unwrap_or_default()
            .trim();
        if let Some((_, name)) = path.rsplit_once('/') {
            if is_backup_name(name, cluster_name) && !names.iter().any(|known| known == name) {
                names.push(name.to_string());
            }
        }
    }
    let upgrade = sqlx::query(
        "SELECT CAST(result AS TEXT) AS value FROM core_agentcommand \
         WHERE CAST(deployment_id AS TEXT) = ? AND command_type = ? \
         AND status IN ('succeeded', 'failed') \
         ORDER BY created_at DESC, CAST(id AS TEXT) DESC LIMIT 1",
    )
    .bind(deployment_id)
    .bind(UPGRADE_ACTION)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())?;
    if let Some(row) = upgrade {
        let result = row
            .try_get::<Option<String>, _>("value")
            .ok()
            .flatten()
            .and_then(|text| serde_json::from_str::<Value>(&text).ok())
            .unwrap_or_else(|| json!({}));
        let snapshot = result.get("etcd_snapshot");
        if snapshot
            .and_then(|snapshot| snapshot.get("verified"))
            .and_then(Value::as_bool)
            == Some(true)
        {
            let path = snapshot
                .and_then(|snapshot| snapshot.get("path"))
                .and_then(Value::as_str)
                .unwrap_or_default()
                .trim();
            if let Some((_, name)) = path.rsplit_once('/') {
                if is_backup_name(name, cluster_name) && !names.iter().any(|known| known == name) {
                    names.push(name.to_string());
                }
            }
        }
    }
    names.sort();
    Ok(names)
}

/// Full payload for one deployment: policy, every known directory, and the
/// protect set derived from live command state.
pub async fn deployment_backup_payload(
    pool: &AnyPool,
    deployment_id: &str,
    spec: &Value,
    cluster_name: &str,
) -> Result<Value, String> {
    let mut payload = backup_payload(spec, cluster_name)?;
    let mut directories = known_directories(pool, deployment_id, spec).await?;
    // The effective directory is always a candidate, declared in the spec or
    // defaulted here, so the very first backup of a cluster still names it.
    if let Some(directory) = payload.get("directory").and_then(Value::as_str) {
        let directory = directory.to_string();
        if !directories.contains(&directory) {
            directories.insert(0, directory);
        }
    }
    let protect = protected_names(pool, deployment_id, cluster_name.trim()).await?;
    if let Some(object) = payload.as_object_mut() {
        object.insert("directories".into(), json!(directories));
        if protect.is_empty() {
            object.remove("protect");
        } else {
            object.insert("protect".into(), json!(protect));
        }
    }
    Ok(payload)
}

/// Recompute a queued backup's protect set at claim time. The payload frozen at
/// enqueue cannot know about a rollback created later.
pub async fn refresh_command_protect(
    pool: &AnyPool,
    command_id: &str,
    payload: &Value,
    deployment_id: &str,
) -> Result<Value, String> {
    let mut payload = payload.clone();
    let Some(object) = payload.as_object_mut() else {
        return Ok(payload);
    };
    let cluster = object
        .get("cluster_name")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .to_string();
    if cluster.is_empty() {
        return Ok(payload);
    }
    let protect = protected_names(pool, deployment_id, &cluster).await?;
    if protect.is_empty() {
        object.remove("protect");
    } else {
        object.insert("protect".into(), json!(protect));
    }
    let updated = payload.clone();
    sqlx::query("UPDATE core_agentcommand SET payload = ? WHERE CAST(id AS TEXT) = ?")
        .bind(serde_json::to_string(&updated).map_err(|error| error.to_string())?)
        .bind(command_id)
        .execute(pool)
        .await
        .map_err(|error| error.to_string())?;
    Ok(updated)
}

struct DeploymentContext {
    cluster_name: String,
    spec: Value,
    node_id: String,
}

async fn deployment_context(
    pool: &AnyPool,
    deployment_id: &str,
) -> Result<Option<DeploymentContext>, String> {
    let row = sqlx::query(
        "SELECT cluster_name, CAST(spec AS TEXT) AS spec, \
         CAST(target_node_id AS TEXT) AS target_node_id \
         FROM core_clusterdeployment WHERE CAST(id AS TEXT) = ? LIMIT 1",
    )
    .bind(deployment_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())?;
    let Some(row) = row else {
        return Ok(None);
    };
    let node_id: Option<String> = row.try_get("target_node_id").ok().flatten();
    let Some(node_id) = node_id.filter(|value| !value.is_empty()) else {
        return Ok(None);
    };
    let node_type: Option<String> =
        sqlx::query_scalar("SELECT node_type FROM core_agentnode WHERE CAST(id AS TEXT) = ?")
            .bind(&node_id)
            .fetch_optional(pool)
            .await
            .ok()
            .flatten();
    if node_type.as_deref() != Some("target_cluster") {
        return Ok(None);
    }
    Ok(Some(DeploymentContext {
        cluster_name: row.try_get("cluster_name").unwrap_or_default(),
        spec: row
            .try_get::<String, _>("spec")
            .ok()
            .and_then(|text| serde_json::from_str(&text).ok())
            .unwrap_or_else(|| json!({})),
        node_id,
    }))
}

/// Insert one backup command for a slot, retrying when that slot ended dead.
/// Returns the command id and whether this call created it.
async fn insert_backup(
    pool: &AnyPool,
    deployment_id: &str,
    node_id: &str,
    base_key: &str,
    payload: &Value,
) -> Result<(String, bool), String> {
    let mut key = base_key.to_string();
    let mut attempt = 0u32;
    loop {
        let existing: Option<(String, String)> = sqlx::query_as(
            "SELECT CAST(id AS TEXT) AS id, status FROM core_agentcommand \
             WHERE CAST(tenant_id AS TEXT) = (SELECT CAST(tenant_id AS TEXT) FROM core_clusterdeployment \
             WHERE CAST(id AS TEXT) = ?) AND idempotency_key = ? LIMIT 1",
        )
        .bind(deployment_id)
        .bind(&key)
        .fetch_optional(pool)
        .await
        .map_err(|error| error.to_string())?;
        if let Some((id, status)) = existing {
            if status != "failed" && status != "cancelled" {
                return Ok((id, false));
            }
            attempt += 1;
            key = format!("{base_key}:retry:{attempt}");
            continue;
        }
        let context = sqlx::query(
            "SELECT CAST(tenant_id AS TEXT) AS tenant_id, CAST(project_id AS TEXT) AS project_id, \
             requested_by_id FROM core_clusterdeployment WHERE CAST(id AS TEXT) = ? LIMIT 1",
        )
        .bind(deployment_id)
        .fetch_one(pool)
        .await
        .map_err(|error| error.to_string())?;
        let tenant_id: String = context.try_get("tenant_id").unwrap_or_default();
        let project_id: String = context.try_get("project_id").unwrap_or_default();
        let requested_by: i64 = context.try_get("requested_by_id").unwrap_or(0);
        let command_id = uuid::Uuid::new_v4().simple().to_string();
        let insert = "INSERT INTO core_agentcommand (id, command_type, payload, status, idempotency_key, \
                      plan_hash, attempts, max_attempts, created_at, updated_at, node_id, tenant_id, \
                      project_id, requested_by_id, deployment_id) \
                      VALUES (?, ?, ?, 'queued', ?, ?, 0, 3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?)";
        sqlx::query(insert)
            .bind(&command_id)
            .bind(ACTION)
            .bind(serde_json::to_string(payload).unwrap_or_else(|_| "{}".into()))
            .bind(&key)
            .bind(payload_hash(ACTION, payload))
            .bind(node_id)
            .bind(&tenant_id)
            .bind(&project_id)
            .bind(requested_by)
            .bind(deployment_id)
            .execute(pool)
            .await
            .map_err(|error| error.to_string())?;
        return Ok((command_id, true));
    }
}

/// Result of one queue attempt: the slot's command, or the stable reason this
/// deployment cannot be backed up right now.
pub enum QueueOutcome {
    Queued { command_id: String, created: bool },
    Skipped { reason: String },
}

async fn queue_for_deployment(
    pool: &AnyPool,
    deployment_id: &str,
    slot: &str,
) -> Result<QueueOutcome, String> {
    let Some(context) = deployment_context(pool, deployment_id).await? else {
        return Ok(QueueOutcome::Skipped {
            reason: "no_target_agent".into(),
        });
    };
    let payload =
        match deployment_backup_payload(pool, deployment_id, &context.spec, &context.cluster_name)
            .await
        {
            Ok(payload) => payload,
            Err(code) => {
                let reason = if code == "ETCD_BACKUP_DISABLED" {
                    "disabled".to_string()
                } else {
                    code
                };
                return Ok(QueueOutcome::Skipped { reason });
            }
        };
    let base_key = format!("etcd-backup:{deployment_id}:{slot}");
    let (command_id, created) =
        insert_backup(pool, deployment_id, &context.node_id, &base_key, &payload).await?;
    Ok(QueueOutcome::Queued {
        command_id,
        created,
    })
}

/// Queue one post-change backup. Returns whether a row was created. A missing
/// target node, a disabled policy or a malformed spec is a quiet no-op.
pub async fn queue_change_backup(
    pool: &AnyPool,
    deployment_id: &str,
    command_id: &str,
    now: &str,
) -> Result<bool, String> {
    match queue_for_deployment(
        pool,
        deployment_id,
        &format!("{}:change:cmd:{command_id}", hour_bucket(now)),
    )
    .await?
    {
        QueueOutcome::Queued { created, .. } => Ok(created),
        QueueOutcome::Skipped { .. } => Ok(false),
    }
}

/// Queue the recurring hourly backup for one deployment. The hourly slot makes
/// repeated scheduler passes in the same hour create at most one command.
pub async fn queue_hourly_backup(
    pool: &AnyPool,
    deployment_id: &str,
    now: &str,
) -> Result<bool, String> {
    match queue_for_deployment(pool, deployment_id, &hour_bucket(now)).await? {
        QueueOutcome::Queued { created, .. } => Ok(created),
        QueueOutcome::Skipped { .. } => Ok(false),
    }
}

/// One scheduler evaluation record per deployment per hour bucket, so the panel
/// can tell a cluster skipped on purpose from a scheduler that never ran.
#[allow(clippy::too_many_arguments)]
async fn record_heartbeat(
    pool: &AnyPool,
    database_url: &str,
    deployment_id: &str,
    tenant_id: &str,
    project_id: &str,
    cluster_name: &str,
    bucket: &str,
    outcome: &QueueOutcome,
) -> Result<(), String> {
    let latest = sqlx::query(
        "SELECT CAST(metadata AS TEXT) AS value FROM core_auditevent \
         WHERE event_type = ? AND resource_type = ? AND CAST(resource_id AS TEXT) = ? \
         ORDER BY created_at DESC, CAST(id AS TEXT) DESC LIMIT 1",
    )
    .bind(SCHEDULER_EVENT)
    .bind(SCHEDULER_RESOURCE)
    .bind(deployment_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())?;
    if let Some(row) = latest {
        let recorded = row
            .try_get::<Option<String>, _>("value")
            .ok()
            .flatten()
            .and_then(|text| serde_json::from_str::<Value>(&text).ok())
            .and_then(|metadata| {
                metadata
                    .get("bucket")
                    .and_then(Value::as_str)
                    .map(str::to_string)
            });
        if recorded.as_deref() == Some(bucket) {
            return Ok(());
        }
    }
    let mut metadata = Map::new();
    metadata.insert("bucket".into(), json!(bucket));
    metadata.insert("cluster_name".into(), json!(cluster_name));
    match outcome {
        QueueOutcome::Queued { command_id, .. } => {
            metadata.insert("result".into(), json!("queued"));
            metadata.insert("command_id".into(), json!(command_id));
        }
        QueueOutcome::Skipped { reason } => {
            metadata.insert("result".into(), json!("skipped"));
            metadata.insert("reason".into(), json!(reason));
        }
    }
    crate::audit::record_event(
        pool,
        database_url,
        crate::audit::AuditEvent {
            tenant_id: Some(tenant_id),
            project_id: Some(project_id),
            event_type: SCHEDULER_EVENT,
            resource_type: SCHEDULER_RESOURCE,
            resource_id: deployment_id,
            outcome: "success",
            actor_id: None,
            request_id: "",
            remote_addr: None,
            user_agent: "",
            metadata: Value::Object(metadata),
        },
    )
    .await
}

/// Queue the hourly backup for every installed Target cluster and record one
/// scheduler heartbeat per evaluation.
pub async fn dispatch_hourly_backups(
    pool: &AnyPool,
    database_url: &str,
    now: &str,
) -> Result<usize, String> {
    let rows = sqlx::query(
        "SELECT CAST(id AS TEXT) AS id, cluster_name, CAST(tenant_id AS TEXT) AS tenant_id, \
         CAST(project_id AS TEXT) AS project_id FROM core_clusterdeployment \
         WHERE status = 'succeeded' AND target_node_id IS NOT NULL",
    )
    .fetch_all(pool)
    .await
    .map_err(|error| error.to_string())?;
    let bucket = hour_bucket(now);
    let mut queued = 0;
    for row in rows {
        let deployment_id: String = row.try_get("id").unwrap_or_default();
        let cluster_name: String = row.try_get("cluster_name").unwrap_or_default();
        let tenant_id: String = row.try_get("tenant_id").unwrap_or_default();
        let project_id: String = row.try_get("project_id").unwrap_or_default();
        let outcome = queue_for_deployment(pool, &deployment_id, &bucket).await?;
        if matches!(outcome, QueueOutcome::Queued { created: true, .. }) {
            queued += 1;
        }
        if let Err(error) = record_heartbeat(
            pool,
            database_url,
            &deployment_id,
            &tenant_id,
            &project_id,
            &cluster_name,
            &bucket,
            &outcome,
        )
        .await
        {
            tracing::warn!(%error, %deployment_id, "etcd backup scheduler heartbeat failed");
        }
    }
    Ok(queued)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builds_policy_payload_and_refuses_disabled_backups() {
        let spec = json!({
            "kubernetes": {
                "etcd_backup_directory": "/var/lib/etcd/backups/",
                "etcd_backup_keep_count": 8,
                "etcd_backup_keep_max_age_hours": 72,
                "etcd_backup": true
            }
        });
        let payload = backup_payload(&spec, "prod-a").unwrap();
        assert_eq!(payload["directory"], "/var/lib/etcd/backups");
        assert_eq!(payload["directories"], json!(["/var/lib/etcd/backups"]));
        assert_eq!(payload["keep_count"], 8);
        assert_eq!(payload["pre_upgrade_keep_max_age_hours"], 168);
        assert_eq!(payload["max_age_hours"], 26);
        assert_eq!(payload["min_free_bytes"], 536_870_912);
        assert_eq!(payload["confirm_cluster_name"], "prod-a");
        assert!(payload.get("protect").is_none());

        let disabled = json!({"kubernetes": {"etcd_backup": false}});
        assert_eq!(
            backup_payload(&disabled, "prod-a").unwrap_err(),
            "ETCD_BACKUP_DISABLED"
        );
        let bad_switch = json!({"kubernetes": {"etcd_backup": "false"}});
        assert_eq!(
            backup_payload(&bad_switch, "prod-a").unwrap_err(),
            "ETCD_BACKUP_ENABLED_INVALID"
        );
        let bad_directory = json!({"kubernetes": {"etcd_backup_directory": "/tmp/backups"}});
        assert_eq!(
            backup_payload(&bad_directory, "prod-a").unwrap_err(),
            "ETCD_BACKUP_DIRECTORY_INVALID"
        );
        let bad_keep = json!({"kubernetes": {"etcd_backup_keep_count": 1}});
        assert_eq!(
            backup_payload(&bad_keep, "prod-a").unwrap_err(),
            "ETCD_BACKUP_RETENTION_INVALID"
        );
    }

    #[test]
    fn accepts_only_snapshots_of_this_cluster() {
        assert!(is_backup_name("prod-a-20260101T010101Z.db", "prod-a"));
        assert!(is_backup_name(
            "prod-a-20260101T010101Z-0a1b2c3d.db",
            "prod-a"
        ));
        assert!(!is_backup_name("prod-b-20260101T010101Z.db", "prod-a"));
        assert!(!is_backup_name("prod-a-2-20260101T010101Z.db", "prod-a"));
        assert!(!is_backup_name(
            "prod-a-20260101T010101Z.db.partial",
            "prod-a"
        ));
        assert!(!is_backup_name("prod-a-20260231T010101Z.db", "prod-a"));
        assert!(is_pre_upgrade_name(
            "snapshot-pre-prod-a-v1.34.1-20260101T010101Z.db"
        ));
        assert!(is_pre_upgrade_name(
            "snapshot-pre-v1.34.1-20260101T010101Z.db"
        ));
        assert!(!is_pre_upgrade_name("snapshot-pre-v1.34.1.db"));

        assert_eq!(
            protect_names(
                Some(&json!(["prod-a-20260101T010101Z.db", "/etc/passwd"])),
                "prod-a"
            )
            .unwrap_err(),
            "ETCD_BACKUP_PROTECT_INVALID"
        );
        assert_eq!(
            protect_names(Some(&json!(["prod-b-20260101T010101Z.db"])), "prod-a").unwrap_err(),
            "ETCD_BACKUP_PROTECT_INVALID"
        );
        assert_eq!(
            protect_names(
                Some(&json!([
                    "prod-a-20260101T010101Z.db",
                    "prod-a-20260101T010101Z.db"
                ])),
                "prod-a"
            )
            .unwrap(),
            vec!["prod-a-20260101T010101Z.db".to_string()]
        );
    }

    #[test]
    fn classifies_rollbacks_that_must_keep_their_snapshot() {
        let live = json!({"status": "running", "payload": {}, "result": {}});
        assert!(rollback_should_protect(&live));
        let unknown = json!({"status": "queued", "payload": {}, "result": {}});
        assert!(rollback_should_protect(&unknown));
        let cancelled = json!({"status": "cancelled", "payload": {}, "result": {}});
        assert!(!rollback_should_protect(&cancelled));
        let restored = json!({"status": "failed", "payload": {}, "result": {"rolled_back": true}});
        assert!(!rollback_should_protect(&restored));
        let unusable = json!({
            "status": "failed",
            "payload": {},
            "result": {"failure_code": "ROLLBACK_CLUSTER_NAME_MISMATCH"}
        });
        assert!(!rollback_should_protect(&unusable));
        let never_ran = json!({
            "status": "failed",
            "payload": {},
            "result": {"failure_code": "LEASE_EXPIRED"}
        });
        assert!(rollback_should_protect(&never_ran));
    }
}
