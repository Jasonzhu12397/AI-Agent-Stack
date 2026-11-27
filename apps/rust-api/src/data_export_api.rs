#![allow(clippy::result_large_err)]

//! Native tenant data export jobs backed by `core_dataexportjob`.

use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use chrono::{Duration, SecondsFormat, Utc};
use serde_json::{json, Value};
use sqlx::{AnyPool, Row};

use crate::proxy::AppState;

fn error_json(status: StatusCode, code: &str, message: &str) -> Response {
    (
        status,
        Json(json!({"error": {"code": code, "message": message, "details": {}}})),
    )
        .into_response()
}

fn is_postgres(state: &AppState) -> bool {
    state
        .config
        .database_url
        .as_deref()
        .is_some_and(|url| url.starts_with("postgres"))
}

fn timestamp(state: &AppState, alias: &str, column: &str) -> String {
    if is_postgres(state) {
        format!("to_char({alias}.{column} AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')")
    } else {
        format!("strftime('%Y-%m-%dT%H:%M:%SZ', {alias}.{column})")
    }
}

async fn require_user(
    state: &AppState,
    headers: &HeaderMap,
) -> Result<crate::context::SessionUser, Response> {
    crate::billing_api::require_user(state, headers).await
}

fn pool(state: &AppState) -> Result<&AnyPool, Response> {
    state.identity_pool.as_ref().ok_or_else(|| {
        error_json(
            StatusCode::SERVICE_UNAVAILABLE,
            "UNAVAILABLE",
            "Database unavailable.",
        )
    })
}

async fn tenant_admin(pool: &AnyPool, tenant_id: &str, user_id: i64) -> Result<bool, Response> {
    let row = sqlx::query(
        "SELECT 1 FROM core_tenantmembership WHERE CAST(tenant_id AS TEXT) = ? \
         AND user_id = ? AND role IN ('owner', 'admin') LIMIT 1",
    )
    .bind(tenant_id)
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| {
        error_json(
            StatusCode::INTERNAL_SERVER_ERROR,
            "DATABASE_ERROR",
            &error.to_string(),
        )
    })?;
    Ok(row.is_some())
}

async fn tenant_exists(pool: &AnyPool, tenant_id: &str, user_id: i64) -> Result<bool, Response> {
    let row = sqlx::query(
        "SELECT 1 FROM core_tenant tenant JOIN core_tenantmembership membership \
         ON CAST(membership.tenant_id AS TEXT) = CAST(tenant.id AS TEXT) \
         WHERE CAST(tenant.id AS TEXT) = ? AND membership.user_id = ? LIMIT 1",
    )
    .bind(tenant_id)
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| {
        error_json(
            StatusCode::INTERNAL_SERVER_ERROR,
            "DATABASE_ERROR",
            &error.to_string(),
        )
    })?;
    Ok(row.is_some())
}

async fn project_exists(
    pool: &AnyPool,
    tenant_id: &str,
    project_id: &str,
) -> Result<bool, Response> {
    let row = sqlx::query(
        "SELECT 1 FROM core_project WHERE CAST(id AS TEXT) = ? \
         AND CAST(tenant_id AS TEXT) = ? LIMIT 1",
    )
    .bind(project_id)
    .bind(tenant_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| {
        error_json(
            StatusCode::INTERNAL_SERVER_ERROR,
            "DATABASE_ERROR",
            &error.to_string(),
        )
    })?;
    Ok(row.is_some())
}

async fn data_control_settings(pool: &AnyPool, tenant_id: &str, state: &AppState) -> (bool, i64) {
    let text = if is_postgres(state) {
        "CAST(setting.\"values\" AS TEXT)"
    } else {
        "setting.\"values\""
    };
    let sql = format!(
        "SELECT {text} AS values_text FROM core_tenantsetting setting \
         WHERE CAST(setting.tenant_id AS TEXT) = ? AND setting.section = 'data-controls' LIMIT 1"
    );
    let values: Value = sqlx::query(&sql)
        .bind(tenant_id)
        .fetch_optional(pool)
        .await
        .ok()
        .flatten()
        .and_then(|row| row.try_get::<String, _>("values_text").ok())
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_else(|| json!({}));
    let allow = values
        .get("allow_export")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let days = values
        .get("retention_days")
        .and_then(Value::as_i64)
        .unwrap_or(30);
    (allow, days)
}

fn job_payload(row: &sqlx::any::AnyRow, include_file_ref: bool) -> Value {
    let status: String = row.try_get("status").unwrap_or_default();
    let scope: Value = row
        .try_get::<String, _>("scope_text")
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_else(|| json!({}));
    let mut payload = json!({
        "id": row.try_get::<String, _>("id").unwrap_or_default(),
        "status": status,
        "scope": scope,
        "row_count": row.try_get::<i64, _>("row_count").unwrap_or(0),
        "expires_at": row.try_get::<Option<String>, _>("expires").ok().flatten(),
        "error_code": row.try_get::<String, _>("error_code").unwrap_or_default(),
        "created_at": row.try_get::<Option<String>, _>("created").ok().flatten(),
    });
    if include_file_ref && status == "succeeded" {
        payload["file_ref"] = json!(row.try_get::<String, _>("file_ref").unwrap_or_default());
    }
    payload
}

fn job_select(state: &AppState) -> String {
    let scope_text = if is_postgres(state) {
        "CAST(job.scope AS TEXT)"
    } else {
        "job.scope"
    };
    format!(
        "SELECT CAST(job.id AS TEXT) AS id, job.status, {scope_text} AS scope_text, \
         job.row_count, job.file_ref, job.error_code, {} AS expires, {} AS created \
         FROM core_dataexportjob job",
        timestamp(state, "job", "expires_at"),
        timestamp(state, "job", "created_at")
    )
}

async fn fetch_job(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: &str,
    job_id: &str,
) -> Result<Option<sqlx::any::AnyRow>, Response> {
    let sql = format!(
        "{} WHERE CAST(job.tenant_id AS TEXT) = ? AND CAST(job.id AS TEXT) = ? LIMIT 1",
        job_select(state)
    );
    sqlx::query(&sql)
        .bind(tenant_id)
        .bind(job_id)
        .fetch_optional(pool)
        .await
        .map_err(|error| {
            error_json(
                StatusCode::INTERNAL_SERVER_ERROR,
                "DATABASE_ERROR",
                &error.to_string(),
            )
        })
}

/// `GET|POST /api/v1/tenants/{tenant}/data-controls/exports/`
pub async fn list_or_create(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
    body: Option<Json<Value>>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let pool = match pool(&state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    if !tenant_exists(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return error_json(StatusCode::NOT_FOUND, "NOT_FOUND", "Not found.");
    }
    let body = body.map(|Json(value)| value).unwrap_or_else(|| json!({}));
    if !body.is_object() {
        return error_json(
            StatusCode::BAD_REQUEST,
            "EXPORT_SCOPE_INVALID",
            "scope must be a JSON object.",
        );
    }
    let is_create = body.get("idempotency_key").is_some()
        || body.get("scope").is_some()
        || !body.as_object().map(|map| map.is_empty()).unwrap_or(true);
    if !is_create {
        let sql = format!(
            "{} WHERE CAST(job.tenant_id AS TEXT) = ? ORDER BY job.created_at DESC",
            job_select(&state)
        );
        let rows = match sqlx::query(&sql).bind(&tenant_id).fetch_all(pool).await {
            Ok(rows) => rows,
            Err(error) => {
                return error_json(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "DATABASE_ERROR",
                    &error.to_string(),
                )
            }
        };
        return Json(json!({
            "results": rows.iter().map(|row| job_payload(row, false)).collect::<Vec<_>>()
        }))
        .into_response();
    }
    if !tenant_admin(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return error_json(
            StatusCode::FORBIDDEN,
            "FORBIDDEN",
            "Tenant admin permission is required.",
        );
    }
    let key = body
        .get("idempotency_key")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    if key.is_empty() || key.chars().count() > 128 {
        return error_json(
            StatusCode::BAD_REQUEST,
            "EXPORT_KEY_REQUIRED",
            "An idempotency_key is required.",
        );
    }
    let scope = body.get("scope").cloned().unwrap_or_else(|| json!({}));
    if !scope.is_object() {
        return error_json(
            StatusCode::BAD_REQUEST,
            "EXPORT_SCOPE_INVALID",
            "scope must be a JSON object.",
        );
    }
    let (allow, retention_days) = data_control_settings(pool, &tenant_id, &state).await;
    if !allow {
        return error_json(
            StatusCode::FORBIDDEN,
            "EXPORT_DISABLED",
            "Data export is disabled for this tenant.",
        );
    }
    let existing_sql = format!(
        "{} WHERE CAST(job.tenant_id AS TEXT) = ? AND job.idempotency_key = ? LIMIT 1",
        job_select(&state)
    );
    let existing = sqlx::query(&existing_sql)
        .bind(&tenant_id)
        .bind(&key)
        .fetch_optional(pool)
        .await;
    match existing {
        Ok(Some(row)) => return Json(job_payload(&row, true)).into_response(),
        Ok(None) => {}
        Err(error) => {
            return error_json(
                StatusCode::INTERNAL_SERVER_ERROR,
                "DATABASE_ERROR",
                &error.to_string(),
            )
        }
    }
    let project_id = scope
        .get("project_id")
        .and_then(Value::as_str)
        .map(str::to_string);
    if let Some(project_id) = &project_id {
        if !project_exists(pool, &tenant_id, project_id)
            .await
            .unwrap_or(false)
        {
            return error_json(StatusCode::NOT_FOUND, "NOT_FOUND", "Not found.");
        }
    }
    let kinds: Vec<String> = scope
        .get("kinds")
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(|item| item.as_str().map(str::to_string))
                .collect()
        })
        .unwrap_or_else(|| vec!["tasks".into(), "runs".into(), "audit_events".into()]);
    const ALLOWED: [&str; 3] = ["tasks", "runs", "audit_events"];
    let unknown: Vec<&String> = kinds
        .iter()
        .filter(|kind| !ALLOWED.contains(&kind.as_str()))
        .collect();
    if !unknown.is_empty() {
        return error_json(
            StatusCode::BAD_REQUEST,
            "EXPORT_SCOPE_INVALID",
            &format!(
                "Unsupported export kinds: {:?}",
                unknown.iter().map(|k| k.as_str()).collect::<Vec<_>>()
            ),
        );
    }
    let job_id = uuid::Uuid::new_v4().to_string();
    let now = Utc::now();
    let expires =
        (now + Duration::days(retention_days.max(1))).to_rfc3339_opts(SecondsFormat::Micros, false);
    let insert = sqlx::query(
        "INSERT INTO core_dataexportjob (id, tenant_id, project_id, requested_by_id, scope, status, \
         idempotency_key, row_count, file_ref, expires_at, error_code, created_at, updated_at) \
         VALUES (?, ?, ?, ?, ?, 'pending', ?, 0, '', ?, '', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
    )
    .bind(&job_id)
    .bind(&tenant_id)
    .bind(&project_id)
    .bind(user.id)
    .bind(scope.to_string())
    .bind(&key)
    .bind(&expires)
    .execute(pool)
    .await;
    if let Err(error) = insert {
        let message = error.to_string().to_lowercase();
        if message.contains("unique") || message.contains("constraint") {
            let retry_sql = format!(
                "{} WHERE CAST(job.tenant_id AS TEXT) = ? AND job.idempotency_key = ? LIMIT 1",
                job_select(&state)
            );
            if let Ok(Some(row)) = sqlx::query(&retry_sql)
                .bind(&tenant_id)
                .bind(&key)
                .fetch_optional(pool)
                .await
            {
                return Json(job_payload(&row, true)).into_response();
            }
        }
        return error_json(
            StatusCode::INTERNAL_SERVER_ERROR,
            "DATABASE_ERROR",
            &error.to_string(),
        );
    }
    match generate_export(
        pool,
        &state,
        &tenant_id,
        project_id.as_deref(),
        &job_id,
        &scope,
        &kinds,
    )
    .await
    {
        Ok((row_count, _file_ref)) => {
            let _ = crate::audit::record_event(
                pool,
                state.config.database_url.as_deref().unwrap_or_default(),
                crate::audit::AuditEvent {
                    tenant_id: Some(&tenant_id),
                    project_id: project_id.as_deref(),
                    event_type: "data.export.generated",
                    resource_type: "data_export_job",
                    resource_id: &job_id,
                    outcome: "success",
                    actor_id: Some(user.id),
                    request_id: headers
                        .get("x-request-id")
                        .and_then(|value| value.to_str().ok())
                        .unwrap_or_default(),
                    remote_addr: None,
                    user_agent: headers
                        .get("user-agent")
                        .and_then(|value| value.to_str().ok())
                        .unwrap_or_default(),
                    metadata: json!({"row_count": row_count, "kinds": kinds}),
                },
            )
            .await;
            let sql = format!(
                "{} WHERE CAST(job.tenant_id AS TEXT) = ? AND CAST(job.id AS TEXT) = ? LIMIT 1",
                job_select(&state)
            );
            match sqlx::query(&sql)
                .bind(&tenant_id)
                .bind(&job_id)
                .fetch_optional(pool)
                .await
            {
                Ok(Some(row)) => {
                    (StatusCode::CREATED, Json(job_payload(&row, true))).into_response()
                }
                _ => error_json(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    "EXPORT_GENERATION_FAILED",
                    "Export job could not be read after generation.",
                ),
            }
        }
        Err(message) => {
            let _ = sqlx::query(
                "UPDATE core_dataexportjob SET status = 'failed', error_code = 'EXPORT_GENERATION_FAILED', \
                 updated_at = CURRENT_TIMESTAMP WHERE CAST(id AS TEXT) = ?",
            )
            .bind(&job_id)
            .execute(pool)
            .await;
            error_json(
                StatusCode::INTERNAL_SERVER_ERROR,
                "EXPORT_GENERATION_FAILED",
                &message,
            )
        }
    }
}

/// `GET /api/v1/tenants/{tenant}/data-controls/exports/{job}/`
pub async fn detail(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, job_id)): Path<(String, String)>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let pool = match pool(&state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    if !tenant_exists(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return error_json(StatusCode::NOT_FOUND, "NOT_FOUND", "Not found.");
    }
    match fetch_job(pool, &state, &tenant_id, &job_id).await {
        Ok(Some(row)) => {
            if row.try_get::<String, _>("status").unwrap_or_default() != "succeeded" {
                return error_json(
                    StatusCode::CONFLICT,
                    "EXPORT_NOT_READY",
                    "Export content is not available until the job succeeds.",
                );
            }
            Json(job_payload(&row, true)).into_response()
        }
        Ok(None) => error_json(StatusCode::NOT_FOUND, "NOT_FOUND", "Not found."),
        Err(response) => response,
    }
}

/// `POST /api/v1/tenants/{tenant}/data-controls/exports/{job}/expire/`
pub async fn expire(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, job_id)): Path<(String, String)>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let pool = match pool(&state) {
        Ok(pool) => pool,
        Err(response) => return response,
    };
    if !tenant_exists(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return error_json(StatusCode::NOT_FOUND, "NOT_FOUND", "Not found.");
    }
    if !tenant_admin(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return error_json(
            StatusCode::FORBIDDEN,
            "FORBIDDEN",
            "Tenant admin permission is required.",
        );
    }
    let row = match fetch_job(pool, &state, &tenant_id, &job_id).await {
        Ok(Some(row)) => row,
        Ok(None) => return error_json(StatusCode::NOT_FOUND, "NOT_FOUND", "Not found."),
        Err(response) => return response,
    };
    if row.try_get::<String, _>("status").unwrap_or_default() != "expired" {
        if let Err(error) = sqlx::query(
            "UPDATE core_dataexportjob SET status = 'expired', updated_at = CURRENT_TIMESTAMP \
             WHERE CAST(id AS TEXT) = ?",
        )
        .bind(&job_id)
        .execute(pool)
        .await
        {
            return error_json(
                StatusCode::INTERNAL_SERVER_ERROR,
                "DATABASE_ERROR",
                &error.to_string(),
            );
        }
        let _ = crate::audit::record_event(
            pool,
            state.config.database_url.as_deref().unwrap_or_default(),
            crate::audit::AuditEvent {
                tenant_id: Some(&tenant_id),
                project_id: None,
                event_type: "data.export.expired",
                resource_type: "data_export_job",
                resource_id: &job_id,
                outcome: "success",
                actor_id: Some(user.id),
                request_id: headers
                    .get("x-request-id")
                    .and_then(|value| value.to_str().ok())
                    .unwrap_or_default(),
                remote_addr: None,
                user_agent: headers
                    .get("user-agent")
                    .and_then(|value| value.to_str().ok())
                    .unwrap_or_default(),
                metadata: json!({"row_count": row.try_get::<i64, _>("row_count").unwrap_or(0)}),
            },
        )
        .await;
    }
    match fetch_job(pool, &state, &tenant_id, &job_id).await {
        Ok(Some(row)) => Json(job_payload(&row, false)).into_response(),
        _ => error_json(StatusCode::NOT_FOUND, "NOT_FOUND", "Not found."),
    }
}

async fn generate_export(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: &str,
    project_id: Option<&str>,
    job_id: &str,
    scope: &Value,
    kinds: &[String],
) -> Result<(i64, String), String> {
    let mut payload = json!({
        "tenant_id": tenant_id,
        "generated_at": Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false),
    });
    let mut row_count = 0_i64;
    if kinds.iter().any(|kind| kind == "tasks") {
        let created = timestamp(state, "task", "created_at");
        let started = timestamp(state, "task", "started_at");
        let finished = timestamp(state, "task", "finished_at");
        let sql = format!(
            "SELECT CAST(task.id AS TEXT) AS id, CAST(task.project_id AS TEXT) AS project_id, \
             task.task_type, task.status, task.attempts, task.max_attempts, {created} AS created, \
             {started} AS started, {finished} AS finished FROM core_task task \
             WHERE CAST(task.tenant_id AS TEXT) = ? AND (? IS NULL OR CAST(task.project_id AS TEXT) = ?)"
        );
        let rows = sqlx::query(&sql)
            .bind(tenant_id)
            .bind(project_id)
            .bind(project_id)
            .fetch_all(pool)
            .await
            .map_err(|error| error.to_string())?;
        let tasks: Vec<Value> = rows
            .iter()
            .map(|row| {
                json!({
                    "id": row.try_get::<String, _>("id").unwrap_or_default(),
                    "project_id": row.try_get::<String, _>("project_id").unwrap_or_default(),
                    "task_type": row.try_get::<String, _>("task_type").unwrap_or_default(),
                    "status": row.try_get::<String, _>("status").unwrap_or_default(),
                    "attempts": row.try_get::<i64, _>("attempts").unwrap_or(0),
                    "max_attempts": row.try_get::<i64, _>("max_attempts").unwrap_or(0),
                    "created_at": row.try_get::<Option<String>, _>("created").ok().flatten(),
                    "started_at": row.try_get::<Option<String>, _>("started").ok().flatten(),
                    "finished_at": row.try_get::<Option<String>, _>("finished").ok().flatten(),
                })
            })
            .collect();
        row_count += tasks.len() as i64;
        payload["tasks"] = Value::Array(tasks);
    }
    if kinds.iter().any(|kind| kind == "runs") {
        let created = timestamp(state, "run", "created_at");
        let finished = timestamp(state, "run", "finished_at");
        let sql = format!(
            "SELECT CAST(run.id AS TEXT) AS id, CAST(run.task_id AS TEXT) AS task_id, \
             run.provider_name, run.model_name, run.input_tokens, run.output_tokens, run.latency_ms, \
             {created} AS created, {finished} AS finished FROM core_agentrun run \
             JOIN core_task task ON CAST(task.id AS TEXT) = CAST(run.task_id AS TEXT) \
             WHERE CAST(task.tenant_id AS TEXT) = ? AND (? IS NULL OR CAST(task.project_id AS TEXT) = ?)"
        );
        let rows = sqlx::query(&sql)
            .bind(tenant_id)
            .bind(project_id)
            .bind(project_id)
            .fetch_all(pool)
            .await
            .map_err(|error| error.to_string())?;
        let runs: Vec<Value> = rows
            .iter()
            .map(|row| {
                json!({
                    "id": row.try_get::<String, _>("id").unwrap_or_default(),
                    "task_id": row.try_get::<String, _>("task_id").unwrap_or_default(),
                    "provider_name": row.try_get::<String, _>("provider_name").unwrap_or_default(),
                    "model_name": row.try_get::<String, _>("model_name").unwrap_or_default(),
                    "input_tokens": row.try_get::<i64, _>("input_tokens").unwrap_or(0),
                    "output_tokens": row.try_get::<i64, _>("output_tokens").unwrap_or(0),
                    "latency_ms": row.try_get::<i64, _>("latency_ms").unwrap_or(0),
                    "created_at": row.try_get::<Option<String>, _>("created").ok().flatten(),
                    "finished_at": row.try_get::<Option<String>, _>("finished").ok().flatten(),
                })
            })
            .collect();
        row_count += runs.len() as i64;
        payload["runs"] = Value::Array(runs);
    }
    if kinds.iter().any(|kind| kind == "audit_events") {
        let created = timestamp(state, "event", "created_at");
        let metadata_text = if is_postgres(state) {
            "CAST(event.metadata AS TEXT)"
        } else {
            "event.metadata"
        };
        let sql = format!(
            "SELECT CAST(event.id AS TEXT) AS id, event.event_type, event.resource_type, \
             event.resource_id, event.outcome, {metadata_text} AS metadata_text, {created} AS created \
             FROM core_auditevent event WHERE CAST(event.tenant_id AS TEXT) = ?"
        );
        let rows = sqlx::query(&sql)
            .bind(tenant_id)
            .fetch_all(pool)
            .await
            .map_err(|error| error.to_string())?;
        let events: Vec<Value> = rows
            .iter()
            .map(|row| {
                let metadata: Value = row
                    .try_get::<String, _>("metadata_text")
                    .ok()
                    .and_then(|text| serde_json::from_str(&text).ok())
                    .unwrap_or_else(|| json!({}));
                json!({
                    "id": row.try_get::<String, _>("id").unwrap_or_default(),
                    "event_type": row.try_get::<String, _>("event_type").unwrap_or_default(),
                    "resource_type": row.try_get::<String, _>("resource_type").unwrap_or_default(),
                    "resource_id": row.try_get::<String, _>("resource_id").unwrap_or_default(),
                    "outcome": row.try_get::<String, _>("outcome").unwrap_or_default(),
                    "metadata": metadata,
                    "created_at": row.try_get::<Option<String>, _>("created").ok().flatten(),
                })
            })
            .collect();
        row_count += events.len() as i64;
        payload["audit_events"] = Value::Array(events);
    }
    let directory = std::path::Path::new(&state.config.media_root)
        .join("exports")
        .join(tenant_id);
    std::fs::create_dir_all(&directory).map_err(|error| error.to_string())?;
    let file_name = format!(
        "{}_{}.json",
        uuid::Uuid::new_v4().simple(),
        uuid::Uuid::new_v4().simple()
    );
    let file_path = directory.join(file_name);
    std::fs::write(
        &file_path,
        serde_json::to_string(&payload).map_err(|error| error.to_string())?,
    )
    .map_err(|error| error.to_string())?;
    let file_ref = file_path.to_string_lossy().to_string();
    sqlx::query(
        "UPDATE core_dataexportjob SET row_count = ?, file_ref = ?, status = 'succeeded', \
         error_code = '', scope = ?, updated_at = CURRENT_TIMESTAMP WHERE CAST(id AS TEXT) = ?",
    )
    .bind(row_count)
    .bind(&file_ref)
    .bind(scope.to_string())
    .bind(job_id)
    .execute(pool)
    .await
    .map_err(|error| error.to_string())?;
    Ok((row_count, file_ref))
}
