#![allow(clippy::result_large_err)]

//! Native etcd backup surface: policy, state, manual run and directory check.

use axum::{
    extract::{Path, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use chrono::{DateTime, NaiveDateTime, Timelike, Utc};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use sqlx::Row;

use crate::proxy::AppState;

const RUN_ACTION: &str = "cluster.kubernetes.backup.run";
const STATUS_ACTION: &str = "cluster.kubernetes.backup.status";
const DEFAULT_DIRECTORY: &str = "/var/lib/etcd/backups";
const DEFAULT_MAX_AGE_HOURS: i64 = 26;
const DEFAULT_KEEP_COUNT: i64 = 2;
const DEFAULT_KEEP_MAX_AGE_HOURS: i64 = 8760;
const DEFAULT_PRE_UPGRADE_KEEP_MAX_AGE_HOURS: i64 = 168;
const DEFAULT_MIN_FREE_BYTES: i64 = 512 * 1024 * 1024;
const HISTORY_LIMIT: i64 = 12;

fn fail(status: StatusCode, code: &str, message: &str) -> Response {
    (status, Json(json!({"detail": message, "code": code}))).into_response()
}

fn is_postgres(state: &AppState) -> bool {
    state
        .config
        .database_url
        .as_deref()
        .is_some_and(|url| url.starts_with("postgres"))
}

fn timestamp_column(state: &AppState, alias: &str, name: &str) -> String {
    if is_postgres(state) {
        format!("to_char({alias}.{name} AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')")
    } else {
        format!("strftime('%Y-%m-%d %H:%M:%S', {alias}.{name})")
    }
}

fn parse_json(value: Option<String>) -> Value {
    value
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or(Value::Null)
}

fn parse_time(value: Option<String>) -> Option<NaiveDateTime> {
    let text = value?;
    NaiveDateTime::parse_from_str(&text, "%Y-%m-%d %H:%M:%S")
        .or_else(|_| NaiveDateTime::parse_from_str(&text, "%Y-%m-%dT%H:%M:%S%.fZ"))
        .ok()
}

fn advance_time(value: Option<String>) -> Option<DateTime<Utc>> {
    parse_time(value).map(|naive| naive.and_utc())
}

fn valid_directory(value: &str) -> bool {
    value.starts_with("/var/lib/etcd")
        && !value.contains("..")
        && value.chars().all(|character| {
            character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | '/' | '-')
        })
}

fn policy_int(value: Option<&Value>, low: i64, high: i64, default: i64) -> Result<i64, String> {
    match value {
        None | Some(Value::Null) => Ok(default),
        Some(Value::String(text)) if text.is_empty() => Ok(default),
        Some(Value::Number(number)) => match number.as_i64() {
            Some(parsed) if (low..=high).contains(&parsed) => Ok(parsed),
            _ => Err(format!("must be an integer between {low} and {high}")),
        },
        _ => Err(format!("must be an integer between {low} and {high}")),
    }
}

fn kubernetes(spec: &Value) -> Map<String, Value> {
    spec.get("kubernetes")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

fn spec_policy(kubernetes: &Map<String, Value>) -> Result<Value, String> {
    let directory = kubernetes
        .get("etcd_backup_directory")
        .and_then(Value::as_str)
        .filter(|value| !value.trim().is_empty())
        .unwrap_or(DEFAULT_DIRECTORY);
    if !valid_directory(directory) {
        return Err("ETCD_BACKUP_DIRECTORY_INVALID: directory must be an absolute path under /var/lib/etcd without traversal".into());
    }
    let keep_count = policy_int(
        kubernetes.get("etcd_backup_keep_count"),
        2,
        168,
        DEFAULT_KEEP_COUNT,
    )
    .map_err(|detail| format!("ETCD_BACKUP_RETENTION_INVALID: {detail}"))?;
    let keep_age = policy_int(
        kubernetes.get("etcd_backup_keep_max_age_hours"),
        1,
        8760,
        DEFAULT_KEEP_MAX_AGE_HOURS,
    )
    .map_err(|detail| format!("ETCD_BACKUP_RETENTION_INVALID: {detail}"))?;
    let pre_age = policy_int(
        kubernetes.get("etcd_backup_pre_upgrade_keep_max_age_hours"),
        1,
        8760,
        DEFAULT_PRE_UPGRADE_KEEP_MAX_AGE_HOURS,
    )
    .map_err(|detail| format!("ETCD_BACKUP_RETENTION_INVALID: {detail}"))?;
    let max_age = policy_int(
        kubernetes.get("etcd_backup_max_age_hours"),
        1,
        8760,
        DEFAULT_MAX_AGE_HOURS,
    )
    .map_err(|detail| format!("ETCD_BACKUP_STALENESS_INVALID: {detail}"))?;
    let min_free = policy_int(
        kubernetes.get("etcd_backup_min_free_bytes"),
        0,
        1024 * 1024 * 1024 * 1024,
        DEFAULT_MIN_FREE_BYTES,
    )
    .map_err(|detail| format!("ETCD_BACKUP_DISK_POLICY_INVALID: {detail}"))?;
    Ok(json!({
        "directory": directory,
        "keep_count": keep_count,
        "keep_max_age_hours": keep_age,
        "pre_upgrade_keep_max_age_hours": pre_age,
        "max_age_hours": max_age,
        "min_free_bytes": min_free,
    }))
}

fn enabled_switch(kubernetes: &Map<String, Value>) -> Result<Option<bool>, String> {
    match kubernetes.get("etcd_backup") {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Bool(value)) => Ok(Some(*value)),
        _ => Err(
            "ETCD_BACKUP_ENABLED_INVALID: spec.kubernetes.etcd_backup must be true or false".into(),
        ),
    }
}

struct DeploymentRow {
    id: String,
    tenant_id: String,
    project_id: String,
    target_node_id: Option<String>,
    cluster_name: String,
    spec: Value,
}

async fn deployment_scope(
    _state: &AppState,
    pool: &sqlx::AnyPool,
    _deployment_id: &str,
) -> Result<(String, String), Response> {
    let row = sqlx::query(
        "SELECT CAST(tenant_id AS TEXT) AS tenant_id, CAST(project_id AS TEXT) AS project_id \
         FROM core_clusterdeployment WHERE CAST(id AS TEXT) = ? LIMIT 1",
    )
    .bind(_deployment_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| {
        fail(
            StatusCode::INTERNAL_SERVER_ERROR,
            "DATABASE_ERROR",
            &error.to_string(),
        )
    })?;
    let Some(row) = row else {
        return Err(fail(StatusCode::NOT_FOUND, "NOT_FOUND", "Not found."));
    };
    Ok((
        row.try_get::<String, _>("tenant_id").unwrap_or_default(),
        row.try_get::<String, _>("project_id").unwrap_or_default(),
    ))
}

async fn load_deployment(
    state: &AppState,
    pool: &sqlx::AnyPool,
    deployment_id: &str,
) -> Result<DeploymentRow, Response> {
    let text = if is_postgres(state) {
        "CAST(deployment.spec AS TEXT)".to_string()
    } else {
        "deployment.spec".to_string()
    };
    let sql = format!(
        "SELECT CAST(deployment.id AS TEXT) AS id, CAST(deployment.tenant_id AS TEXT) AS tenant_id, \
         CAST(deployment.project_id AS TEXT) AS project_id, \
         CAST(deployment.target_node_id AS TEXT) AS target_node_id, deployment.cluster_name, \
         CAST(deployment.requested_by_id AS TEXT) AS requested_by_id, {} AS spec_text \
         FROM core_clusterdeployment deployment WHERE CAST(deployment.id AS TEXT) = ? LIMIT 1",
        text
    );
    let row = sqlx::query(&sql)
        .bind(deployment_id)
        .fetch_optional(pool)
        .await
        .map_err(|error| {
            fail(
                StatusCode::INTERNAL_SERVER_ERROR,
                "DATABASE_ERROR",
                &error.to_string(),
            )
        })?;
    let Some(row) = row else {
        return Err(fail(StatusCode::NOT_FOUND, "NOT_FOUND", "Not found."));
    };
    Ok(DeploymentRow {
        id: row.try_get::<String, _>("id").unwrap_or_default(),
        tenant_id: row.try_get::<String, _>("tenant_id").unwrap_or_default(),
        project_id: row.try_get::<String, _>("project_id").unwrap_or_default(),
        target_node_id: row
            .try_get::<Option<String>, _>("target_node_id")
            .ok()
            .flatten(),
        cluster_name: row.try_get::<String, _>("cluster_name").unwrap_or_default(),
        spec: parse_json(row.try_get::<Option<String>, _>("spec_text").ok().flatten()),
    })
}

async fn authorize(
    state: &AppState,
    headers: &HeaderMap,
    deployment_id: &str,
) -> Result<(sqlx::AnyPool, DeploymentRow), Response> {
    let user = crate::billing_api::require_user(state, headers).await?;
    let Some(pool) = state.identity_pool.clone() else {
        return Err(fail(
            StatusCode::SERVICE_UNAVAILABLE,
            "UNAVAILABLE",
            "Database unavailable.",
        ));
    };
    let (tenant_id, project_id) = deployment_scope(state, &pool, deployment_id).await?;
    match crate::tenancy::project_access(&pool, &tenant_id, &project_id, user.id).await {
        Ok(true) => {}
        Ok(false) => return Err(fail(StatusCode::NOT_FOUND, "NOT_FOUND", "Not found.")),
        Err(error) => {
            return Err(fail(
                StatusCode::INTERNAL_SERVER_ERROR,
                "DATABASE_ERROR",
                &error,
            ))
        }
    }
    let deployment = load_deployment(state, &pool, deployment_id).await?;
    Ok((pool, deployment))
}

fn payload_hash(command_type: &str, payload: &Value) -> String {
    let canonical = json!({"command_type": command_type, "payload": payload});
    let encoded = serde_json::to_string(&canonical).unwrap_or_default();
    let mut hasher = Sha256::new();
    hasher.update(encoded.as_bytes());
    hex::encode(hasher.finalize())
}

fn command_summary(row: &sqlx::any::AnyRow) -> Value {
    let result = parse_json(
        row.try_get::<Option<String>, _>("result_text")
            .ok()
            .flatten(),
    );
    let backup = result.get("backup").cloned().unwrap_or(Value::Null);
    json!({
        "command_id": row.try_get::<String, _>("id").unwrap_or_default(),
        "status": row.try_get::<String, _>("status").unwrap_or_default(),
        "created_at": row.try_get::<Option<String>, _>("created").ok().flatten(),
        "finished_at": row.try_get::<Option<String>, _>("finished").ok().flatten(),
        "backup": if backup.is_null() { Value::Null } else { backup },
        "pruned": result.get("pruned").cloned().unwrap_or(json!([])),
        "pruned_entries": result.get("pruned_entries").cloned().unwrap_or(json!([])),
        "count": result.get("count").cloned().unwrap_or(Value::Null),
        "failure_code": result.get("failure_code").cloned().unwrap_or(Value::Null),
        "error": row.try_get::<Option<String>, _>("error_text").ok().flatten(),
    })
}

async fn command_rows(
    state: &AppState,
    pool: &sqlx::AnyPool,
    deployment_id: &str,
    run_only: bool,
) -> Result<Vec<sqlx::any::AnyRow>, String> {
    let created = timestamp_column(state, "command", "created_at");
    let finished = timestamp_column(state, "command", "finished_at");
    let text = if is_postgres(state) {
        "CAST(command.result AS TEXT)"
    } else {
        "command.result"
    };
    let filter = if run_only {
        "command.command_type = ?"
    } else {
        "command.command_type IN (?, ?)"
    };
    let sql = format!(
        "SELECT CAST(command.id AS TEXT) AS id, command.status, command.command_type, \
         {created} AS created, {finished} AS finished, {text} AS result_text, command.error AS error_text \
         FROM core_agentcommand command WHERE CAST(command.deployment_id AS TEXT) = ? AND {filter} \
         ORDER BY command.created_at DESC LIMIT {HISTORY_LIMIT}"
    );
    let mut query = sqlx::query(&sql).bind(deployment_id);
    query = query.bind(RUN_ACTION);
    if !run_only {
        query = query.bind(STATUS_ACTION);
    }
    query
        .fetch_all(pool)
        .await
        .map_err(|error| error.to_string())
}

fn policy_response(deployment: &DeploymentRow, spec: &Value) -> Result<(Value, Value), String> {
    let kubernetes = kubernetes(spec);
    let enabled = enabled_switch(&kubernetes)?;
    let policy = spec_policy(&kubernetes)?;
    let mut body = policy.clone();
    if let Some(object) = body.as_object_mut() {
        object.insert("enabled".into(), json!(enabled.unwrap_or(true)));
        object.insert("valid".into(), json!(true));
        object.insert("protect".into(), json!([]));
    }
    Ok((
        body,
        json!({
            "deployment_id": deployment.id,
            "cluster_name": deployment.cluster_name,
        }),
    ))
}

/// `GET /api/v1/cluster-deployments/{deployment_id}/etcd-backup/`
pub async fn get_state(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(deployment_id): Path<String>,
) -> Response {
    let (pool, deployment) = match authorize(&state, &headers, &deployment_id).await {
        Ok(value) => value,
        Err(response) => return response,
    };
    let (policy, base) = match policy_response(&deployment, &deployment.spec) {
        Ok(value) => value,
        Err(message) => {
            let code = message.split(':').next().unwrap_or("ETCD_BACKUP_INVALID");
            return fail(StatusCode::BAD_REQUEST, code, &message);
        }
    };
    let history_rows = match command_rows(&state, &pool, &deployment_id, false).await {
        Ok(rows) => rows,
        Err(error) => return fail(StatusCode::INTERNAL_SERVER_ERROR, "DATABASE_ERROR", &error),
    };
    let run_rows = match command_rows(&state, &pool, &deployment_id, true).await {
        Ok(rows) => rows,
        Err(error) => return fail(StatusCode::INTERNAL_SERVER_ERROR, "DATABASE_ERROR", &error),
    };
    let history: Vec<Value> = history_rows.iter().map(command_summary).collect();
    let latest_run = run_rows.first().map(command_summary);
    let enabled = policy
        .get("enabled")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let max_age = policy
        .get("max_age_hours")
        .and_then(Value::as_i64)
        .unwrap_or(DEFAULT_MAX_AGE_HOURS);
    let newest_backup = latest_run
        .as_ref()
        .map(|value| value.get("backup").cloned().unwrap_or(Value::Null))
        .unwrap_or(Value::Null);
    let newest_name = newest_backup
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let newest_age = latest_run
        .as_ref()
        .and_then(|value| value.get("finished_at").cloned())
        .and_then(|value| value.as_str().map(str::to_string))
        .and_then(|text| advance_time(Some(text)))
        .map(|at| (Utc::now() - at).num_minutes() as f64 / 60.0)
        .map(|age| (age * 1000.0).round() / 1000.0);
    let stale = newest_age.map(|age| age > max_age as f64).unwrap_or(true);
    let mut findings = Vec::new();
    if newest_name.is_empty() {
        findings.push(json!({
            "name": "backup.command",
            "ok": false,
            "code": "ETCD_BACKUP_NOT_RUN_YET",
            "detail": "the scheduler has not reported a backup for this cluster",
        }));
    } else if stale {
        findings.push(json!({
            "name": "backup.newest",
            "ok": false,
            "code": "ETCD_BACKUP_STALE",
            "detail": format!("newest snapshot is {newest_name}, {}h old (limit {max_age}h)", newest_age.unwrap_or_default()),
        }));
    }
    let ok = enabled && !stale && !newest_name.is_empty() && findings.is_empty();
    let restore_point = if newest_name.is_empty() {
        Value::Null
    } else {
        json!({
            "name": newest_name,
            "kind": "periodic",
            "path": format!("{}/{}", policy.get("directory").and_then(Value::as_str).unwrap_or(DEFAULT_DIRECTORY), newest_name),
            "listed": true,
            "unconfirmed": false,
        })
    };
    let mut body = base;
    if let Some(object) = body.as_object_mut() {
        object.insert("policy".into(), policy);
        object.insert(
            "directories".into(),
            json!([{"directory": DEFAULT_DIRECTORY, "source": "policy"}]),
        );
        object.insert("invalid_directories".into(), json!([]));
        object.insert("restore_point".into(), restore_point);
        object.insert("unconfirmed_restore_points".into(), json!([]));
        object.insert("scheduler_last_run".into(), Value::Null);
        object.insert("scheduler_age_hours".into(), Value::Null);
        object.insert("last_run".into(), latest_run.unwrap_or(Value::Null));
        object.insert("history".into(), json!(history));
        object.insert("newest".into(), newest_backup);
        object.insert(
            "newest_age_hours".into(),
            newest_age.map(|v| json!(v)).unwrap_or(Value::Null),
        );
        object.insert("stale".into(), json!(stale));
        object.insert("enabled".into(), json!(enabled));
        object.insert("ok".into(), json!(ok));
        object.insert("findings".into(), json!(findings));
    }
    Json(body).into_response()
}

/// `POST /api/v1/cluster-deployments/{deployment_id}/etcd-backup/policy/`
pub async fn update_policy(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(deployment_id): Path<String>,
    Json(body): Json<Value>,
) -> Response {
    let (pool, deployment) = match authorize(&state, &headers, &deployment_id).await {
        Ok(value) => value,
        Err(response) => return response,
    };
    let Some(object) = body.as_object() else {
        return fail(
            StatusCode::BAD_REQUEST,
            "policy_invalid",
            "Request body must be a JSON object.",
        );
    };
    let allowed = [
        "keep_count",
        "keep_max_age_hours",
        "pre_upgrade_keep_max_age_hours",
        "max_age_hours",
        "directory",
        "min_free_bytes",
        "enabled",
    ];
    if object.is_empty() || object.keys().any(|key| !allowed.contains(&key.as_str())) {
        if object.is_empty() {
            return fail(
                StatusCode::BAD_REQUEST,
                "policy_empty",
                "No policy fields were provided.",
            );
        }
        return fail(
            StatusCode::BAD_REQUEST,
            "policy_invalid",
            "Unknown policy field.",
        );
    }
    let mut kubernetes = kubernetes(&deployment.spec);
    let mapping = [
        ("keep_count", "etcd_backup_keep_count"),
        ("keep_max_age_hours", "etcd_backup_keep_max_age_hours"),
        (
            "pre_upgrade_keep_max_age_hours",
            "etcd_backup_pre_upgrade_keep_max_age_hours",
        ),
        ("max_age_hours", "etcd_backup_max_age_hours"),
        ("directory", "etcd_backup_directory"),
        ("min_free_bytes", "etcd_backup_min_free_bytes"),
    ];
    for (field, key) in mapping {
        if let Some(value) = object.get(field) {
            kubernetes.insert(key.into(), value.clone());
        }
    }
    if let Some(value) = object.get("enabled") {
        if !value.is_boolean() {
            return fail(
                StatusCode::BAD_REQUEST,
                "ETCD_BACKUP_ENABLED_INVALID",
                "enabled must be true or false, not a string or a number",
            );
        }
        kubernetes.insert("etcd_backup".into(), value.clone());
    }
    let _policy = match enabled_switch(&kubernetes).and_then(|_| spec_policy(&kubernetes)) {
        Ok(policy) => policy,
        Err(message) => {
            let (code, detail) = message
                .split_once(": ")
                .unwrap_or(("policy_invalid", message.as_str()));
            return fail(StatusCode::BAD_REQUEST, code, detail);
        }
    };
    let mut spec = if deployment.spec.is_object() {
        deployment.spec.clone()
    } else {
        json!({})
    };
    if let Some(map) = spec.as_object_mut() {
        map.insert("kubernetes".into(), Value::Object(kubernetes));
    }
    let serialized = spec.to_string();
    if let Err(error) = sqlx::query(
        "UPDATE core_clusterdeployment SET spec = ?, updated_at = CURRENT_TIMESTAMP \
         WHERE CAST(id AS TEXT) = ?",
    )
    .bind(&serialized)
    .bind(&deployment.id)
    .execute(&pool)
    .await
    {
        return fail(
            StatusCode::INTERNAL_SERVER_ERROR,
            "DATABASE_ERROR",
            &error.to_string(),
        );
    }
    let policy_values = Value::Object(object.clone());
    if let Err(error) = crate::audit::record_event(
        &pool,
        state.config.database_url.as_deref().unwrap_or(""),
        crate::audit::AuditEvent {
            tenant_id: Some(deployment.tenant_id.as_str()),
            project_id: Some(deployment.project_id.as_str()),
            event_type: "cluster.etcd_backup.policy.updated",
            resource_type: "cluster_deployment",
            resource_id: deployment.id.as_str(),
            outcome: "success",
            actor_id: None,
            request_id: "",
            remote_addr: None,
            user_agent: "",
            metadata: json!({
                "cluster_name": deployment.cluster_name,
                "policy": policy_values,
            }),
        },
    )
    .await
    {
        tracing::warn!(%error, "etcd backup policy audit event failed");
    }
    let (mut response, _) = match policy_response(&deployment, &spec) {
        Ok(value) => value,
        Err(message) => return fail(StatusCode::BAD_REQUEST, "policy_invalid", &message),
    };
    if let Some(map) = response.as_object_mut() {
        map.insert("cluster_name".into(), json!(deployment.cluster_name));
        map.insert("deployment_id".into(), json!(deployment.id));
    }
    Json(response).into_response()
}

async fn queue(
    state: &AppState,
    pool: &sqlx::AnyPool,
    deployment: &DeploymentRow,
    user_id: i64,
    command_type: &str,
    key: &str,
) -> Result<(String, String, bool), Response> {
    let existing = sqlx::query(
        "SELECT CAST(id AS TEXT) AS id, status FROM core_agentcommand \
         WHERE CAST(tenant_id AS TEXT) = ? AND idempotency_key = ? LIMIT 1",
    )
    .bind(&deployment.tenant_id)
    .bind(key)
    .fetch_optional(pool)
    .await;
    let existing = match existing {
        Ok(value) => value,
        Err(error) => {
            return Err(fail(
                StatusCode::INTERNAL_SERVER_ERROR,
                "DATABASE_ERROR",
                &error.to_string(),
            ))
        }
    };
    if let Some(row) = existing {
        let status = row.try_get::<String, _>("status").unwrap_or_default();
        if status != "failed" && status != "cancelled" {
            return Ok((
                row.try_get::<String, _>("id").unwrap_or_default(),
                status,
                false,
            ));
        }
    }
    let kubernetes = kubernetes(&deployment.spec);
    if let Err(message) = enabled_switch(&kubernetes).and_then(|_| spec_policy(&kubernetes)) {
        let code = message.split(':').next().unwrap_or("ETCD_BACKUP_INVALID");
        return Err(fail(StatusCode::CONFLICT, code, &message));
    }
    let payload = match crate::backup_queue::deployment_backup_payload(
        pool,
        &deployment.id,
        &deployment.spec,
        &deployment.cluster_name,
    )
    .await
    {
        Ok(payload) => payload,
        Err(code) => {
            let detail = format!("etcd backup request refused: {code}");
            return Err(fail(StatusCode::CONFLICT, &code, &detail));
        }
    };
    let hash = payload_hash(command_type, &payload);
    let id = uuid::Uuid::new_v4().simple().to_string();
    let sql = "INSERT INTO core_agentcommand \
        (id, command_type, payload, status, idempotency_key, plan_hash, attempts, \
         created_at, updated_at, node_id, project_id, requested_by_id, tenant_id, deployment_id) \
        VALUES (?, ?, ?, 'queued', ?, ?, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?)";
    if let Err(error) = sqlx::query(sql)
        .bind(&id)
        .bind(command_type)
        .bind(payload.to_string())
        .bind(key)
        .bind(&hash)
        .bind(&deployment.target_node_id)
        .bind(&deployment.project_id)
        .bind(user_id)
        .bind(&deployment.tenant_id)
        .bind(&deployment.id)
        .execute(pool)
        .await
    {
        return Err(fail(
            StatusCode::INTERNAL_SERVER_ERROR,
            "DATABASE_ERROR",
            &error.to_string(),
        ));
    }
    let _ = state;
    Ok((id, "queued".into(), true))
}

fn unqueueable(deployment: &DeploymentRow) -> Option<(&'static str, &'static str)> {
    let kubernetes = kubernetes(&deployment.spec);
    match enabled_switch(&kubernetes) {
        Ok(Some(false)) => return Some(("disabled", "backups are switched off for this cluster")),
        Err(_) => {
            return Some((
                "ETCD_BACKUP_ENABLED_INVALID",
                "spec.kubernetes.etcd_backup must be true or false",
            ))
        }
        Ok(_) => {}
    }
    if deployment.target_node_id.is_none() {
        return Some((
            "no_target_agent",
            "this deployment has no Target Agent to run the backup",
        ));
    }
    None
}

async fn queue_endpoint(
    state: AppState,
    headers: HeaderMap,
    deployment_id: String,
    command_type: &'static str,
    bucket: String,
    already_code: &'static str,
) -> Response {
    let (pool, deployment) = match authorize(&state, &headers, &deployment_id).await {
        Ok(value) => value,
        Err(response) => return response,
    };
    if let Some((reason, detail)) = unqueueable(&deployment) {
        let code = if reason.starts_with("ETCD_BACKUP_") {
            reason.to_string()
        } else {
            format!("ETCD_BACKUP_{}", reason.to_uppercase())
        };
        return fail(StatusCode::CONFLICT, &code, detail);
    }
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(value) => value,
        Err(response) => return response,
    };
    let key = format!("{bucket}:{}", deployment.id);
    match queue(&state, &pool, &deployment, user.id, command_type, &key).await {
        Ok((command_id, status, true)) => Json(json!({
            "deployment_id": deployment.id,
            "command_id": command_id,
            "status": status,
            "created": true,
            "code": "",
        }))
        .into_response(),
        Ok((command_id, status, false)) => Json(json!({
            "deployment_id": deployment.id,
            "command_id": command_id,
            "status": status,
            "created": false,
            "code": already_code,
        }))
        .into_response(),
        Err(response) => response,
    }
}

/// `POST /api/v1/cluster-deployments/{deployment_id}/etcd-backup/run/`
pub async fn run_backup(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(deployment_id): Path<String>,
) -> Response {
    let now = Utc::now();
    let bucket = format!(
        "etcd-backup:{}:{}T{:02}",
        deployment_id,
        now.format("%Y%m%d"),
        now.hour()
    );
    queue_endpoint(
        state,
        headers,
        deployment_id,
        RUN_ACTION,
        bucket,
        "ETCD_BACKUP_ALREADY_QUEUED",
    )
    .await
}

/// `POST /api/v1/cluster-deployments/{deployment_id}/etcd-backup/check/`
pub async fn run_check(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(deployment_id): Path<String>,
) -> Response {
    let now = Utc::now();
    let bucket = format!(
        "etcd-backup-status:{}:{}T{:02}{}",
        deployment_id,
        now.format("%Y%m%d"),
        now.hour(),
        now.minute() / 10
    );
    queue_endpoint(
        state,
        headers,
        deployment_id,
        STATUS_ACTION,
        bucket,
        "ETCD_BACKUP_CHECK_ALREADY_QUEUED",
    )
    .await
}
