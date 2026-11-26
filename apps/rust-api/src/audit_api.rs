#![allow(clippy::result_large_err)]

use std::collections::BTreeMap;

use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use chrono::NaiveDateTime;
use serde::Deserialize;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use sqlx::{AnyPool, Row};

use crate::context::{authenticate, AuthError};
use crate::proxy::AppState;
use crate::tenancy::timestamp_field_expr;

fn created_expr(postgres: bool, alias: &str) -> String {
    if postgres {
        timestamp_field_expr(postgres, alias, "created_at")
    } else {
        format!("CAST({alias}.created_at AS TEXT)")
    }
}
use crate::tenancy::{is_postgres, normalize_timestamp, tenant_role};

#[derive(Deserialize, Default)]
pub struct AuditQuery {
    project_id: Option<String>,
    limit: Option<String>,
    offset: Option<String>,
}

/// Native replacement for tenant and global audit list/verify endpoints.
pub async fn list_tenant_events(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
    Query(query): Query<AuditQuery>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let role = match tenant_role(pool, &tenant_id, user.id).await {
        Ok(Some(role)) => role,
        Ok(None) => return not_found("Not found."),
        Err(error) => return database_error_message(error),
    };
    if role != "owner" && role != "admin" {
        return (
            StatusCode::FORBIDDEN,
            Json(
                json!({"detail": "Tenant administrator permission required to view audit events."}),
            ),
        )
            .into_response();
    }
    let (limit, offset) = match audit_window(&query) {
        Ok(window) => window,
        Err(response) => return response,
    };
    let project_id = match query.project_id.as_deref() {
        Some(project_id) => {
            let exists = sqlx::query_scalar::<_, i64>(
                "SELECT COUNT(*) FROM core_project \
                 WHERE CAST(id AS TEXT) = ? AND CAST(tenant_id AS TEXT) = ?",
            )
            .bind(project_id)
            .bind(&tenant_id)
            .fetch_one(pool)
            .await;
            match exists {
                Ok(0) => return not_found("Project not found."),
                Ok(_) => Some(project_id.to_string()),
                Err(error) => return database_error(error),
            }
        }
        None => None,
    };
    list_events(
        pool,
        &state,
        Some(&tenant_id),
        project_id.as_deref(),
        limit,
        offset,
    )
    .await
}

pub async fn verify_tenant_events(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let role = match tenant_role(pool, &tenant_id, user.id).await {
        Ok(Some(role)) => role,
        Ok(None) => return not_found("Not found."),
        Err(error) => return database_error_message(error),
    };
    if role != "owner" && role != "admin" {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"detail": "Tenant administrator permission required to verify audit events."})),
        )
            .into_response();
    }
    match verify_chain(pool, &state, Some(&tenant_id)).await {
        Ok(result) => Json(result).into_response(),
        Err(error) => database_error_message(error),
    }
}

pub async fn list_global_events(
    State(state): State<AppState>,
    headers: HeaderMap,
    Query(query): Query<AuditQuery>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    if !user.is_staff {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"detail": "Platform administrator permission required to view the global audit chain."})),
        )
            .into_response();
    }
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let (limit, offset) = match audit_window(&query) {
        Ok(window) => window,
        Err(response) => return response,
    };
    list_events(pool, &state, None, None, limit, offset).await
}

pub async fn verify_global_events(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    if !user.is_staff {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"detail": "Platform administrator permission required to view the global audit chain."})),
        )
            .into_response();
    }
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    match verify_chain(pool, &state, None).await {
        Ok(result) => Json(result).into_response(),
        Err(error) => database_error_message(error),
    }
}

async fn list_events(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: Option<&str>,
    project_id: Option<&str>,
    limit: i64,
    offset: i64,
) -> Response {
    let postgres = is_postgres(state);
    let json_cast = if postgres { "::text" } else { "" };
    let tenant_filter = if tenant_id.is_some() {
        "CAST(e.tenant_id AS TEXT) = ?"
    } else {
        "e.tenant_id IS NULL"
    };
    let project_filter = if project_id.is_some() {
        " AND CAST(e.project_id AS TEXT) = ?"
    } else {
        ""
    };
    let table = "core_auditevent e LEFT JOIN auth_user u ON u.id = e.actor_id";
    let count_sql = format!("SELECT COUNT(*) FROM {table} WHERE {tenant_filter}{project_filter}");
    let mut count = sqlx::query_scalar::<_, i64>(&count_sql);
    if let Some(tenant_id) = tenant_id {
        count = count.bind(tenant_id);
    }
    if let Some(project_id) = project_id {
        count = count.bind(project_id);
    }
    let total = match count.fetch_one(pool).await {
        Ok(value) => value,
        Err(error) => return database_error(error),
    };
    let sql = format!(
        "SELECT CAST(e.id AS TEXT) AS id, CAST(e.tenant_id AS TEXT) AS tenant_id, \
         CAST(e.project_id AS TEXT) AS project_id, e.actor_id, u.username AS actor_username, \
         e.event_type, e.resource_type, e.resource_id, e.outcome, e.request_id, e.remote_addr, \
         e.user_agent, e.metadata{json_cast} AS metadata_text, e.previous_hash, e.event_hash, \
         {created} FROM {table} WHERE {tenant_filter}{project_filter} \
         ORDER BY e.created_at DESC, e.id DESC LIMIT ? OFFSET ?",
        created = created_expr(postgres, "e"),
    );
    let mut query = sqlx::query(&sql);
    if let Some(tenant_id) = tenant_id {
        query = query.bind(tenant_id);
    }
    if let Some(project_id) = project_id {
        query = query.bind(project_id);
    }
    let rows = match query.bind(limit).bind(offset).fetch_all(pool).await {
        Ok(rows) => rows,
        Err(error) => return database_error(error),
    };
    let results: Vec<Value> = rows
        .iter()
        .map(|row| {
            let metadata: Value = row
                .try_get::<Option<String>, _>("metadata_text")
                .ok()
                .flatten()
                .and_then(|text| serde_json::from_str(&text).ok())
                .unwrap_or_else(|| json!({}));
            json!({
                "id": row.try_get::<String, _>("id").unwrap_or_default(),
                "tenant_id": row.try_get::<Option<String>, _>("tenant_id").ok().flatten(),
                "project_id": row.try_get::<Option<String>, _>("project_id").ok().flatten(),
                "actor_id": row.try_get::<Option<i64>, _>("actor_id").ok().flatten(),
                "actor_username": row.try_get::<Option<String>, _>("actor_username").ok().flatten(),
                "event_type": row.try_get::<String, _>("event_type").unwrap_or_default(),
                "resource_type": row.try_get::<String, _>("resource_type").unwrap_or_default(),
                "resource_id": row.try_get::<String, _>("resource_id").unwrap_or_default(),
                "outcome": row.try_get::<String, _>("outcome").unwrap_or_default(),
                "request_id": row.try_get::<String, _>("request_id").unwrap_or_default(),
                "remote_addr": row.try_get::<Option<String>, _>("remote_addr").ok().flatten(),
                "user_agent": row.try_get::<String, _>("user_agent").unwrap_or_default(),
                "metadata": metadata,
                "previous_hash": row.try_get::<String, _>("previous_hash").unwrap_or_default(),
                "event_hash": row.try_get::<String, _>("event_hash").unwrap_or_default(),
                "created_at": row.try_get::<String, _>("created").ok().map(|value| normalize_timestamp(&value)),
            })
        })
        .collect();
    Json(json!({
        "count": total,
        "limit": limit,
        "offset": offset,
        "results": results,
    }))
    .into_response()
}

async fn verify_chain(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: Option<&str>,
) -> Result<Value, String> {
    let postgres = is_postgres(state);
    let json_cast = if postgres { "::text" } else { "" };
    let filter = if tenant_id.is_some() {
        "CAST(tenant_id AS TEXT) = ?"
    } else {
        "tenant_id IS NULL"
    };
    let sql = format!(
        "SELECT CAST(id AS TEXT) AS id, CAST(tenant_id AS TEXT) AS tenant_id, \
         CAST(project_id AS TEXT) AS project_id, actor_id, event_type, resource_type, \
         resource_id, outcome, request_id, remote_addr, user_agent, metadata{json_cast} AS metadata_text, \
         previous_hash, event_hash, {created} AS created_text \
         FROM core_auditevent WHERE {filter} ORDER BY created_at ASC, id ASC",
        created = created_expr(postgres, "core_auditevent"),
    );
    let mut query = sqlx::query(&sql);
    if let Some(tenant_id) = tenant_id {
        query = query.bind(tenant_id);
    }
    let rows = query
        .fetch_all(pool)
        .await
        .map_err(|error| error.to_string())?;
    let mut previous_hash = String::new();
    let mut verified_count = 0i64;
    for row in rows {
        let id: String = row.try_get("id").map_err(|error| error.to_string())?;
        let canonical = canonical_event(
            &id,
            row.try_get::<Option<String>, _>("tenant_id")
                .map_err(|error| error.to_string())?
                .as_deref(),
            row.try_get::<Option<String>, _>("project_id")
                .map_err(|error| error.to_string())?
                .as_deref(),
            row.try_get::<Option<i64>, _>("actor_id")
                .map_err(|error| error.to_string())?,
            row.try_get::<String, _>("event_type")
                .map_err(|error| error.to_string())?,
            row.try_get::<String, _>("resource_type")
                .map_err(|error| error.to_string())?,
            row.try_get::<String, _>("resource_id")
                .map_err(|error| error.to_string())?,
            row.try_get::<String, _>("outcome")
                .map_err(|error| error.to_string())?,
            row.try_get::<String, _>("request_id")
                .map_err(|error| error.to_string())?,
            row.try_get::<Option<String>, _>("remote_addr")
                .map_err(|error| error.to_string())?,
            row.try_get::<String, _>("user_agent")
                .map_err(|error| error.to_string())?,
            &row.try_get::<Option<String>, _>("metadata_text")
                .ok()
                .flatten()
                .unwrap_or_else(|| "{}".to_string()),
            row.try_get::<String, _>("previous_hash")
                .map_err(|error| error.to_string())?,
            &row.try_get::<String, _>("created_text")
                .map_err(|error| error.to_string())?,
        );
        let expected_hash = hex::encode(Sha256::digest(canonical.as_bytes()));
        let stored_previous: String = row
            .try_get("previous_hash")
            .map_err(|error| error.to_string())?;
        let stored_hash: String = row
            .try_get("event_hash")
            .map_err(|error| error.to_string())?;
        if stored_previous != previous_hash || stored_hash != expected_hash {
            return Ok(json!({
                "valid": false,
                "verified_count": verified_count,
                "invalid_event_id": id,
                "expected_previous_hash": previous_hash,
                "stored_previous_hash": stored_previous,
            }));
        }
        previous_hash = stored_hash;
        verified_count += 1;
    }
    Ok(json!({
        "valid": true,
        "verified_count": verified_count,
        "invalid_event_id": Value::Null,
        "head_hash": previous_hash,
    }))
}

#[allow(clippy::too_many_arguments)]
fn canonical_event(
    id: &str,
    tenant_id: Option<&str>,
    project_id: Option<&str>,
    actor_id: Option<i64>,
    event_type: String,
    resource_type: String,
    resource_id: String,
    outcome: String,
    request_id: String,
    remote_addr: Option<String>,
    user_agent: String,
    metadata_text: &str,
    previous_hash: String,
    created_at: &str,
) -> String {
    let metadata = serde_json::from_str::<Value>(metadata_text)
        .map(sort_value)
        .unwrap_or_else(|_| json!({}));
    let mut map: BTreeMap<&str, Value> = BTreeMap::new();
    map.insert("id", json!(id));
    map.insert("tenant_id", json!(tenant_id));
    map.insert("project_id", json!(project_id));
    map.insert("actor_id", json!(actor_id));
    map.insert("event_type", json!(event_type));
    map.insert("resource_type", json!(resource_type));
    map.insert("resource_id", json!(resource_id));
    map.insert("outcome", json!(outcome));
    map.insert("request_id", json!(request_id));
    map.insert("remote_addr", json!(remote_addr));
    map.insert("user_agent", json!(user_agent));
    map.insert("metadata", metadata);
    map.insert("previous_hash", json!(previous_hash));
    map.insert("created_at", json!(canonical_timestamp(created_at)));
    serde_json::to_string(&map).unwrap_or_default()
}

fn sort_value(value: Value) -> Value {
    match value {
        Value::Object(map) => {
            let sorted: BTreeMap<String, Value> = map
                .into_iter()
                .map(|(key, value)| (key, sort_value(value)))
                .collect();
            Value::Object(sorted.into_iter().collect::<Map<String, Value>>())
        }
        Value::Array(items) => Value::Array(items.into_iter().map(sort_value).collect()),
        other => other,
    }
}

fn canonical_timestamp(value: &str) -> String {
    for format in ["%Y-%m-%dT%H:%M:%S%.fZ", "%Y-%m-%d %H:%M:%S%.f"] {
        if let Ok(timestamp) = NaiveDateTime::parse_from_str(value, format) {
            return format_python_timestamp(timestamp, 0);
        }
    }
    if let Ok(timestamp) = chrono::DateTime::parse_from_rfc3339(value) {
        return format_python_timestamp(
            timestamp.naive_utc(),
            timestamp.offset().local_minus_utc(),
        );
    }
    value.to_string()
}

fn format_python_timestamp(timestamp: NaiveDateTime, offset_seconds: i32) -> String {
    let base = timestamp.format("%Y-%m-%dT%H:%M:%S").to_string();
    let micros = timestamp.and_utc().timestamp_subsec_micros();
    let offset = if offset_seconds == 0 {
        "+00:00".to_string()
    } else {
        let minutes = offset_seconds / 60;
        format!(
            "{}{:02}:{:02}",
            if minutes >= 0 { "+" } else { "-" },
            minutes.abs() / 60,
            minutes.abs() % 60
        )
    };
    if micros == 0 {
        format!("{base}{offset}")
    } else {
        format!("{base}.{micros:06}{offset}")
    }
}

fn audit_window(query: &AuditQuery) -> Result<(i64, i64), Response> {
    let limit = match query.limit.as_deref() {
        Some(value) => value
            .parse::<i64>()
            .map_err(|_| bad_request("limit and offset must be integers."))?,
        None => 50,
    };
    let offset = match query.offset.as_deref() {
        Some(value) => value
            .parse::<i64>()
            .map_err(|_| bad_request("limit and offset must be integers."))?,
        None => 0,
    };
    if !(1..=100).contains(&limit) || offset < 0 {
        return Err(bad_request(
            "limit must be 1–100 and offset must be non-negative.",
        ));
    }
    Ok((limit, offset))
}

async fn require_user(
    state: &AppState,
    headers: &HeaderMap,
) -> Result<crate::context::SessionUser, Response> {
    let Some(pool) = state.identity_pool.as_ref() else {
        return Err(unavailable());
    };
    let Some(secret_key) = state.config.django_secret_key.as_deref() else {
        return Err(unavailable());
    };
    match authenticate(pool, secret_key, headers).await {
        Ok(user) => Ok(user),
        Err(AuthError::Unauthenticated(message)) => {
            Err((StatusCode::UNAUTHORIZED, Json(json!({"detail": message}))).into_response())
        }
        Err(AuthError::Unavailable(message)) => {
            tracing::error!(%message, "rust audit api could not resolve the session");
            Err(unavailable())
        }
    }
}

fn bad_request(message: &str) -> Response {
    (StatusCode::BAD_REQUEST, Json(json!({"detail": message}))).into_response()
}

fn not_found(message: &str) -> Response {
    (StatusCode::NOT_FOUND, Json(json!({"detail": message}))).into_response()
}

fn unavailable() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({"detail": "The service is temporarily unavailable."})),
    )
        .into_response()
}

fn database_error(error: sqlx::Error) -> Response {
    tracing::error!(%error, "rust audit api database operation failed");
    unavailable()
}

fn database_error_message(message: String) -> Response {
    tracing::error!(%message, "rust audit api database operation failed");
    unavailable()
}
