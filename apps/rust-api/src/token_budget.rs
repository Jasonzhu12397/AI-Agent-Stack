#![allow(clippy::result_large_err)]

use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use chrono::{Datelike, SecondsFormat, Timelike, Utc};
use serde_json::{json, Value};

use crate::audit::{record_event_in_transaction, AuditEvent};
use crate::context::{authenticate, AuthError};
use crate::proxy::AppState;
use crate::tenancy::{is_postgres, tenant_admin, tenant_role};

const DEFAULT_DAILY_LIMIT: i64 = 100_000;
const DEFAULT_MONTHLY_LIMIT: i64 = 1_000_000;

/// Native replacement for `/api/v1/tenants/{tenant}/token-budget/`.
pub async fn get_budget(
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
    match tenant_role(pool, &tenant_id, user.id).await {
        Ok(Some(_)) => {}
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    }
    let budget = match ensure_budget(pool, &state, &tenant_id).await {
        Ok(budget) => budget,
        Err(error) => return database_error_message(error),
    };
    match usage_payload(pool, &state, &tenant_id, budget).await {
        Ok(payload) => Json(payload).into_response(),
        Err(error) => database_error_message(error),
    }
}

pub async fn update_budget(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
    body: Bytes,
) -> Response {
    update_budget_inner(state, headers, tenant_id, body, false).await
}

pub async fn patch_budget(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
    body: Bytes,
) -> Response {
    update_budget_inner(state, headers, tenant_id, body, true).await
}

async fn update_budget_inner(
    state: AppState,
    headers: HeaderMap,
    tenant_id: String,
    body: Bytes,
    partial: bool,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    match tenant_role(pool, &tenant_id, user.id).await {
        Ok(Some(_)) => {}
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    }
    if !match tenant_admin(pool, &tenant_id, user.id).await {
        Ok(value) => value,
        Err(error) => return database_error_message(error),
    } {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"detail": "You do not have permission to perform this action."})),
        )
            .into_response();
    }
    let payload: Value = match serde_json::from_slice::<Value>(&body) {
        Ok(value) if value.is_object() => value,
        _ => return bad_request("A valid JSON body is required."),
    };
    let daily = match read_limit(&payload, "daily_limit", partial) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let monthly = match read_limit(&payload, "monthly_limit", partial) {
        Ok(value) => value,
        Err(response) => return response,
    };
    if daily.is_none() && monthly.is_none() {
        return bad_request("At least one budget limit must be supplied.");
    }
    let postgres = is_postgres(&state);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return sqlx_error(error),
    };
    let budget = match ensure_budget_in_transaction(&mut transaction, postgres, &tenant_id).await {
        Ok(budget) => budget,
        Err(error) => return database_error_message(error),
    };
    if let Some(value) = daily {
        if let Err(error) =
            update_budget_field(&mut transaction, postgres, "daily_limit", value, &tenant_id).await
        {
            return database_error_message(error);
        }
    }
    if let Some(value) = monthly {
        if let Err(error) = update_budget_field(
            &mut transaction,
            postgres,
            "monthly_limit",
            value,
            &tenant_id,
        )
        .await
        {
            return database_error_message(error);
        }
    }
    let mut metadata = serde_json::Map::new();
    if let Some(value) = daily {
        metadata.insert("daily_limit".to_string(), json!(value));
    }
    if let Some(value) = monthly {
        metadata.insert("monthly_limit".to_string(), json!(value));
    }
    if let Err(error) = record_event_in_transaction(
        &mut transaction,
        postgres,
        &AuditEvent {
            tenant_id: Some(&tenant_id),
            project_id: None,
            event_type: "token_budget.updated",
            resource_type: "tenant_token_budget",
            resource_id: &budget.id.to_string(),
            outcome: "success",
            actor_id: Some(user.id),
            request_id: headers
                .get("x-request-id")
                .and_then(|value| value.to_str().ok())
                .unwrap_or(""),
            remote_addr: None,
            user_agent: headers
                .get("user-agent")
                .and_then(|value| value.to_str().ok())
                .unwrap_or(""),
            metadata: Value::Object(metadata),
        },
    )
    .await
    {
        return database_error_message(error);
    }
    if let Err(error) = transaction.commit().await {
        return sqlx_error(error);
    }
    let budget = match ensure_budget(pool, &state, &tenant_id).await {
        Ok(budget) => budget,
        Err(error) => return database_error_message(error),
    };
    match usage_payload(pool, &state, &tenant_id, budget).await {
        Ok(payload) => Json(payload).into_response(),
        Err(error) => database_error_message(error),
    }
}

async fn update_budget_field(
    transaction: &mut sqlx::Transaction<'_, sqlx::Any>,
    postgres: bool,
    field: &str,
    value: i64,
    tenant_id: &str,
) -> Result<(), String> {
    let sql = if postgres {
        format!(
            "UPDATE core_tenanttokenbudget SET {field} = ?, updated_at = ?::timestamptz \
             WHERE CAST(tenant_id AS TEXT) = ?"
        )
    } else {
        format!(
            "UPDATE core_tenanttokenbudget SET {field} = ?, updated_at = ? \
             WHERE CAST(tenant_id AS TEXT) = ?"
        )
    };
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    sqlx::query(&sql)
        .bind(value)
        .bind(&now)
        .bind(tenant_id)
        .execute(&mut **transaction)
        .await
        .map(|_| ())
        .map_err(|error| error.to_string())
}

fn read_limit(payload: &Value, field: &str, partial: bool) -> Result<Option<i64>, Response> {
    let value = payload.get(field);
    if value.is_none() {
        if partial {
            return Ok(None);
        }
        return Err(bad_request("A positive integer is required."));
    }
    let value = value.unwrap();
    let Some(number) = value.as_i64() else {
        return Err(bad_request("A positive integer is required."));
    };
    if number < 1 {
        return Err(bad_request("A positive integer is required."));
    }
    Ok(Some(number))
}

struct BudgetRow {
    id: i64,
    daily_limit: i64,
    monthly_limit: i64,
}

async fn ensure_budget(
    pool: &sqlx::AnyPool,
    state: &AppState,
    tenant_id: &str,
) -> Result<BudgetRow, String> {
    let postgres = is_postgres(state);
    let mut transaction = pool.begin().await.map_err(|error| error.to_string())?;
    let budget = ensure_budget_in_transaction(&mut transaction, postgres, tenant_id).await?;
    transaction
        .commit()
        .await
        .map_err(|error| error.to_string())?;
    Ok(budget)
}

async fn ensure_budget_in_transaction(
    transaction: &mut sqlx::Transaction<'_, sqlx::Any>,
    postgres: bool,
    tenant_id: &str,
) -> Result<BudgetRow, String> {
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    let insert = if postgres {
        "INSERT INTO core_tenanttokenbudget \
         (tenant_id, daily_limit, monthly_limit, created_at, updated_at) \
         VALUES (?::uuid, ?, ?, ?::timestamptz, ?::timestamptz) \
         ON CONFLICT (tenant_id) DO NOTHING"
    } else {
        "INSERT INTO core_tenanttokenbudget \
         (tenant_id, daily_limit, monthly_limit, created_at, updated_at) \
         VALUES (?, ?, ?, ?, ?) ON CONFLICT (tenant_id) DO NOTHING"
    };
    sqlx::query(insert)
        .bind(tenant_id)
        .bind(DEFAULT_DAILY_LIMIT)
        .bind(DEFAULT_MONTHLY_LIMIT)
        .bind(&now)
        .bind(&now)
        .execute(&mut **transaction)
        .await
        .map_err(|error| error.to_string())?;

    let (id, daily_limit, monthly_limit) = sqlx::query_as::<_, (i64, i64, i64)>(
        "SELECT id, daily_limit, monthly_limit FROM core_tenanttokenbudget \
         WHERE CAST(tenant_id AS TEXT) = ?",
    )
    .bind(tenant_id)
    .fetch_one(&mut **transaction)
    .await
    .map_err(|error| error.to_string())?;
    Ok(BudgetRow {
        id,
        daily_limit,
        monthly_limit,
    })
}

async fn usage_payload(
    pool: &sqlx::AnyPool,
    state: &AppState,
    tenant_id: &str,
    budget: BudgetRow,
) -> Result<Value, String> {
    let postgres = is_postgres(state);
    let now = Utc::now();
    let day_start = now
        .with_hour(0)
        .and_then(|value| value.with_minute(0))
        .and_then(|value| value.with_second(0))
        .and_then(|value| value.with_nanosecond(0))
        .unwrap_or(now);
    let month_start = day_start.with_day(1).unwrap_or(day_start);
    let daily_used = period_usage(pool, postgres, tenant_id, day_start, now).await?;
    let monthly_used = period_usage(pool, postgres, tenant_id, month_start, now).await?;
    Ok(json!({
        "daily_limit": budget.daily_limit,
        "monthly_limit": budget.monthly_limit,
        "daily_used": daily_used,
        "monthly_used": monthly_used,
    }))
}

async fn period_usage(
    pool: &sqlx::AnyPool,
    postgres: bool,
    tenant_id: &str,
    start: chrono::DateTime<Utc>,
    now: chrono::DateTime<Utc>,
) -> Result<i64, String> {
    let settled_sql = if postgres {
        "SELECT COALESCE(SUM(consumed_tokens), 0) FROM core_tenanttokenusage \
         WHERE CAST(tenant_id AS TEXT) = ? AND status = 'settled' \
         AND settled_at >= ?::timestamptz AND settled_at <= ?::timestamptz"
    } else {
        "SELECT COALESCE(SUM(consumed_tokens), 0) FROM core_tenanttokenusage \
         WHERE CAST(tenant_id AS TEXT) = ? AND status = 'settled' \
         AND datetime(settled_at) >= datetime(?) AND datetime(settled_at) <= datetime(?)"
    };
    let start_text = start.to_rfc3339_opts(SecondsFormat::Micros, false);
    let now_text = now.to_rfc3339_opts(SecondsFormat::Micros, false);
    let settled = sqlx::query_scalar::<_, i64>(settled_sql)
        .bind(tenant_id)
        .bind(&start_text)
        .bind(&now_text)
        .fetch_one(pool)
        .await
        .map_err(|error| error.to_string())?;
    let reserved_sql = if postgres {
        "SELECT COALESCE(SUM(reserved_tokens), 0) FROM core_tenanttokenusage \
         WHERE CAST(tenant_id AS TEXT) = ? AND status = 'reserved' AND expires_at > ?::timestamptz"
    } else {
        "SELECT COALESCE(SUM(reserved_tokens), 0) FROM core_tenanttokenusage \
         WHERE CAST(tenant_id AS TEXT) = ? AND status = 'reserved'          AND datetime(expires_at) > datetime(?)"
    };
    let reserved = sqlx::query_scalar::<_, i64>(reserved_sql)
        .bind(tenant_id)
        .bind(&now_text)
        .fetch_one(pool)
        .await
        .map_err(|error| error.to_string())?;
    Ok(settled + reserved)
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
            tracing::error!(%message, "rust token budget api could not resolve the session");
            Err(unavailable())
        }
    }
}

fn bad_request(message: &str) -> Response {
    (StatusCode::BAD_REQUEST, Json(json!({"detail": message}))).into_response()
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

fn sqlx_error(error: sqlx::Error) -> Response {
    tracing::error!(%error, "rust token budget database operation failed");
    unavailable()
}

fn database_error_message(message: String) -> Response {
    tracing::error!(%message, "rust token budget database operation failed");
    unavailable()
}
