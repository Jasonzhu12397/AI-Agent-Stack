#![allow(clippy::result_large_err)]

use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use chrono::{Datelike, Timelike, Utc};
use serde_json::{json, Value};
use sqlx::{AnyPool, Row};

use crate::context::{authenticate, AuthError};
use crate::proxy::AppState;
use crate::tenancy::{is_postgres, normalize_timestamp};

pub async fn list_jobs(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id)): Path<(String, String)>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    match crate::tenancy::project_access(pool, &tenant_id, &project_id, user.id).await {
        Ok(true) => {}
        Ok(false) => return not_found(),
        Err(error) => return database_error(error),
    }
    match jobs(pool, &state, &tenant_id, &project_id).await {
        Ok(items) => Json(json!({"items": items})).into_response(),
        Err(error) => database_error(error),
    }
}

pub async fn create_job(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id)): Path<(String, String)>,
    Json(payload): Json<Value>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    match crate::tenancy::project_access(pool, &tenant_id, &project_id, user.id).await {
        Ok(true) => {}
        Ok(false) => return not_found(),
        Err(error) => return database_error(error),
    }
    let name = payload
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    let prompt = payload
        .get("prompt")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    if name.is_empty() || prompt.is_empty() {
        return invalid_job("name and prompt are required.");
    }
    if prompt.chars().count() > 4000 {
        return invalid_job("prompt must be at most 4000 characters.");
    }
    let cadence = payload
        .get("cadence")
        .and_then(Value::as_str)
        .unwrap_or("daily")
        .to_string();
    if !["hourly", "daily", "weekly"].contains(&cadence.as_str()) {
        return invalid_job("Unknown cadence.");
    }
    let hour = match integer(payload.get("hour"), 9) {
        Ok(value) if (0..=23).contains(&value) => value,
        _ => return invalid_job("hour must be 0-23 and weekday 0-6."),
    };
    let weekday = match integer(payload.get("weekday"), 0) {
        Ok(value) if (0..=6).contains(&value) => value,
        _ => return invalid_job("hour must be 0-23 and weekday 0-6."),
    };
    let target = payload
        .get("execution_target")
        .and_then(Value::as_str)
        .unwrap_or("cloud")
        .to_string();
    if !["cloud", "local"].contains(&target.as_str()) {
        return invalid_job("execution_target must be cloud or local.");
    }
    let effort = payload
        .get("effort")
        .and_then(Value::as_str)
        .unwrap_or("auto")
        .to_string();
    if !["auto", "low"].contains(&effort.as_str()) {
        return invalid_job("effort must be auto or low.");
    }
    let model_id = payload
        .get("model_config_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string);
    if let Some(model_id) = model_id.as_deref() {
        match visible_model(pool, &tenant_id, user.id, model_id).await {
            Ok(true) => {}
            Ok(false) => return invalid_job("Unknown model configuration."),
            Err(error) => return database_error(error),
        }
    }
    let id = uuid::Uuid::new_v4().to_string();
    let now = Utc::now();
    let next = next_occurrence(&cadence, hour as u32, weekday as u32, now);
    let sql = "INSERT INTO core_scheduledchatjob (id, tenant_id, project_id, created_by_id, \
               name, prompt, cadence, hour, weekday, execution_target, effort, status, \
               next_run_at, last_run_at, last_error_code, created_at, updated_at, \
               model_config_id, thread_id) \
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, NULL, '', ?, ?, ?, NULL)";
    if let Err(error) = sqlx::query(sql)
        .bind(&id)
        .bind(&tenant_id)
        .bind(&project_id)
        .bind(user.id)
        .bind(name.chars().take(160).collect::<String>())
        .bind(&prompt)
        .bind(&cadence)
        .bind(hour)
        .bind(weekday)
        .bind(&target)
        .bind(&effort)
        .bind(format_time(&next))
        .bind(format_time(&now))
        .bind(format_time(&now))
        .bind(model_id)
        .execute(pool)
        .await
    {
        return database_error(error.to_string());
    }
    match job(pool, &state, &tenant_id, &project_id, &id).await {
        Ok(Some(item)) => (StatusCode::CREATED, Json(item)).into_response(),
        Ok(None) => not_found(),
        Err(error) => database_error(error),
    }
}

pub async fn update_job(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id, job_id)): Path<(String, String, String)>,
    Json(payload): Json<Value>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    match crate::tenancy::project_access(pool, &tenant_id, &project_id, user.id).await {
        Ok(true) => {}
        Ok(false) => return not_found(),
        Err(error) => return database_error(error),
    }
    let current = match job(pool, &state, &tenant_id, &project_id, &job_id).await {
        Ok(Some(item)) => item,
        Ok(None) => return not_found(),
        Err(error) => return database_error(error),
    };
    let mut name = current["name"].as_str().unwrap_or("").to_string();
    let mut prompt = current["prompt"].as_str().unwrap_or("").to_string();
    let mut cadence = current["cadence"].as_str().unwrap_or("daily").to_string();
    let mut hour = current["hour"].as_i64().unwrap_or(9);
    let mut weekday = current["weekday"].as_i64().unwrap_or(0);
    let mut target = current["execution_target"]
        .as_str()
        .unwrap_or("cloud")
        .to_string();
    let mut effort = current["effort"].as_str().unwrap_or("auto").to_string();
    let mut status = current["status"].as_str().unwrap_or("active").to_string();
    let mut reschedule = false;
    if let Some(value) = payload.get("name").and_then(Value::as_str) {
        if !value.trim().is_empty() {
            name = value.trim().chars().take(160).collect();
        }
    }
    if payload.get("prompt").is_some() {
        let value = payload
            .get("prompt")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .to_string();
        if value.is_empty() || value.chars().count() > 4000 {
            return invalid_job("prompt is invalid.");
        }
        prompt = value;
    }
    if payload.get("cadence").is_some() {
        let value = payload
            .get("cadence")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        if !["hourly", "daily", "weekly"].contains(&value.as_str()) {
            return invalid_job("Unknown cadence.");
        }
        cadence = value;
        reschedule = true;
    }
    if payload.get("hour").is_some() {
        match integer(payload.get("hour"), 0) {
            Ok(value) if (0..=23).contains(&value) => {
                hour = value;
                reschedule = true;
            }
            _ => return invalid_job("hour must be 0-23."),
        }
    }
    if payload.get("weekday").is_some() {
        match integer(payload.get("weekday"), 0) {
            Ok(value) if (0..=6).contains(&value) => {
                weekday = value;
                reschedule = true;
            }
            _ => return invalid_job("weekday must be 0-6."),
        }
    }
    if payload.get("enabled").is_some() {
        status = if payload
            .get("enabled")
            .and_then(Value::as_bool)
            .unwrap_or(false)
        {
            "active".to_string()
        } else {
            "paused".to_string()
        };
        reschedule = true;
    }
    if payload.get("effort").is_some() {
        let value = payload
            .get("effort")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        if !["auto", "low"].contains(&value.as_str()) {
            return invalid_job("effort must be auto or low.");
        }
        effort = value;
    }
    if payload.get("execution_target").is_some() {
        let value = payload
            .get("execution_target")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        if !["cloud", "local"].contains(&value.as_str()) {
            return invalid_job("execution_target must be cloud or local.");
        }
        target = value;
    }
    let now = Utc::now();
    let next = if reschedule {
        format_time(&next_occurrence(&cadence, hour as u32, weekday as u32, now))
    } else {
        current["next_run_at"].as_str().unwrap_or("").to_string()
    };
    let sql = "UPDATE core_scheduledchatjob SET name = ?, prompt = ?, cadence = ?, hour = ?, \
               weekday = ?, execution_target = ?, effort = ?, status = ?, next_run_at = ?, \
               updated_at = ? WHERE CAST(id AS TEXT) = ?";
    if let Err(error) = sqlx::query(sql)
        .bind(&name)
        .bind(&prompt)
        .bind(&cadence)
        .bind(hour)
        .bind(weekday)
        .bind(&target)
        .bind(&effort)
        .bind(&status)
        .bind(&next)
        .bind(format_time(&now))
        .bind(&job_id)
        .execute(pool)
        .await
    {
        return database_error(error.to_string());
    }
    match job(pool, &state, &tenant_id, &project_id, &job_id).await {
        Ok(Some(item)) => Json(item).into_response(),
        Ok(None) => not_found(),
        Err(error) => database_error(error),
    }
}

pub async fn delete_job(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id, job_id)): Path<(String, String, String)>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    match crate::tenancy::project_access(pool, &tenant_id, &project_id, user.id).await {
        Ok(true) => {}
        Ok(false) => return not_found(),
        Err(error) => return database_error(error),
    }
    let deleted = match sqlx::query(
        "DELETE FROM core_scheduledchatjob WHERE CAST(id AS TEXT) = ? \
         AND CAST(tenant_id AS TEXT) = ? AND CAST(project_id AS TEXT) = ?",
    )
    .bind(&job_id)
    .bind(&tenant_id)
    .bind(&project_id)
    .execute(pool)
    .await
    {
        Ok(result) => result.rows_affected(),
        Err(error) => return database_error(error.to_string()),
    };
    if deleted == 0 {
        return not_found();
    }
    StatusCode::NO_CONTENT.into_response()
}

async fn jobs(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: &str,
    project_id: &str,
) -> Result<Vec<Value>, String> {
    let sql = job_sql(state);
    let rows = sqlx::query(&sql)
        .bind(tenant_id)
        .bind(project_id)
        .fetch_all(pool)
        .await
        .map_err(|error| error.to_string())?;
    Ok(rows.iter().map(job_json).collect())
}

async fn job(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: &str,
    project_id: &str,
    job_id: &str,
) -> Result<Option<Value>, String> {
    let sql = format!(
        "{} AND CAST(job.id AS TEXT) = ? ORDER BY job.next_run_at, job.created_at LIMIT 1",
        job_sql(state)
    );
    let row = sqlx::query(&sql)
        .bind(tenant_id)
        .bind(project_id)
        .bind(job_id)
        .fetch_optional(pool)
        .await
        .map_err(|error| error.to_string())?;
    Ok(row.as_ref().map(job_json))
}

fn job_sql(state: &AppState) -> String {
    let next = timestamp(state, "job", "next_run_at");
    let last = timestamp(state, "job", "last_run_at");
    let created = timestamp(state, "job", "created_at");
    let updated = timestamp(state, "job", "updated_at");
    format!(
        "SELECT CAST(job.id AS TEXT) AS id, job.name, job.prompt, job.cadence, job.hour, \
         job.weekday, job.execution_target, job.effort, job.status, \
         CAST(job.thread_id AS TEXT) AS thread_id, CAST(job.model_config_id AS TEXT) AS model_config_id, \
         {next} AS next_run_at, {last} AS last_run_at, job.last_error_code, {created} AS created_at, \
         {updated} AS updated_at FROM core_scheduledchatjob job \
         WHERE CAST(job.tenant_id AS TEXT) = ? AND CAST(job.project_id AS TEXT) = ?"
    )
}

fn job_json(row: &sqlx::any::AnyRow) -> Value {
    let next: Option<String> = row.try_get("next_run_at").ok().flatten();
    let last: Option<String> = row.try_get("last_run_at").ok().flatten();
    json!({
        "id": row.try_get::<String, _>("id").unwrap_or_default(),
        "name": row.try_get::<String, _>("name").unwrap_or_default(),
        "prompt": row.try_get::<String, _>("prompt").unwrap_or_default(),
        "cadence": row.try_get::<String, _>("cadence").unwrap_or_default(),
        "hour": row.try_get::<i64, _>("hour").unwrap_or(0),
        "weekday": row.try_get::<i64, _>("weekday").unwrap_or(0),
        "execution_target": row.try_get::<String, _>("execution_target").unwrap_or_default(),
        "effort": row.try_get::<String, _>("effort").unwrap_or_default(),
        "status": row.try_get::<String, _>("status").unwrap_or_default(),
        "enabled": row.try_get::<String, _>("status").unwrap_or_default() == "active",
        "thread_id": row.try_get::<Option<String>, _>("thread_id").ok().flatten(),
        "model_config_id": row.try_get::<Option<String>, _>("model_config_id").ok().flatten(),
        "next_run_at": next.as_deref().map(normalize_timestamp),
        "last_run_at": last.as_deref().map(normalize_timestamp),
        "last_error_code": row.try_get::<String, _>("last_error_code").unwrap_or_default(),
        "created_at": row.try_get::<Option<String>, _>("created_at").ok().flatten().as_deref().map(normalize_timestamp),
        "updated_at": row.try_get::<Option<String>, _>("updated_at").ok().flatten().as_deref().map(normalize_timestamp),
    })
}

fn next_occurrence(
    cadence: &str,
    hour: u32,
    weekday: u32,
    after: chrono::DateTime<Utc>,
) -> chrono::DateTime<Utc> {
    let mut candidate = after
        .with_minute(0)
        .and_then(|value| value.with_second(0))
        .and_then(|value| value.with_nanosecond(0))
        .unwrap_or(after);
    if cadence == "hourly" {
        candidate += chrono::Duration::hours(1);
        if candidate <= after {
            candidate += chrono::Duration::hours(1);
        }
        return candidate;
    }
    candidate = candidate
        .with_hour(hour)
        .and_then(|value| value.with_minute(0))
        .and_then(|value| value.with_second(0))
        .and_then(|value| value.with_nanosecond(0))
        .unwrap_or(after);
    if candidate <= after {
        candidate += chrono::Duration::days(1);
    }
    if cadence == "weekly" {
        while candidate.weekday().num_days_from_monday() != weekday {
            candidate += chrono::Duration::days(1);
        }
    }
    candidate
}

fn integer(value: Option<&Value>, default: i64) -> Result<i64, ()> {
    match value {
        None => Ok(default),
        Some(Value::Number(number)) => number.as_i64().ok_or(()),
        Some(Value::Null) => Ok(default),
        Some(Value::String(text)) => text.parse::<i64>().map_err(|_| ()),
        Some(_) => Err(()),
    }
}

fn format_time(value: &chrono::DateTime<Utc>) -> String {
    value.format("%Y-%m-%d %H:%M:%S").to_string()
}

async fn visible_model(
    pool: &AnyPool,
    tenant_id: &str,
    user_id: i64,
    model_id: &str,
) -> Result<bool, String> {
    let count = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_modelconfig config JOIN core_modelprovider provider \
         ON CAST(provider.id AS TEXT) = CAST(config.provider_id AS TEXT) \
         WHERE CAST(config.id AS TEXT) = ? AND config.enabled = 1 AND provider.enabled = 1 \
         AND (CAST(provider.tenant_id AS TEXT) = ? OR provider.tenant_id IS NULL) \
         AND (provider.auth_mode <> 'user_account' OR provider.created_by_id = ?)",
    )
    .bind(model_id)
    .bind(tenant_id)
    .bind(user_id)
    .fetch_one(pool)
    .await
    .map_err(|error| error.to_string())?;
    Ok(count > 0)
}

fn timestamp(state: &AppState, alias: &str, field: &str) -> String {
    if is_postgres(state) {
        format!("to_char({alias}.{field} AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')")
    } else {
        format!("strftime('%Y-%m-%dT%H:%M:%fZ', {alias}.{field})")
    }
}

fn invalid_job(message: &str) -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(json!({"detail": message, "code": "INVALID_SCHEDULED_JOB"})),
    )
        .into_response()
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
            tracing::error!(%message, "rust scheduled api could not resolve the session");
            Err(unavailable())
        }
    }
}

fn not_found() -> Response {
    (StatusCode::NOT_FOUND, Json(json!({"detail": "Not found."}))).into_response()
}

fn unavailable() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({"detail": "The service is temporarily unavailable."})),
    )
        .into_response()
}

fn database_error(error: String) -> Response {
    tracing::error!(%error, "rust scheduled api database operation failed");
    unavailable()
}
