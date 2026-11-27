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
    let sql = "SELECT CAST(id AS TEXT) AS id, CAST(capabilities AS TEXT) AS capabilities, agent_version, cluster_name, credential_hash, status, CASE WHEN client_certificate_revoked_at IS NULL THEN 0 ELSE 1 END AS certificate_revoked FROM core_agentnode WHERE CAST(id AS TEXT) = ? LIMIT 1";
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
