#![allow(clippy::result_large_err)]

use axum::body::Bytes;
use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use chrono::{SecondsFormat, Utc};
use serde::Deserialize;
use serde_json::{json, Value};
use sqlx::{AnyPool, Row};

use crate::context::{authenticate, AuthError};
use crate::proxy::AppState;
use crate::tenancy::{is_postgres, normalize_timestamp, project_access, tenant_role};

const SUPPORTED_TASK_TYPES: [&str; 2] = ["demo.echo", "demo.sleep"];
#[derive(Deserialize, Default)]
pub struct TaskQuery {
    limit: Option<String>,
    offset: Option<String>,
}

/// Native replacement for `/api/v1/tenants/{tenant}/projects/{project}/tasks/`,
/// `/api/v1/tasks/{task}/` and `/api/v1/tasks/{task}/cancel/`.
pub async fn list_tasks(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id)): Path<(String, String)>,
    Query(query): Query<TaskQuery>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    if !match project_access(pool, &tenant_id, &project_id, user.id).await {
        Ok(access) => access,
        Err(error) => return database_error_message(error),
    } {
        return not_found();
    }
    let limit = match query.limit.as_deref().map(str::parse::<i64>) {
        Some(Ok(value)) => value.clamp(1, 100),
        Some(Err(_)) => return bad_request("limit and offset must be integers."),
        None => 50,
    };
    let offset = match query.offset.as_deref().map(str::parse::<i64>) {
        Some(Ok(value)) => value.max(0),
        Some(Err(_)) => return bad_request("limit and offset must be integers."),
        None => 0,
    };
    let count = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_task WHERE CAST(tenant_id AS TEXT) = ? \
         AND CAST(project_id AS TEXT) = ?",
    )
    .bind(&tenant_id)
    .bind(&project_id)
    .fetch_one(pool)
    .await;
    let count = match count {
        Ok(count) => count,
        Err(error) => return database_error(error),
    };
    let rows = task_rows(
        pool,
        &state,
        Some(&tenant_id),
        Some(&project_id),
        offset,
        limit,
    )
    .await;
    match rows {
        Ok(items) => Json(json!({
            "count": count,
            "limit": limit,
            "offset": offset,
            "results": items,
        }))
        .into_response(),
        Err(error) => database_error_message(error),
    }
}

pub async fn create_task(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id)): Path<(String, String)>,
    body: Bytes,
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
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    };
    if !match project_access(pool, &tenant_id, &project_id, user.id).await {
        Ok(access) => access,
        Err(error) => return database_error_message(error),
    } {
        return not_found();
    }
    let editor = role == "owner"
        || role == "admin"
        || project_editor(pool, &project_id, user.id)
            .await
            .unwrap_or(false);
    if !editor {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"detail": "Project editor permission required."})),
        )
            .into_response();
    }
    let idempotency_key = headers
        .get("idempotency-key")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .trim()
        .to_string();
    if idempotency_key.is_empty() || idempotency_key.chars().count() > 128 {
        return bad_request("A valid Idempotency-Key header is required.");
    }
    let payload = match serde_json::from_slice::<Value>(&body) {
        Ok(payload) if payload.is_object() => payload,
        _ => return bad_request("A valid JSON body is required."),
    };
    let task_type = match payload.get("task_type").and_then(Value::as_str) {
        Some(task_type) if SUPPORTED_TASK_TYPES.contains(&task_type) => task_type.to_string(),
        _ => return bad_request("Unsupported task_type."),
    };
    let task_payload = payload.get("payload").cloned().unwrap_or_else(|| json!({}));
    if !task_payload.is_object() {
        return bad_request("payload must be a JSON object no larger than 64 KiB.");
    }
    let payload_text = task_payload.to_string();
    if payload_text.len() > 65536 {
        return bad_request("payload must be a JSON object no larger than 64 KiB.");
    }

    if let Ok(Some(existing)) = find_task_by_key(pool, &state, &tenant_id, &idempotency_key).await {
        if existing.project_id != project_id
            || existing.task_type != task_type
            || existing.payload != task_payload
        {
            return (
                StatusCode::CONFLICT,
                Json(
                    json!({"detail": "Idempotency-Key was already used with a different request."}),
                ),
            )
                .into_response();
        }
        if let Err(error) = audit_task_event(
            pool,
            &state,
            &tenant_id,
            &project_id,
            &existing.id,
            "task.created",
            user.id,
            &headers,
            json!({"task_type": task_type, "deduplicated": true}),
        )
        .await
        {
            return database_error_message(error);
        }
        return (StatusCode::OK, Json(existing.to_json())).into_response();
    }

    let task_id = uuid::Uuid::new_v4().to_string();
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    let postgres = is_postgres(&state);
    let insert = if postgres {
        "INSERT INTO core_task (\
         id, tenant_id, project_id, created_by_id, task_type, payload, result, error, status, \
         idempotency_key, attempts, max_attempts, available_at, lease_owner, lease_token, \
         lease_expires_at, heartbeat_at, cancel_requested_at, started_at, finished_at, \
         created_at, updated_at) \
         VALUES (?::uuid, ?::uuid, ?::uuid, ?, ?, ?::jsonb, NULL, NULL, 'queued', ?, 0, 3, \
         ?::timestamptz, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?::timestamptz, ?::timestamptz)"
    } else {
        "INSERT INTO core_task (\
         id, tenant_id, project_id, created_by_id, task_type, payload, result, error, status, \
         idempotency_key, attempts, max_attempts, available_at, lease_owner, lease_token, \
         lease_expires_at, heartbeat_at, cancel_requested_at, started_at, finished_at, \
         created_at, updated_at) \
         VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, 'queued', ?, 0, 3, ?, NULL, NULL, NULL, NULL, \
         NULL, NULL, NULL, ?, ?)"
    };
    let insert_result = sqlx::query(insert)
        .bind(&task_id)
        .bind(&tenant_id)
        .bind(&project_id)
        .bind(user.id)
        .bind(&task_type)
        .bind(&payload_text)
        .bind(&idempotency_key)
        .bind(&now)
        .bind(&now)
        .bind(&now)
        .execute(pool)
        .await;
    if let Err(error) = insert_result {
        if let Ok(Some(existing)) =
            find_task_by_key(pool, &state, &tenant_id, &idempotency_key).await
        {
            if existing.project_id != project_id
                || existing.task_type != task_type
                || existing.payload != task_payload
            {
                return (
                    StatusCode::CONFLICT,
                    Json(json!({"detail": "Idempotency-Key was already used with a different request."})),
                )
                    .into_response();
            }
            return (StatusCode::OK, Json(existing.to_json())).into_response();
        }
        return database_error(error);
    }
    if let Err(error) = audit_task_event(
        pool,
        &state,
        &tenant_id,
        &project_id,
        &task_id,
        "task.created",
        user.id,
        &headers,
        json!({"task_type": task_type, "deduplicated": false}),
    )
    .await
    {
        return database_error_message(error);
    }
    match fetch_task(pool, &state, &task_id).await {
        Ok(Some(task)) => (StatusCode::ACCEPTED, Json(task.to_json())).into_response(),
        Ok(None) => not_found(),
        Err(error) => database_error_message(error),
    }
}

pub async fn get_task(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(task_id): Path<String>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    match task_access(pool, &state, &task_id, user.id).await {
        Ok(Some(task)) => Json(task.to_json()).into_response(),
        Ok(None) => not_found(),
        Err(error) => database_error_message(error),
    }
}

pub async fn cancel_task(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(task_id): Path<String>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let task = match task_access(pool, &state, &task_id, user.id).await {
        Ok(Some(task)) => task,
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    };
    let postgres = is_postgres(&state);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return database_error(error),
    };
    if postgres {
        if let Err(error) = sqlx::query("SELECT id FROM core_task WHERE id = $1::uuid FOR UPDATE")
            .bind(&task_id)
            .execute(&mut *transaction)
            .await
        {
            return database_error(error);
        }
    }
    let current =
        sqlx::query_as::<_, (String,)>("SELECT status FROM core_task WHERE CAST(id AS TEXT) = ?")
            .bind(&task_id)
            .fetch_optional(&mut *transaction)
            .await;
    let previous_status = match current {
        Ok(Some((status,))) => status,
        Ok(None) => return not_found(),
        Err(error) => return database_error(error),
    };
    let next_status = match previous_status.as_str() {
        "queued" | "waiting_approval" => "cancelled",
        "running" => "cancel_requested",
        "cancel_requested" => {
            if let Ok(Some(task)) = fetch_task(pool, &state, &task_id).await {
                return (StatusCode::ACCEPTED, Json(task.to_json())).into_response();
            }
            return not_found();
        }
        _ => {
            return (
                StatusCode::CONFLICT,
                Json(json!({"detail": "Task is already in a terminal or approval state."})),
            )
                .into_response()
        }
    };
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    let finish = next_status == "cancelled";
    let sql = if finish {
        "UPDATE core_task SET status = ?, finished_at = ?, updated_at = ? WHERE CAST(id AS TEXT) = ?"
    } else {
        "UPDATE core_task SET status = ?, cancel_requested_at = ?, updated_at = ? WHERE CAST(id AS TEXT) = ?"
    };
    if let Err(error) = sqlx::query(sql)
        .bind(next_status)
        .bind(&now)
        .bind(&now)
        .bind(&task_id)
        .execute(&mut *transaction)
        .await
    {
        return database_error(error);
    }
    if finish && (task.task_type == "agent.run" || task.task_type == "workflow.run") {
        let table = if task.task_type == "agent.run" {
            "core_agentrun"
        } else {
            "core_workflowrun"
        };
        let update = format!("UPDATE {table} SET finished_at = ? WHERE CAST(task_id AS TEXT) = ?");
        let _ = sqlx::query(&update)
            .bind(&now)
            .bind(&task_id)
            .execute(&mut *transaction)
            .await;
    }
    let metadata = json!({
        "task_type": task.task_type,
        "previous_status": previous_status,
        "status": next_status,
    });
    if let Err(error) = crate::audit::record_event_in_transaction(
        &mut transaction,
        postgres,
        &crate::audit::AuditEvent {
            tenant_id: Some(&task.tenant_id),
            project_id: Some(&task.project_id),
            event_type: "task.cancelled",
            resource_type: "task",
            resource_id: &task.id,
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
        return database_error_message(error);
    }
    if let Err(error) = transaction.commit().await {
        return database_error(error);
    }
    match fetch_task(pool, &state, &task_id).await {
        Ok(Some(task)) => (StatusCode::ACCEPTED, Json(task.to_json())).into_response(),
        Ok(None) => not_found(),
        Err(error) => database_error_message(error),
    }
}

#[derive(Clone)]
struct TaskRecord {
    id: String,
    tenant_id: String,
    project_id: String,
    created_by_id: i64,
    task_type: String,
    payload: Value,
    result: Option<String>,
    error: Option<String>,
    status: String,
    attempts: i64,
    max_attempts: i64,
    available_at: Option<String>,
    heartbeat_at: Option<String>,
    started_at: Option<String>,
    finished_at: Option<String>,
    created_at: Option<String>,
    updated_at: Option<String>,
}

impl TaskRecord {
    fn to_json(&self) -> Value {
        json!({
            "id": self.id,
            "tenant_id": self.tenant_id,
            "project_id": self.project_id,
            "created_by_id": self.created_by_id,
            "task_type": self.task_type,
            "payload": self.payload,
            "result": parse_json_field(self.result.as_deref()),
            "error": parse_json_field(self.error.as_deref()),
            "status": self.status,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "available_at": self.available_at.as_deref().map(normalize_timestamp),
            "heartbeat_at": self.heartbeat_at.as_deref().map(normalize_timestamp),
            "started_at": self.started_at.as_deref().map(normalize_timestamp),
            "finished_at": self.finished_at.as_deref().map(normalize_timestamp),
            "created_at": self.created_at.as_deref().map(normalize_timestamp),
            "updated_at": self.updated_at.as_deref().map(normalize_timestamp),
        })
    }
}

fn parse_json_field(value: Option<&str>) -> Value {
    value
        .and_then(|text| serde_json::from_str(text).ok())
        .unwrap_or(Value::Null)
}

async fn task_rows(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: Option<&str>,
    project_id: Option<&str>,
    offset: i64,
    limit: i64,
) -> Result<Vec<Value>, String> {
    let postgres = is_postgres(state);
    let json_cast = if postgres { "::text" } else { "" };
    let sql = format!(
        "SELECT CAST(id AS TEXT) AS id, CAST(tenant_id AS TEXT) AS tenant_id, \
         CAST(project_id AS TEXT) AS project_id, created_by_id, task_type, \
         payload{json_cast} AS payload_text, result{json_cast} AS result_text, \
         error{json_cast} AS error_text, status, attempts, max_attempts, \
         {a}, {h}, {s}, {f}, {c}, {u} \
         FROM core_task WHERE CAST(tenant_id AS TEXT) = ? AND CAST(project_id AS TEXT) = ? \
         ORDER BY created_at DESC LIMIT ? OFFSET ?",
        a = crate::tenancy::timestamp_field_expr(postgres, "core_task", "available_at"),
        h = crate::tenancy::timestamp_field_expr(postgres, "core_task", "heartbeat_at"),
        s = crate::tenancy::timestamp_field_expr(postgres, "core_task", "started_at"),
        f = crate::tenancy::timestamp_field_expr(postgres, "core_task", "finished_at"),
        c = crate::tenancy::timestamp_expr(postgres, "core_task"),
        u = crate::tenancy::timestamp_field_expr(postgres, "core_task", "updated_at"),
    );
    let rows = sqlx::query(&sql)
        .bind(tenant_id.unwrap_or_default())
        .bind(project_id.unwrap_or_default())
        .bind(limit)
        .bind(offset)
        .fetch_all(pool)
        .await
        .map_err(|error| error.to_string())?;
    rows.iter()
        .map(|row| TaskRecord::from_row(row).map(|task| task.to_json()))
        .collect()
}

async fn fetch_task(
    pool: &AnyPool,
    state: &AppState,
    task_id: &str,
) -> Result<Option<TaskRecord>, String> {
    let postgres = is_postgres(state);
    let json_cast = if postgres { "::text" } else { "" };
    let sql = format!(
        "SELECT CAST(id AS TEXT) AS id, CAST(tenant_id AS TEXT) AS tenant_id, \
         CAST(project_id AS TEXT) AS project_id, created_by_id, task_type, \
         payload{json_cast} AS payload_text, result{json_cast} AS result_text, \
         error{json_cast} AS error_text, status, attempts, max_attempts, \
         {a}, {h}, {s}, {f}, {c}, {u} \
         FROM core_task WHERE CAST(id AS TEXT) = ?",
        a = crate::tenancy::timestamp_field_expr(postgres, "core_task", "available_at"),
        h = crate::tenancy::timestamp_field_expr(postgres, "core_task", "heartbeat_at"),
        s = crate::tenancy::timestamp_field_expr(postgres, "core_task", "started_at"),
        f = crate::tenancy::timestamp_field_expr(postgres, "core_task", "finished_at"),
        c = crate::tenancy::timestamp_expr(postgres, "core_task"),
        u = crate::tenancy::timestamp_field_expr(postgres, "core_task", "updated_at"),
    );
    let row = sqlx::query(&sql)
        .bind(task_id)
        .fetch_optional(pool)
        .await
        .map_err(|error| error.to_string())?;
    row.as_ref().map(TaskRecord::from_row).transpose()
}

impl TaskRecord {
    fn from_row(row: &sqlx::any::AnyRow) -> Result<Self, String> {
        Ok(Self {
            id: row.try_get("id").map_err(|error| error.to_string())?,
            tenant_id: row
                .try_get("tenant_id")
                .map_err(|error| error.to_string())?,
            project_id: row
                .try_get("project_id")
                .map_err(|error| error.to_string())?,
            created_by_id: row
                .try_get("created_by_id")
                .map_err(|error| error.to_string())?,
            task_type: row
                .try_get("task_type")
                .map_err(|error| error.to_string())?,
            payload: parse_json_field(Some(
                row.try_get::<String, _>("payload_text")
                    .map_err(|error| error.to_string())?
                    .as_str(),
            )),
            result: row.try_get("result_text").ok(),
            error: row.try_get("error_text").ok(),
            status: row.try_get("status").map_err(|error| error.to_string())?,
            attempts: row.try_get("attempts").map_err(|error| error.to_string())?,
            max_attempts: row
                .try_get("max_attempts")
                .map_err(|error| error.to_string())?,
            available_at: row.try_get("available_at").ok(),
            heartbeat_at: row.try_get("heartbeat_at").ok(),
            started_at: row.try_get("started_at").ok(),
            finished_at: row.try_get("finished_at").ok(),
            created_at: row.try_get("created_at").ok(),
            updated_at: row.try_get("updated_at").ok(),
        })
    }
}

async fn find_task_by_key(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: &str,
    key: &str,
) -> Result<Option<TaskRecord>, String> {
    let postgres = is_postgres(state);
    let json_cast = if postgres { "::text" } else { "" };
    let sql = format!(
        "SELECT CAST(id AS TEXT) AS id, CAST(tenant_id AS TEXT) AS tenant_id, \
         CAST(project_id AS TEXT) AS project_id, created_by_id, task_type, \
         payload{json_cast} AS payload_text, result{json_cast} AS result_text, \
         error{json_cast} AS error_text, status, attempts, max_attempts, \
         {a}, {h}, {s}, {f}, {c}, {u} \
         FROM core_task WHERE CAST(tenant_id AS TEXT) = ? AND idempotency_key = ?",
        a = crate::tenancy::timestamp_field_expr(postgres, "core_task", "available_at"),
        h = crate::tenancy::timestamp_field_expr(postgres, "core_task", "heartbeat_at"),
        s = crate::tenancy::timestamp_field_expr(postgres, "core_task", "started_at"),
        f = crate::tenancy::timestamp_field_expr(postgres, "core_task", "finished_at"),
        c = crate::tenancy::timestamp_expr(postgres, "core_task"),
        u = crate::tenancy::timestamp_field_expr(postgres, "core_task", "updated_at"),
    );
    let row = sqlx::query(&sql)
        .bind(tenant_id)
        .bind(key)
        .fetch_optional(pool)
        .await
        .map_err(|error| error.to_string())?;
    row.as_ref().map(TaskRecord::from_row).transpose()
}

async fn task_access(
    pool: &AnyPool,
    state: &AppState,
    task_id: &str,
    user_id: i64,
) -> Result<Option<TaskRecord>, String> {
    let Some(task) = fetch_task(pool, state, task_id).await? else {
        return Ok(None);
    };
    let role = tenant_role(pool, &task.tenant_id, user_id).await?;
    if role.is_none() {
        return Ok(None);
    }
    if role.as_deref() == Some("owner") || role.as_deref() == Some("admin") {
        return Ok(Some(task));
    }
    if project_access(pool, &task.tenant_id, &task.project_id, user_id).await? {
        return Ok(Some(task));
    }
    Ok(None)
}

async fn project_editor(pool: &AnyPool, project_id: &str, user_id: i64) -> Result<bool, String> {
    let role = sqlx::query_scalar::<_, String>(
        "SELECT role FROM core_projectmembership \
         WHERE CAST(project_id AS TEXT) = ? AND user_id = ?",
    )
    .bind(project_id)
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())?;
    Ok(matches!(role.as_deref(), Some("admin") | Some("editor")))
}

#[allow(clippy::too_many_arguments)]
async fn audit_task_event(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: &str,
    project_id: &str,
    task_id: &str,
    event_type: &str,
    actor_id: i64,
    headers: &HeaderMap,
    metadata: Value,
) -> Result<(), String> {
    let postgres = is_postgres(state);
    let mut transaction = pool.begin().await.map_err(|error| error.to_string())?;
    crate::audit::record_event_in_transaction(
        &mut transaction,
        postgres,
        &crate::audit::AuditEvent {
            tenant_id: Some(tenant_id),
            project_id: Some(project_id),
            event_type,
            resource_type: "task",
            resource_id: task_id,
            outcome: "success",
            actor_id: Some(actor_id),
            request_id: headers
                .get("x-request-id")
                .and_then(|value| value.to_str().ok())
                .unwrap_or(""),
            remote_addr: None,
            user_agent: headers
                .get("user-agent")
                .and_then(|value| value.to_str().ok())
                .unwrap_or(""),
            metadata,
        },
    )
    .await?;
    transaction
        .commit()
        .await
        .map_err(|error| error.to_string())
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
            tracing::error!(%message, "rust task api could not resolve the session");
            Err(unavailable())
        }
    }
}

fn bad_request(message: &str) -> Response {
    (StatusCode::BAD_REQUEST, Json(json!({"detail": message}))).into_response()
}

fn not_found() -> Response {
    (
        StatusCode::NOT_FOUND,
        Json(json!({"detail": "No Task matches the given query."})),
    )
        .into_response()
}

fn unavailable() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({"detail": "The service is temporarily unavailable."})),
    )
        .into_response()
}

fn database_error(error: sqlx::Error) -> Response {
    tracing::error!(%error, "rust task api database operation failed");
    unavailable()
}

fn database_error_message(message: String) -> Response {
    tracing::error!(%message, "rust task api database operation failed");
    unavailable()
}
