#![allow(clippy::result_large_err)]

use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Value};
use sqlx::{AnyPool, Row};

use crate::context::{authenticate, AuthError};
use crate::proxy::AppState;
use crate::tenancy::{is_postgres, normalize_timestamp};

pub async fn list_plans(
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
    if !tenant_member(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return not_found();
    }
    let providers = payment_providers(&state).await;
    let true_literal = if is_postgres(&state) { "TRUE" } else { "1" };
    let features = json_text(&state, "plan.features");
    let sql = format!(
        "SELECT CAST(plan.id AS TEXT) AS id, plan.code, plan.name, plan.description, \
         plan.interval, plan.price_cents, plan.currency, plan.token_quota_daily, \
         plan.token_quota_monthly, {features} AS features_text, plan.region_scope, \
         CAST(plan.active AS INTEGER) AS active_int FROM core_plan plan \
         WHERE plan.active = {true_literal} ORDER BY plan.price_cents, plan.code"
    );
    let rows = match sqlx::query(&sql).fetch_all(pool).await {
        Ok(rows) => rows,
        Err(error) => return database_error(error.to_string()),
    };
    let results: Vec<Value> = rows
        .iter()
        .map(|row| {
            let available = !providers.is_empty();
            json!({
                "id": row.try_get::<String, _>("id").unwrap_or_default(),
                "code": row.try_get::<String, _>("code").unwrap_or_default(),
                "name": row.try_get::<String, _>("name").unwrap_or_default(),
                "description": row.try_get::<String, _>("description").unwrap_or_default(),
                "interval": row.try_get::<String, _>("interval").unwrap_or_default(),
                "price_cents": row.try_get::<i64, _>("price_cents").unwrap_or_default(),
                "currency": row.try_get::<String, _>("currency").unwrap_or_default(),
                "token_quota_daily": row.try_get::<i64, _>("token_quota_daily").unwrap_or_default(),
                "token_quota_monthly": row.try_get::<i64, _>("token_quota_monthly").unwrap_or_default(),
                "features": parse_json(row.try_get::<Option<String>, _>("features_text").ok().flatten()).unwrap_or_else(|| json!([])),
                "region_scope": row.try_get::<String, _>("region_scope").unwrap_or_default(),
                "active": row.try_get::<i64, _>("active_int").unwrap_or(0) != 0,
                "purchasable": available,
                "reason_code": if available { Value::Null } else { Value::String("PAYMENT_PROVIDER_NOT_CONFIGURED".into()) },
            })
        })
        .collect();
    Json(json!({"results": results})).into_response()
}

pub async fn get_subscription(
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
    if !tenant_member(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return not_found();
    }
    let started = timestamp(&state, "subscription", "started_at");
    let period_start = timestamp(&state, "subscription", "current_period_start");
    let period_end = timestamp(&state, "subscription", "current_period_end");
    let sql = format!(
        "SELECT CAST(subscription.id AS TEXT) AS id, plan.code AS plan_code, subscription.status, \
         {started} AS started, {period_start} AS period_start, {period_end} AS period_end, \
         CAST(subscription.cancel_at_period_end AS INTEGER) AS cancel_int, \
         subscription.renewal_mode FROM core_subscription subscription \
         JOIN core_plan plan ON CAST(plan.id AS TEXT) = CAST(subscription.plan_id AS TEXT) \
         WHERE CAST(subscription.tenant_id AS TEXT) = ? \
         ORDER BY subscription.created_at DESC LIMIT 1"
    );
    let row = match sqlx::query(&sql)
        .bind(&tenant_id)
        .fetch_optional(pool)
        .await
    {
        Ok(row) => row,
        Err(error) => return database_error(error.to_string()),
    };
    let subscription = match row {
        None => Value::Null,
        Some(row) => {
            let started_at: Option<String> = row.try_get("started").ok();
            let period_start_at: Option<String> = row.try_get("period_start").ok();
            let period_end_at: Option<String> = row.try_get("period_end").ok();
            let cancel = row.try_get::<i64, _>("cancel_int").unwrap_or(0) != 0;
            json!({
                "id": row.try_get::<String, _>("id").unwrap_or_default(),
                "plan": row.try_get::<String, _>("plan_code").unwrap_or_default(),
                "status": row.try_get::<String, _>("status").unwrap_or_default(),
                "started_at": started_at.as_deref().map(normalize_timestamp),
                "current_period_start": period_start_at.as_deref().map(normalize_timestamp),
                "current_period_end": period_end_at.as_deref().map(normalize_timestamp),
                "cancel_at_period_end": cancel,
                "renewal_mode": row.try_get::<String, _>("renewal_mode").unwrap_or_default(),
                "cancel_effective_at": if cancel { period_end_at.as_deref().map(normalize_timestamp) } else { None },
            })
        }
    };
    Json(json!({"subscription": subscription, "source": "authoritative"})).into_response()
}

async fn payment_providers(state: &AppState) -> Vec<String> {
    let mut providers = Vec::new();
    if env_slots(
        "alipay",
        &[
            "ALIPAY_APP_ID",
            "ALIPAY_PRIVATE_KEY",
            "ALIPAY_PUBLIC_KEY",
            "ALIPAY_NOTIFY_URL",
        ],
    )
    .await
        || stored_slots(
            state,
            "alipay",
            &["app_id", "private_key", "public_key", "notify_url"],
        )
        .await
    {
        providers.push("alipay".to_string());
    }
    if env_slots(
        "wechat",
        &[
            "WECHATPAY_APPID",
            "WECHATPAY_MCHID",
            "WECHATPAY_API_V3_KEY",
            "WECHATPAY_CERT_SERIAL_NO",
            "WECHATPAY_PRIVATE_KEY",
            "WECHATPAY_PLATFORM_CERT",
            "WECHATPAY_NOTIFY_URL",
        ],
    )
    .await
        || stored_slots(
            state,
            "wechat",
            &[
                "appid",
                "mchid",
                "api_v3_key",
                "serial_no",
                "private_key",
                "platform_cert",
                "notify_url",
            ],
        )
        .await
    {
        providers.push("wechat".to_string());
    }
    providers.sort();
    providers
}

async fn stored_slots(state: &AppState, channel: &str, slots: &[&str]) -> bool {
    let Some(pool) = state.identity_pool.as_ref() else {
        return false;
    };
    for slot in slots {
        let count = sqlx::query_scalar::<_, i64>(
            "SELECT COUNT(*) FROM core_paymentchannelsecret WHERE channel = ? AND slot = ?",
        )
        .bind(channel)
        .bind(*slot)
        .fetch_one(pool)
        .await
        .unwrap_or(0);
        if count == 0 {
            return false;
        }
    }
    true
}

async fn env_slots(_channel: &str, names: &[&str]) -> bool {
    names.iter().all(|name| {
        std::env::var(name)
            .map(|value| !value.trim().is_empty())
            .unwrap_or(false)
    })
}

async fn tenant_member(pool: &AnyPool, tenant_id: &str, user_id: i64) -> Result<bool, String> {
    let count = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_tenantmembership \
         WHERE CAST(tenant_id AS TEXT) = ? AND user_id = ?",
    )
    .bind(tenant_id)
    .bind(user_id)
    .fetch_one(pool)
    .await
    .map_err(|error| error.to_string())?;
    Ok(count > 0)
}

fn json_text(state: &AppState, column: &str) -> String {
    if is_postgres(state) {
        format!("CAST({column} AS TEXT)")
    } else {
        column.to_string()
    }
}

fn parse_json(value: Option<String>) -> Option<Value> {
    value.and_then(|value| serde_json::from_str(&value).ok())
}

fn timestamp(state: &AppState, alias: &str, field: &str) -> String {
    if is_postgres(state) {
        format!("to_char({alias}.{field} AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')")
    } else {
        format!("strftime('%Y-%m-%dT%H:%M:%fZ', {alias}.{field})")
    }
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
            tracing::error!(%message, "rust billing api could not resolve the session");
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
    tracing::error!(%error, "rust billing api database operation failed");
    unavailable()
}
