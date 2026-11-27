#![allow(clippy::result_large_err)]

use std::collections::HashSet;

use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use chrono::{Duration, Utc};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use sqlx::Row;

use crate::proxy::AppState;
use crate::tenancy::is_postgres;

pub async fn heartbeat(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(payload): Json<Value>,
) -> Response {
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let node = match authenticate_node(pool, &headers, payload.get("node_id")).await {
        Ok(node) => node,
        Err(response) => return response,
    };
    if serde_json::to_vec(&payload)
        .map(|bytes| bytes.len())
        .unwrap_or(0)
        > 16384
    {
        return error(
            StatusCode::PAYLOAD_TOO_LARGE,
            "heartbeat payload must not exceed 16 KiB.",
        );
    }
    let capabilities = match payload.get("capabilities") {
        None => node.capabilities,
        Some(Value::Array(items)) => {
            let mut unique = HashSet::new();
            for item in items {
                let Some(text) = item.as_str() else {
                    return error(StatusCode::BAD_REQUEST, "capabilities must contain at most 64 unique identifier strings of 1-96 characters.");
                };
                if text.len() > 96 || !valid_identifier(text) || !unique.insert(text.to_string()) {
                    return error(StatusCode::BAD_REQUEST, "capabilities must contain at most 64 unique identifier strings of 1-96 characters.");
                }
            }
            if unique.len() > 64 {
                return error(StatusCode::BAD_REQUEST, "capabilities must contain at most 64 unique identifier strings of 1-96 characters.");
            }
            Value::Array(items.clone())
        }
        Some(_) => return error(
            StatusCode::BAD_REQUEST,
            "capabilities must contain at most 64 unique identifier strings of 1-96 characters.",
        ),
    };
    let agent_version = match payload.get("agent_version") {
        None => node.agent_version,
        Some(Value::String(value))
            if value.len() <= 40 && (value.is_empty() || valid_version(value)) =>
        {
            value.clone()
        }
        _ => return error(StatusCode::BAD_REQUEST, "agent_version is invalid."),
    };
    let cluster_name = match payload.get("cluster_name") {
        None => node.cluster_name,
        Some(Value::String(value)) if value.len() <= 120 => value.clone(),
        _ => return error(StatusCode::BAD_REQUEST, "cluster_name is invalid."),
    };
    let now = timestamp();
    let remote = client_ip(&headers);
    let sql_update = if is_postgres(&state) {
        "UPDATE core_agentnode SET agent_version = ?, capabilities = ?::jsonb, cluster_name = ?, \
         status = 'online', last_seen_at = NOW(), last_seen_ip = ?, updated_at = NOW() \
         WHERE CAST(id AS TEXT) = ?"
    } else {
        "UPDATE core_agentnode SET agent_version = ?, capabilities = ?, cluster_name = ?, \
         status = 'online', last_seen_at = ?, last_seen_ip = ?, updated_at = ? \
         WHERE CAST(id AS TEXT) = ?"
    };
    let mut query = sqlx::query(sql_update)
        .bind(&agent_version)
        .bind(serde_json::to_string(&capabilities).unwrap_or_else(|_| "[]".to_string()))
        .bind(&cluster_name)
        .bind(now.as_str());
    if is_postgres(&state) {
        query = query.bind(&remote);
    } else {
        query = query.bind(&remote).bind(&now);
    }
    if let Err(value) = query.bind(&node.id).execute(pool).await {
        return database_error(value.to_string());
    }
    Json(json!({"node_id": node.id, "status": "online", "server_time": now})).into_response()
}

pub async fn renew_lease(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(command_id): Path<String>,
    Json(payload): Json<Value>,
) -> Response {
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let node = match authenticate_node(pool, &headers, payload.get("node_id")).await {
        Ok(node) => node,
        Err(response) => return response,
    };
    let Some(lease) = lease_header(&headers) else {
        return error(
            StatusCode::BAD_REQUEST,
            "A valid X-Agent-Lease header is required.",
        );
    };
    let now = timestamp();
    let expires = sql_expression_after(&state, 90);
    let select = "SELECT CAST(id AS TEXT) AS id FROM core_agentcommand WHERE CAST(id AS TEXT) = ? \
         AND CAST(node_id AS TEXT) = ? AND status = 'running' AND lease_token = ? \
         AND lease_expires_at >= ? LIMIT 1";
    match sqlx::query(select)
        .bind(&command_id)
        .bind(&node.id)
        .bind(&lease)
        .bind(&now)
        .fetch_optional(pool)
        .await
    {
        Ok(Some(_)) => {}
        Ok(None) => return error(StatusCode::NOT_FOUND, "Command not found."),
        Err(value) => return database_error(value.to_string()),
    }
    let now_expr = if is_postgres(&state) {
        "NOW()".to_string()
    } else {
        format!("'{now}'")
    };
    let update = format!(
        "UPDATE core_agentcommand SET lease_expires_at = {expires}, updated_at = {now_expr} \
         WHERE CAST(id AS TEXT) = ?"
    );
    if let Err(value) = sqlx::query(&update).bind(&command_id).execute(pool).await {
        return database_error(value.to_string());
    }
    Json(json!({"id": command_id, "lease_expires_at": expires_text(&state, &now)})).into_response()
}

pub async fn command_result(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(command_id): Path<String>,
    Json(payload): Json<Value>,
) -> Response {
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let node = match authenticate_node(pool, &headers, payload.get("node_id")).await {
        Ok(node) => node,
        Err(response) => return response,
    };
    let Some(lease) = lease_header(&headers) else {
        return error(
            StatusCode::BAD_REQUEST,
            "A valid X-Agent-Lease header is required.",
        );
    };
    let outcome = payload.get("outcome").and_then(Value::as_str).unwrap_or("");
    if !matches!(outcome, "succeeded" | "failed") {
        return error(
            StatusCode::BAD_REQUEST,
            "outcome must be succeeded or failed.",
        );
    }
    let result = payload.get("result").cloned().unwrap_or_else(|| json!({}));
    if !result.is_object()
        || serde_json::to_vec(&result)
            .map(|bytes| bytes.len())
            .unwrap_or(0)
            > 65536
    {
        return error(
            StatusCode::BAD_REQUEST,
            "result must be a JSON object no larger than 64 KiB.",
        );
    }
    let error_value = payload.get("error").cloned();
    if error_value.as_ref().is_some_and(|value| !value.is_object()) {
        return error(StatusCode::BAD_REQUEST, "error must be a JSON object.");
    }
    let lease_time = if is_postgres(&state) {
        "to_char(lease_expires_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"
    } else {
        "strftime('%Y-%m-%d %H:%M:%S', lease_expires_at)"
    };
    let select = format!(
        "SELECT CAST(id AS TEXT) AS id, status, result, error, {lease_time} AS lease_text, \
         CAST(deployment_id AS TEXT) AS deployment_id FROM core_agentcommand \
         WHERE CAST(id AS TEXT) = ? AND CAST(node_id AS TEXT) = ? LIMIT 1"
    );
    let row = match sqlx::query(&select)
        .bind(&command_id)
        .bind(&node.id)
        .fetch_optional(pool)
        .await
    {
        Ok(Some(row)) => row,
        Ok(None) => return error(StatusCode::NOT_FOUND, "Command not found."),
        Err(value) => return database_error(value.to_string()),
    };
    let status: String = row.try_get("status").unwrap_or_default();
    let stored_result: Option<String> = row.try_get("result").ok();
    let stored_error: Option<String> = row.try_get("error").ok();
    if matches!(status.as_str(), "succeeded" | "failed") {
        let same = (outcome == "succeeded"
            && status == "succeeded"
            && stored_result.as_deref() == Some(&result.to_string())
            && stored_error.is_none())
            || (outcome == "failed"
                && status == "failed"
                && stored_result.is_none()
                && stored_error.as_deref()
                    == error_value.as_ref().map(Value::to_string).as_deref());
        if same {
            return Json(json!({"id": command_id, "status": status, "deployment_id": row.try_get::<Option<String>, _>("deployment_id").ok().flatten()})).into_response();
        }
        return error(
            StatusCode::CONFLICT,
            "Command already has a different terminal result.",
        );
    }
    let expires: Option<String> = row.try_get("lease_text").ok().flatten();
    if status != "running"
        || expires
            .as_deref()
            .is_none_or(|value| value < timestamp().as_str())
    {
        return error(
            StatusCode::NOT_FOUND,
            "Command lease is invalid or expired.",
        );
    }
    let stored_lease: Option<String> =
        sqlx::query_scalar("SELECT lease_token FROM core_agentcommand WHERE CAST(id AS TEXT) = ?")
            .bind(&command_id)
            .fetch_optional(pool)
            .await
            .ok()
            .flatten();
    if stored_lease.as_deref() != Some(lease.as_str()) {
        return error(
            StatusCode::NOT_FOUND,
            "Command lease is invalid or expired.",
        );
    }
    let now = timestamp();
    let status_value = if outcome == "succeeded" {
        "succeeded"
    } else {
        "failed"
    };
    let result_text = if outcome == "succeeded" {
        Some(result.to_string())
    } else {
        None
    };
    let error_text = if outcome == "failed" {
        error_value.as_ref().map(Value::to_string)
    } else {
        None
    };
    let update = "UPDATE core_agentcommand SET status = ?, result = ?, error = ?, finished_at = ?, \
                  lease_token = NULL, lease_expires_at = NULL, updated_at = ? WHERE CAST(id AS TEXT) = ?";
    if let Err(value) = sqlx::query(update)
        .bind(status_value)
        .bind(result_text)
        .bind(error_text)
        .bind(&now)
        .bind(&now)
        .bind(&command_id)
        .execute(pool)
        .await
    {
        return database_error(value.to_string());
    }
    Json(json!({"id": command_id, "status": status_value, "deployment_id": row.try_get::<Option<String>, _>("deployment_id").ok().flatten()})).into_response()
}

struct Node {
    id: String,
    node_type: String,
    capabilities: Value,
    agent_version: String,
    cluster_name: String,
}

async fn authenticate_node(
    pool: &sqlx::AnyPool,
    headers: &HeaderMap,
    node_id: Option<&Value>,
) -> Result<Node, Response> {
    let authorization = headers
        .get("authorization")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("");
    let (scheme, token) = authorization.split_once(' ').unwrap_or(("", ""));
    if !scheme.eq_ignore_ascii_case("bearer") || token.is_empty() || token.len() > 512 {
        return Err(error(
            StatusCode::UNAUTHORIZED,
            "Invalid or revoked node credential.",
        ));
    }
    let Some(node_id) = node_id.and_then(Value::as_str) else {
        return Err(error(
            StatusCode::UNAUTHORIZED,
            "Invalid or revoked node credential.",
        ));
    };
    let sql = "SELECT CAST(id AS TEXT) AS id, node_type, CAST(capabilities AS TEXT) AS capabilities, agent_version, cluster_name, credential_hash, status, CASE WHEN client_certificate_revoked_at IS NULL THEN 0 ELSE 1 END AS certificate_revoked FROM core_agentnode WHERE CAST(id AS TEXT) = ? LIMIT 1";
    let row = match sqlx::query(sql).bind(node_id).fetch_optional(pool).await {
        Ok(Some(row)) => row,
        Ok(None) => {
            return Err(error(
                StatusCode::UNAUTHORIZED,
                "Invalid or revoked node credential.",
            ))
        }
        Err(value) => return Err(database_error(value.to_string())),
    };
    let status: String = row.try_get("status").unwrap_or_default();
    let revoked: i64 = row.try_get("certificate_revoked").unwrap_or(0);
    let expected: String = row.try_get("credential_hash").unwrap_or_default();
    let supplied = hex::encode(Sha256::digest(token.as_bytes()));
    if status == "revoked" || revoked != 0 || supplied != expected {
        return Err(error(
            StatusCode::UNAUTHORIZED,
            "Invalid or revoked node credential.",
        ));
    }
    Ok(Node {
        id: row.try_get("id").unwrap_or_default(),
        node_type: row.try_get("node_type").unwrap_or_default(),
        capabilities: row
            .try_get::<Option<String>, _>("capabilities")
            .ok()
            .flatten()
            .and_then(|value| serde_json::from_str(&value).ok())
            .unwrap_or_else(|| json!([])),
        agent_version: row.try_get("agent_version").unwrap_or_default(),
        cluster_name: row.try_get("cluster_name").unwrap_or_default(),
    })
}

fn valid_identifier(value: &str) -> bool {
    let mut chars = value.chars();
    let Some(first) = chars.next() else {
        return false;
    };
    first.is_ascii_alphanumeric()
        && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | ':' | '/' | '-'))
}

fn valid_version(value: &str) -> bool {
    let mut chars = value.chars();
    let Some(first) = chars.next() else {
        return false;
    };
    first.is_ascii_alphanumeric()
        && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '+' | '_' | '-'))
}

fn lease_header(headers: &HeaderMap) -> Option<String> {
    let raw = headers.get("x-agent-lease")?.to_str().ok()?.trim();
    let normalized = raw.replace('-', "").to_ascii_lowercase();
    if normalized.len() != 32 || !normalized.chars().all(|value| value.is_ascii_hexdigit()) {
        return None;
    }
    Some(normalized)
}

fn timestamp() -> String {
    Utc::now().format("%Y-%m-%d %H:%M:%S").to_string()
}

fn sql_expression_after(state: &AppState, seconds: i64) -> String {
    if is_postgres(state) {
        format!("NOW() + INTERVAL '{seconds} seconds'")
    } else {
        format!("datetime('now', '+{seconds} seconds')")
    }
}

fn expires_text(state: &AppState, now: &str) -> String {
    if is_postgres(state) {
        "NOW() + INTERVAL '90 seconds'".to_string()
    } else {
        let base =
            chrono::NaiveDateTime::parse_from_str(now, "%Y-%m-%d %H:%M:%S").unwrap_or_default();
        (base + Duration::seconds(90))
            .format("%Y-%m-%d %H:%M:%S")
            .to_string()
    }
}

fn client_ip(headers: &HeaderMap) -> String {
    headers
        .get("x-forwarded-for")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.split(',').next())
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .unwrap_or("")
        .to_string()
}

fn unavailable() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({"detail": "The service is temporarily unavailable."})),
    )
        .into_response()
}

fn database_error(value: String) -> Response {
    tracing::error!(%value, "rust agent runtime database operation failed");
    unavailable()
}

fn error(status: StatusCode, message: &str) -> Response {
    (status, Json(json!({"detail": message}))).into_response()
}

const EPH_COMMANDS: &[&str] = &[
    "host.network.observe",
    "node.health",
    "host.preflight",
    "cluster.plan",
    "cluster.health",
    "cluster.network.inspect",
    "cluster.observe",
    "cluster.diagnose",
    "cluster.postflight",
    "k8s.resource.read",
    "capi.cluster.observe",
    "metal3.host.inspect",
    "cluster.manifest.validate",
    "eph.preflight",
    "eph.network.configure",
    "eph.source.observe",
    "eph.bootstrap.render",
    "eph.controlstack.apply",
    "eph.metal3.bmh.apply",
    "eph.metal3.bmh.observe",
    "eph.metal3.bmh.wait_ready",
    "eph.capi.cluster.apply",
    "eph.capi.cluster.observe",
    "eph.capi.cluster.wait_ready",
    "eph.target.agent.bootstrap",
];
const TARGET_COMMANDS: &[&str] = &[
    "host.network.observe",
    "node.health",
    "host.preflight",
    "cluster.health",
    "cluster.network.inspect",
    "cluster.observe",
    "cluster.diagnose",
    "cluster.postflight",
    "cluster.manifest.validate",
    "cluster.kubernetes.version.upgrade",
    "cluster.scale",
    "cluster.kubernetes.version.rollback",
    "cluster.kubernetes.upgrade.readiness",
    "cluster.kubernetes.recovery.report",
    "cluster.worker.repair",
    "target.network.upgrade",
    "target.network.rollback",
    "target.network.revisions",
    "cluster.kubernetes.backup.run",
    "cluster.kubernetes.backup.status",
    "k8s.resource.read",
    "target.cluster.observe",
    "target.capi.observe",
    "target.capi.pivot.dry_run",
    "target.capi.pivot",
    "target.metal3.observe",
    "target.metal3.verify_ownership",
    "target.worker.ntp.validate",
    "target.ntp.control_plane.render",
    "target.ntp.control_plane.verify_bootstrap",
    "target.ntp.internal.probe",
    "target.handoff.evaluate",
    "target.eph.cleanup",
    "target.worker.provision",
    "target.network.apply",
    "cluster.scale",
    "cluster.worker.repair",
    "target.network.upgrade",
    "target.network.rollback",
    "target.network.revisions",
];
const LOCAL_COMMANDS: &[&str] = &["local.system_info", "local.disk_usage"];
const MUTATING_COMMANDS: &[&str] = &[
    "target.capi.pivot",
    "target.worker.provision",
    "target.eph.cleanup",
    "cluster.scale",
    "cluster.worker.repair",
    "target.network.upgrade",
    "target.network.rollback",
    "cluster.kubernetes.version.upgrade",
    "cluster.kubernetes.version.rollback",
    "cluster.kubernetes.backup.run",
];
const APPROVAL_REQUIRED_COMMANDS: &[&str] = &[
    "cluster.kubernetes.version.upgrade",
    "cluster.kubernetes.version.rollback",
    "cluster.scale",
    "cluster.worker.repair",
    "target.network.upgrade",
    "target.network.rollback",
];

pub async fn poll_commands(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(payload): Json<Value>,
) -> Response {
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let node = match authenticate_node(pool, &headers, payload.get("node_id")).await {
        Ok(node) => node,
        Err(response) => return response,
    };
    let node_type = match node.node_type.as_str() {
        "eph_node" => "eph_node",
        "local_runner" => "local_runner",
        _ => "target_cluster",
    };
    let allowed: &[&str] = match node_type {
        "eph_node" => EPH_COMMANDS,
        "local_runner" => LOCAL_COMMANDS,
        _ => TARGET_COMMANDS,
    };
    let now = timestamp();
    let postgres = is_postgres(&state);
    let lease_time = if postgres {
        "to_char(lease_expires_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"
    } else {
        "strftime('%Y-%m-%d %H:%M:%S', lease_expires_at)"
    };
    let approval_time = if postgres {
        "to_char(approved_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"
    } else {
        "strftime('%Y-%m-%d %H:%M:%S', approved_at)"
    };
    let select = format!(
        "SELECT CAST(id AS TEXT) AS id, command_type, CAST(payload AS TEXT) AS payload_text, \
         CAST(attempts AS INTEGER) AS attempts, CAST(max_attempts AS INTEGER) AS max_attempts, \
         status, approved_plan_hash, {approval_time} AS approved_text, {lease_time} AS lease_text \
         FROM core_agentcommand WHERE CAST(node_id AS TEXT) = ? \
         AND (status = 'queued' OR (status = 'running' AND lease_expires_at < ?)) \
         ORDER BY created_at, CAST(id AS TEXT) LIMIT 20"
    );
    let rows = match sqlx::query(&select)
        .bind(&node.id)
        .bind(&now)
        .fetch_all(pool)
        .await
    {
        Ok(rows) => rows,
        Err(value) => return database_error(value.to_string()),
    };
    let mut chosen: Option<sqlx::any::AnyRow> = None;
    for row in &rows {
        let command_type: String = row.try_get("command_type").unwrap_or_default();
        if !allowed.contains(&command_type.as_str()) {
            let _ = fail_command(
                pool,
                row,
                "COMMAND_NOT_ALLOWED_FOR_AGENT_ROLE",
                &now,
                &state,
            )
            .await;
            continue;
        }
        let status: String = row.try_get("status").unwrap_or_default();
        let attempts: i64 = row.try_get("attempts").unwrap_or(0);
        let max_attempts: i64 = row.try_get("max_attempts").unwrap_or(3);
        let expired_running = status == "running";
        if attempts >= max_attempts
            || (expired_running && MUTATING_COMMANDS.contains(&command_type.as_str()))
        {
            let _ = fail_command(pool, row, "LEASE_EXPIRED", &now, &state).await;
            continue;
        }
        if APPROVAL_REQUIRED_COMMANDS.contains(&command_type.as_str()) {
            let approved: Option<String> = row.try_get("approved_text").ok().flatten();
            let approved_hash: String = row.try_get("approved_plan_hash").unwrap_or_default();
            let cutoff_text = cutoff(15);
            if approved.is_none()
                || approved
                    .as_deref()
                    .is_some_and(|value| value < cutoff_text.as_str())
            {
                let _ = fail_command(pool, row, "APPROVAL_EXPIRED", &now, &state).await;
                continue;
            }
            let payload_text: String = row
                .try_get("payload_text")
                .unwrap_or_else(|_| "{}".to_string());
            let payload_value: Value =
                serde_json::from_str(&payload_text).unwrap_or_else(|_| json!({}));
            if approved_hash.is_empty()
                || approved_hash != payload_hash(&command_type, &payload_value)
            {
                let _ = fail_command(pool, row, "APPROVAL_HASH_MISMATCH", &now, &state).await;
                continue;
            }
        }
        chosen = Some(row.clone());
        break;
    }
    let Some(row) = chosen else {
        return Json(json!({"command": null})).into_response();
    };
    let command_id: String = row.try_get("id").unwrap_or_default();
    let command_type: String = row.try_get("command_type").unwrap_or_default();
    let payload_text: String = row
        .try_get("payload_text")
        .unwrap_or_else(|_| "{}".to_string());
    let attempts: i64 = row.try_get("attempts").unwrap_or(0);
    let lease = uuid::Uuid::new_v4().simple().to_string();
    let expires_expr = sql_expression_after(&state, 90);
    let started_expr = if postgres {
        "COALESCE(started_at, NOW())".to_string()
    } else {
        format!("COALESCE(started_at, '{now}')")
    };
    let update = format!(
        "UPDATE core_agentcommand SET status = 'running', attempts = ?, lease_token = ?, \
         lease_expires_at = {expires_expr}, started_at = {started_expr}, updated_at = {now_expr} \
         WHERE CAST(id AS TEXT) = ? AND (status = 'queued' OR status = 'running')",
        now_expr = if postgres {
            "NOW()".to_string()
        } else {
            format!("'{now}'")
        }
    );
    if let Err(value) = sqlx::query(&update)
        .bind(attempts + 1)
        .bind(&lease)
        .bind(&command_id)
        .execute(pool)
        .await
    {
        return database_error(value.to_string());
    }
    Json(json!({"command": {
        "id": command_id,
        "command_type": command_type,
        "payload": serde_json::from_str::<Value>(&payload_text).unwrap_or_else(|_| json!({})),
        "attempt": attempts + 1,
        "lease_token": lease,
        "lease_expires_at": expires_text(&state, &now),
        "deployment_id": row.try_get::<Option<String>, _>("deployment_id").ok().flatten(),
    }}))
    .into_response()
}

async fn fail_command(
    pool: &sqlx::AnyPool,
    row: &sqlx::any::AnyRow,
    code: &str,
    now: &str,
    state: &AppState,
) -> Result<(), sqlx::Error> {
    let id: String = row.try_get("id").unwrap_or_default();
    let error = json!({"code": code, "retryable": false}).to_string();
    let now_expr = if is_postgres(state) {
        "NOW()".to_string()
    } else {
        format!("'{now}'")
    };
    let sql = format!(
        "UPDATE core_agentcommand SET status = 'failed', error = ?, finished_at = {now_expr}, \
         lease_token = NULL, lease_expires_at = NULL, updated_at = {now_expr} WHERE CAST(id AS TEXT) = ?"
    );
    sqlx::query(&sql).bind(error).bind(id).execute(pool).await?;
    Ok(())
}

fn cutoff(minutes: i64) -> String {
    (Utc::now() - Duration::minutes(minutes))
        .format("%Y-%m-%d %H:%M:%S")
        .to_string()
}

fn payload_hash(command_type: &str, payload: &Value) -> String {
    let document = json!({"command_type": command_type, "payload": payload});
    let canonical = serde_json::to_string(&document).unwrap_or_else(|_| "{}".to_string());
    hex::encode(Sha256::digest(canonical.as_bytes()))
}

pub async fn approve_command(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(command_id): Path<String>,
    Json(payload): Json<Value>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let row = match sqlx::query(
        "SELECT CAST(id AS TEXT) AS id, CAST(node_id AS TEXT) AS node_id, command_type, \
         CAST(payload AS TEXT) AS payload_text, status, plan_hash, CAST(attempts AS INTEGER) AS attempts, \
         CAST(result AS TEXT) AS result_text, CAST(error AS TEXT) AS error_text, \
         CAST(requested_by_id AS BIGINT) AS requested_by, CAST(tenant_id AS TEXT) AS tenant_id, \
         CAST(project_id AS TEXT) AS project_id, CAST(deployment_id AS TEXT) AS deployment_id \
         FROM core_agentcommand WHERE CAST(id AS TEXT) = ? LIMIT 1",
    )
    .bind(&command_id)
    .fetch_optional(pool)
    .await
    {
        Ok(Some(row)) => row,
        Ok(None) => return approval_error("Agent command not found.", 404),
        Err(error) => return database_error(error.to_string()),
    };
    let tenant_id: String = row.try_get("tenant_id").unwrap_or_default();
    if !crate::billing_api::tenant_member(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return approval_error("Agent command not found.", 404);
    }
    match crate::billing_api::tenant_admin(pool, &tenant_id, user.id).await {
        Ok(true) => {}
        Ok(false) => {
            return approval_error(
                "Tenant owner or admin role is required to approve provisioning.",
                403,
            )
        }
        Err(error) => return database_error(error),
    }
    let requested_by: Option<i64> = row.try_get("requested_by").ok();
    if requested_by == Some(user.id) {
        return approval_error("A different administrator must approve this command.", 403);
    }
    let status: String = row.try_get("status").unwrap_or_default();
    if status != "waiting_approval" {
        return approval_error("Command is not waiting for approval.", 409);
    }
    let command_type: String = row.try_get("command_type").unwrap_or_default();
    let payload_text: String = row
        .try_get("payload_text")
        .unwrap_or_else(|_| "{}".to_string());
    let payload_value: Value = serde_json::from_str(&payload_text).unwrap_or_else(|_| json!({}));
    let current_hash = payload_hash(&command_type, &payload_value);
    let stored_hash: Option<String> = row.try_get("plan_hash").ok().flatten();
    let supplied = payload
        .get("plan_hash")
        .and_then(Value::as_str)
        .unwrap_or("");
    if supplied != current_hash || stored_hash.as_deref() != Some(current_hash.as_str()) {
        return approval_error(
            "Approval plan hash does not match the immutable command payload.",
            409,
        );
    }
    let now = timestamp();
    let now_expr = if is_postgres(&state) {
        "NOW()".to_string()
    } else {
        format!("'{now}'")
    };
    let sql = format!(
        "UPDATE core_agentcommand SET status = 'queued', approved_plan_hash = ?, approved_by_id = ?, \
         approved_at = {now_expr}, updated_at = {now_expr} WHERE CAST(id AS TEXT) = ? AND status = 'waiting_approval'"
    );
    if let Err(error) = sqlx::query(&sql)
        .bind(&current_hash)
        .bind(user.id)
        .bind(&command_id)
        .execute(pool)
        .await
    {
        return database_error(error.to_string());
    }
    let metadata = json!({"command_type": command_type, "plan_hash": current_hash});
    let project_id: Option<String> = row
        .try_get("project_id")
        .ok()
        .flatten()
        .filter(|value: &String| !value.is_empty());
    if let Err(error) = crate::audit::record_event(
        pool,
        state.config.database_url.as_deref().unwrap_or(""),
        crate::audit::AuditEvent {
            tenant_id: Some(tenant_id.as_str()),
            project_id: project_id.as_deref(),
            event_type: "agent.command.approved",
            resource_type: "agent_command",
            resource_id: command_id.as_str(),
            outcome: "success",
            actor_id: Some(user.id),
            request_id: "",
            remote_addr: None,
            user_agent: "",
            metadata,
        },
    )
    .await
    {
        return database_error(error.to_string());
    }
    let attempts: i64 = row.try_get("attempts").unwrap_or(0);
    Json(json!({
        "id": command_id,
        "node_id": row.try_get::<String, _>("node_id").unwrap_or_default(),
        "deployment_id": row.try_get::<Option<String>, _>("deployment_id").ok().flatten(),
        "command_type": command_type,
        "payload": payload_value,
        "status": "queued",
        "plan_hash": current_hash,
        "approved_at": now,
        "attempts": attempts,
        "result": serde_json::from_str::<Value>(&row.try_get::<Option<String>, _>("result_text").ok().flatten().unwrap_or_default()).ok(),
        "error": serde_json::from_str::<Value>(&row.try_get::<Option<String>, _>("error_text").ok().flatten().unwrap_or_default()).ok(),
        "created_at": null,
        "started_at": null,
        "finished_at": null,
    }))
    .into_response()
}

fn approval_error(message: &str, status: u16) -> Response {
    (
        StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_REQUEST),
        Json(json!({"detail": message})),
    )
        .into_response()
}

pub async fn list_node_commands(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(node_id): Path<String>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let node = match sqlx::query(
        "SELECT CAST(tenant_id AS TEXT) AS tenant_id, CAST(project_id AS TEXT) AS project_id \
         FROM core_agentnode WHERE CAST(id AS TEXT) = ? LIMIT 1",
    )
    .bind(&node_id)
    .fetch_optional(pool)
    .await
    {
        Ok(Some(row)) => row,
        Ok(None) => return approval_error("Agent node not found.", 404),
        Err(error) => return database_error(error.to_string()),
    };
    let tenant_id: String = node.try_get("tenant_id").unwrap_or_default();
    let project_id: String = node.try_get("project_id").unwrap_or_default();
    if !crate::billing_api::tenant_member(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return approval_error("Agent node not found.", 404);
    }
    let project_editor = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_projectmembership WHERE CAST(project_id AS TEXT) = ? \
         AND user_id = ? AND role IN ('admin', 'editor')",
    )
    .bind(&project_id)
    .bind(user.id)
    .fetch_one(pool)
    .await
    .unwrap_or(0)
        > 0;
    let tenant_admin = crate::billing_api::tenant_admin(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false);
    if !project_editor && !tenant_admin {
        return approval_error("Project membership required.", 403);
    }
    let ts = |column: &str| {
        if is_postgres(&state) {
            format!("to_char({column} AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') AS {column}")
        } else {
            format!("strftime('%Y-%m-%d %H:%M:%S', {column}) AS {column}")
        }
    };
    let columns = [
        ts("created_at"),
        ts("started_at"),
        ts("finished_at"),
        ts("approved_at"),
    ]
    .join(", ");
    let select = format!(
        "SELECT CAST(id AS TEXT) AS id, CAST(node_id AS TEXT) AS node_id, \
         CAST(deployment_id AS TEXT) AS deployment_id, command_type, CAST(payload AS TEXT) AS payload_text, \
         status, plan_hash, CAST(attempts AS INTEGER) AS attempts, CAST(result AS TEXT) AS result_text, \
         CAST(error AS TEXT) AS error_text, {columns} \
         FROM core_agentcommand WHERE CAST(node_id AS TEXT) = ? ORDER BY created_at DESC LIMIT 100"
    );
    let rows = match sqlx::query(&select).bind(&node_id).fetch_all(pool).await {
        Ok(rows) => rows,
        Err(error) => return database_error(error.to_string()),
    };
    let results: Vec<Value> = rows
        .iter()
        .map(|row| {
            json!({
                "id": row.try_get::<String, _>("id").unwrap_or_default(),
                "node_id": row.try_get::<String, _>("node_id").unwrap_or_default(),
                "deployment_id": row.try_get::<Option<String>, _>("deployment_id").ok().flatten(),
                "command_type": row.try_get::<String, _>("command_type").unwrap_or_default(),
                "payload": serde_json::from_str::<Value>(&row.try_get::<String, _>("payload_text").unwrap_or_default()).unwrap_or_else(|_| json!({})),
                "status": row.try_get::<String, _>("status").unwrap_or_default(),
                "plan_hash": row.try_get::<Option<String>, _>("plan_hash").ok().flatten(),
                "approved_at": timestamp_value(row, "approved_at"),
                "attempts": row.try_get::<i64, _>("attempts").unwrap_or(0),
                "result": parse_optional_json(row, "result_text"),
                "error": parse_optional_json(row, "error_text"),
                "created_at": timestamp_value(row, "created_at"),
                "started_at": timestamp_value(row, "started_at"),
                "finished_at": timestamp_value(row, "finished_at"),
            })
        })
        .collect();
    Json(json!({"count": results.len(), "results": results})).into_response()
}

fn parse_optional_json(row: &sqlx::any::AnyRow, column: &str) -> Value {
    let text: Option<String> = row.try_get(column).ok().flatten();
    text.and_then(|value| serde_json::from_str::<Value>(&value).ok())
        .unwrap_or(Value::Null)
}

fn timestamp_value(row: &sqlx::any::AnyRow, column: &str) -> Value {
    let text: Option<String> = row.try_get(column).ok().flatten();
    text.map(|value| json!(value)).unwrap_or(Value::Null)
}

pub async fn list_agent_nodes(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id)): Path<(String, String)>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    if let Err(response) = tenant_project_scope(pool, &tenant_id, &project_id, user.id).await {
        return response;
    }
    let cutoff = (Utc::now() - Duration::seconds(60))
        .format("%Y-%m-%d %H:%M:%S")
        .to_string();
    let update = format!(
        "UPDATE core_agentnode SET status = 'offline' WHERE CAST(tenant_id AS TEXT) = ? \
         AND CAST(project_id AS TEXT) = ? AND status = 'online' AND last_seen_at < '{cutoff}'"
    );
    if let Err(error) = sqlx::query(&update)
        .bind(&tenant_id)
        .bind(&project_id)
        .execute(pool)
        .await
    {
        return database_error(error.to_string());
    }
    match load_nodes(&state, pool, &tenant_id, &project_id).await {
        Ok(results) => Json(json!({"count": results.len(), "results": results})).into_response(),
        Err(response) => response,
    }
}

pub async fn create_agent_node(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id)): Path<(String, String)>,
    Json(payload): Json<Value>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let admin = match crate::billing_api::tenant_admin(pool, &tenant_id, user.id).await {
        Ok(value) => value,
        Err(error) => return database_error(error),
    };
    if let Err(response) = tenant_project_scope(pool, &tenant_id, &project_id, user.id).await {
        return response;
    }
    if !admin {
        return approval_error(
            "Tenant owner or admin role is required to enroll node agents.",
            403,
        );
    }
    let name = payload
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    if name.is_empty() || name.chars().count() > 120 {
        return approval_error("name is required and must be at most 120 characters.", 400);
    }
    let node_type = payload
        .get("node_type")
        .and_then(Value::as_str)
        .unwrap_or("");
    if !matches!(node_type, "eph_node" | "target_cluster" | "local_runner") {
        return approval_error(
            "node_type must be eph_node, target_cluster or local_runner.",
            400,
        );
    }
    let cluster_name = payload
        .get("cluster_name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    let capabilities = match payload.get("capabilities") {
        None => Vec::<String>::new(),
        Some(Value::Array(items)) if items.iter().all(Value::is_string) => items
            .iter()
            .filter_map(Value::as_str)
            .map(str::to_string)
            .collect(),
        Some(_) => return approval_error("capabilities must be a list of strings.", 400),
    };
    let labels = match payload.get("labels") {
        None => json!({}),
        Some(value @ Value::Object(_)) => value.clone(),
        Some(_) => return approval_error("labels must be a JSON object.", 400),
    };
    let token = format!(
        "{}{}",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    );
    let credential_hash = hex::encode(Sha256::digest(token.as_bytes()));
    let node_id = uuid::Uuid::new_v4().simple().to_string();
    let now = timestamp();
    let now_expr = if is_postgres(&state) {
        "NOW()".to_string()
    } else {
        format!("'{now}'")
    };
    let sql = format!(
        "INSERT INTO core_agentnode (id, name, node_type, cluster_name, agent_version, capabilities, \
         labels, credential_hash, status, created_at, updated_at, created_by_id, tenant_id, project_id) \
         VALUES (?, ?, ?, ?, '', ?, ?, ?, 'offline', {now_expr}, {now_expr}, ?, ?, ?)"
    );
    let result = sqlx::query(&sql)
        .bind(&node_id)
        .bind(&name)
        .bind(node_type)
        .bind(&cluster_name)
        .bind(json!(capabilities).to_string())
        .bind(labels.to_string())
        .bind(&credential_hash)
        .bind(user.id)
        .bind(&tenant_id)
        .bind(&project_id)
        .execute(pool)
        .await;
    if let Err(error) = result {
        let message = error.to_string();
        if message.contains("UNIQUE") || message.contains("unique") {
            return approval_error("A node with this name already exists in the project.", 409);
        }
        return database_error(message);
    }
    let metadata = json!({"node_type": node_type, "node_name": name});
    let _ = crate::audit::record_event(
        pool,
        state.config.database_url.as_deref().unwrap_or(""),
        crate::audit::AuditEvent {
            tenant_id: Some(tenant_id.as_str()),
            project_id: Some(project_id.as_str()),
            event_type: "agent.node.enrolled",
            resource_type: "agent_node",
            resource_id: node_id.as_str(),
            outcome: "success",
            actor_id: Some(user.id),
            request_id: "",
            remote_addr: None,
            user_agent: "",
            metadata,
        },
    )
    .await;
    let mut data = match load_nodes(&state, pool, &tenant_id, &project_id).await {
        Ok(results) => results
            .into_iter()
            .find(|value| value["id"] == node_id)
            .unwrap_or_else(|| json!({"id": node_id})),
        Err(_) => json!({"id": node_id}),
    };
    data["enrollment_token"] = json!(token);
    data["token_warning"] =
        json!("Store this token securely; it is shown only once. Use TLS for agent communication.");
    (StatusCode::CREATED, Json(data)).into_response()
}

async fn tenant_project_scope(
    pool: &sqlx::AnyPool,
    tenant_id: &str,
    project_id: &str,
    user_id: i64,
) -> Result<(), Response> {
    if !crate::billing_api::tenant_member(pool, tenant_id, user_id)
        .await
        .unwrap_or(false)
    {
        return Err(approval_error("Not found.", 404));
    }
    let project_in_tenant = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_project WHERE CAST(id AS TEXT) = ? AND CAST(tenant_id AS TEXT) = ?",
    )
    .bind(project_id)
    .bind(tenant_id)
    .fetch_one(pool)
    .await
    .map_err(|error| database_error(error.to_string()))?;
    if project_in_tenant == 0 {
        return Err(approval_error("Not found.", 404));
    }
    let admin = crate::billing_api::tenant_admin(pool, tenant_id, user_id)
        .await
        .unwrap_or(false);
    if !admin {
        let member = sqlx::query_scalar::<_, i64>(
            "SELECT COUNT(*) FROM core_projectmembership WHERE CAST(project_id AS TEXT) = ? AND user_id = ?",
        )
        .bind(project_id)
        .bind(user_id)
        .fetch_one(pool)
        .await
        .map_err(|error| database_error(error.to_string()))?;
        if member == 0 {
            return Err(approval_error("Not found.", 404));
        }
    }
    Ok(())
}

async fn load_nodes(
    state: &AppState,
    pool: &sqlx::AnyPool,
    tenant_id: &str,
    project_id: &str,
) -> Result<Vec<Value>, Response> {
    let ts = |column: &str| {
        if is_postgres(state) {
            format!("to_char({column} AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') AS {column}")
        } else {
            format!("strftime('%Y-%m-%d %H:%M:%S', {column}) AS {column}")
        }
    };
    let columns = [
        ts("last_seen_at"),
        ts("created_at"),
        ts("client_certificate_bound_at"),
        ts("client_certificate_revoked_at"),
    ]
    .join(", ");
    let select = format!(
        "SELECT CAST(id AS TEXT) AS id, CAST(tenant_id AS TEXT) AS tenant_id, \
         CAST(project_id AS TEXT) AS project_id, name, node_type, cluster_name, agent_version, \
         CAST(capabilities AS TEXT) AS capabilities_text, CAST(labels AS TEXT) AS labels_text, status, \
         client_certificate_fingerprint, last_seen_ip, {columns} \
         FROM core_agentnode WHERE CAST(tenant_id AS TEXT) = ? AND CAST(project_id AS TEXT) = ? \
         ORDER BY created_at DESC"
    );
    let rows = sqlx::query(&select)
        .bind(tenant_id)
        .bind(project_id)
        .fetch_all(pool)
        .await
        .map_err(|error| database_error(error.to_string()))?;
    Ok(rows
        .iter()
        .map(|row| {
            let fingerprint: Option<String> =
                row.try_get("client_certificate_fingerprint").ok().flatten();
            let revoked: Option<String> =
                row.try_get("client_certificate_revoked_at").ok().flatten();
            let certificate_status = if revoked.is_some() {
                "revoked"
            } else if fingerprint.is_some() {
                "active"
            } else {
                "unbound"
            };
            json!({
                "id": row.try_get::<String, _>("id").unwrap_or_default(),
                "tenant_id": row.try_get::<String, _>("tenant_id").unwrap_or_default(),
                "project_id": row.try_get::<String, _>("project_id").unwrap_or_default(),
                "name": row.try_get::<String, _>("name").unwrap_or_default(),
                "node_type": row.try_get::<String, _>("node_type").unwrap_or_default(),
                "cluster_name": row.try_get::<String, _>("cluster_name").unwrap_or_default(),
                "agent_version": row.try_get::<String, _>("agent_version").unwrap_or_default(),
                "capabilities": parse_optional_json(row, "capabilities_text"),
                "labels": parse_optional_json(row, "labels_text"),
                "status": row.try_get::<String, _>("status").unwrap_or_default(),
                "client_certificate": {
                    "fingerprint_sha256": fingerprint,
                    "bound_at": timestamp_value(row, "client_certificate_bound_at"),
                    "revoked_at": timestamp_value(row, "client_certificate_revoked_at"),
                    "status": certificate_status,
                },
                "last_seen_at": timestamp_value(row, "last_seen_at"),
                "last_seen_ip": row.try_get::<Option<String>, _>("last_seen_ip").ok().flatten(),
                "created_at": timestamp_value(row, "created_at"),
            })
        })
        .collect())
}
