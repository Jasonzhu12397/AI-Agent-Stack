#![allow(clippy::result_large_err)]

use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use chrono::Datelike;
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

pub async fn get_usage(
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
    let budget = match ensure_budget(&state, &tenant_id).await {
        Ok(budget) => budget,
        Err(response) => return response,
    };
    let now = chrono::Utc::now();
    let day_start = now.date_naive().and_hms_opt(0, 0, 0).unwrap().and_utc();
    let today = now.date_naive();
    let month_start = chrono::NaiveDate::from_ymd_opt(today.year(), today.month(), 1)
        .unwrap()
        .and_hms_opt(0, 0, 0)
        .unwrap()
        .and_utc();
    let day_end = day_start + chrono::Duration::days(1);
    let month_end = if month_start.month() == 12 {
        chrono::NaiveDate::from_ymd_opt(month_start.year() + 1, 1, 1)
            .unwrap()
            .and_hms_opt(0, 0, 0)
            .unwrap()
            .and_utc()
    } else {
        chrono::NaiveDate::from_ymd_opt(month_start.year(), month_start.month() + 1, 1)
            .unwrap()
            .and_hms_opt(0, 0, 0)
            .unwrap()
            .and_utc()
    };
    let daily_used = match period_usage(&state, &tenant_id, day_start, now).await {
        Ok(value) => value,
        Err(response) => return response,
    };
    let monthly_used = match period_usage(&state, &tenant_id, month_start, now).await {
        Ok(value) => value,
        Err(response) => return response,
    };
    let history = match usage_history(&state, &tenant_id).await {
        Ok(value) => value,
        Err(response) => return response,
    };
    let grants = match grant_history(&state, &tenant_id).await {
        Ok(value) => value,
        Err(response) => return response,
    };
    let daily_limit = budget.0;
    let monthly_limit = budget.1;
    Json(json!({
        "daily_limit": daily_limit,
        "monthly_limit": monthly_limit,
        "daily_used": daily_used,
        "monthly_used": monthly_used,
        "remaining_daily": (daily_limit - daily_used).max(0),
        "remaining_monthly": (monthly_limit - monthly_used).max(0),
        "reset_at": rfc3339(day_end),
        "monthly_reset_at": rfc3339(month_end),
        "period_start": rfc3339(day_start),
        "period_end": rfc3339(day_end),
        "limits_source": "tenant_budget",
        "history": history,
        "grants": grants,
    }))
    .into_response()
}

pub async fn get_capabilities(
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
    let mut channels = Vec::new();
    let mut diagnostics = Vec::new();
    for (channel, env_names, slots) in [
        (
            "alipay",
            vec![
                "ALIPAY_APP_ID",
                "ALIPAY_PRIVATE_KEY",
                "ALIPAY_PUBLIC_KEY",
                "ALIPAY_NOTIFY_URL",
            ],
            vec!["app_id", "private_key", "public_key", "notify_url"],
        ),
        (
            "wechat",
            vec![
                "WECHATPAY_APPID",
                "WECHATPAY_MCHID",
                "WECHATPAY_API_V3_KEY",
                "WECHATPAY_CERT_SERIAL_NO",
                "WECHATPAY_PRIVATE_KEY",
                "WECHATPAY_PLATFORM_CERT",
                "WECHATPAY_NOTIFY_URL",
            ],
            vec![
                "appid",
                "mchid",
                "api_v3_key",
                "serial_no",
                "private_key",
                "platform_cert",
                "notify_url",
            ],
        ),
    ] {
        let missing = missing_slots(&state, channel, &env_names, &slots).await;
        if missing.is_empty() {
            channels.push(channel);
        }
        diagnostics.push(json!({
            "channel": channel,
            "configured": missing.is_empty(),
            "missing": missing,
            "required": slots,
        }));
    }
    channels.sort();
    let available = !channels.is_empty();
    Json(json!({
        "payment": {
            "available": available,
            "channels": channels,
            "reason_code": if available { Value::Null } else { Value::String("PAYMENT_PROVIDER_NOT_CONFIGURED".into()) },
            "diagnostics": diagnostics,
        }
    }))
    .into_response()
}

pub async fn list_orders(
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
    let sql = order_select(&state, "ORDER BY order_row.created_at DESC");
    let rows = match sqlx::query(&sql).bind(&tenant_id).fetch_all(pool).await {
        Ok(rows) => rows,
        Err(error) => return database_error(error.to_string()),
    };
    let results: Vec<Value> = rows.iter().map(order_payload).collect();
    Json(json!({"results": results})).into_response()
}

pub async fn get_order(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, order_id)): Path<(String, String)>,
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
    let sql = order_select(&state, "AND CAST(order_row.id AS TEXT) = ? LIMIT 1");
    let row = match sqlx::query(&sql)
        .bind(&tenant_id)
        .bind(&order_id)
        .fetch_optional(pool)
        .await
    {
        Ok(row) => row,
        Err(error) => return database_error(error.to_string()),
    };
    let Some(row) = row else {
        return not_found();
    };
    let payment_text: Option<String> = row.try_get("payment_text").ok();
    let payment = payment_text.and_then(|text| serde_json::from_str::<Value>(&text).ok());
    let mut payload = order_payload(&row);
    if let Some(payment) = payment.filter(|value| !value.is_null()) {
        payload["payment"] = payment;
    }
    Json(payload).into_response()
}

fn order_select(state: &AppState, suffix: &str) -> String {
    let paid = timestamp(state, "order_row", "paid_at");
    let created = timestamp(state, "order_row", "created_at");
    let payment = json_text(state, "order_row.payment_instruction");
    format!(
        "SELECT CAST(order_row.id AS TEXT) AS id, CAST(order_row.tenant_id AS TEXT) AS tenant, \
         plan.code AS plan_code, order_row.status, order_row.amount_cents, \
         order_row.refunded_cents, order_row.currency, order_row.channel, \
         order_row.provider_trade_no, {paid} AS paid, order_row.idempotency_key, \
         {created} AS created, {payment} AS payment_text FROM core_order order_row \
         JOIN core_plan plan ON CAST(plan.id AS TEXT) = CAST(order_row.plan_id AS TEXT) \
         WHERE CAST(order_row.tenant_id AS TEXT) = ? {suffix}"
    )
}

fn order_payload(row: &sqlx::any::AnyRow) -> Value {
    json!({
        "id": row.try_get::<String, _>("id").unwrap_or_default(),
        "tenant": row.try_get::<String, _>("tenant").unwrap_or_default(),
        "plan": row.try_get::<String, _>("plan_code").unwrap_or_default(),
        "status": row.try_get::<String, _>("status").unwrap_or_default(),
        "amount_cents": row.try_get::<i64, _>("amount_cents").unwrap_or(0),
        "refunded_cents": row.try_get::<i64, _>("refunded_cents").unwrap_or(0),
        "currency": row.try_get::<String, _>("currency").unwrap_or_default(),
        "channel": row.try_get::<String, _>("channel").unwrap_or_default(),
        "provider_trade_no": row.try_get::<String, _>("provider_trade_no").unwrap_or_default(),
        "paid_at": row.try_get::<Option<String>, _>("paid").ok().flatten(),
        "idempotency_key": row.try_get::<String, _>("idempotency_key").unwrap_or_default(),
        "created_at": row.try_get::<String, _>("created").unwrap_or_default(),
    })
}

async fn ensure_budget(state: &AppState, tenant_id: &str) -> Result<(i64, i64), Response> {
    let Some(pool) = state.identity_pool.as_ref() else {
        return Err(unavailable());
    };
    let existing = sqlx::query(
        "SELECT daily_limit, monthly_limit FROM core_tenanttokenbudget \
         WHERE CAST(tenant_id AS TEXT) = ? LIMIT 1",
    )
    .bind(tenant_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| database_error(error.to_string()))?;
    if let Some(row) = existing {
        return Ok((
            row.try_get::<i64, _>("daily_limit").unwrap_or(100_000),
            row.try_get::<i64, _>("monthly_limit").unwrap_or(1_000_000),
        ));
    }
    let id = uuid::Uuid::new_v4().to_string();
    let now = if is_postgres(state) {
        "NOW()".to_string()
    } else {
        "datetime('now')".to_string()
    };
    let sql = format!(
        "INSERT INTO core_tenanttokenbudget (id, tenant_id, daily_limit, monthly_limit, created_at, updated_at) \
         VALUES (?, ?, 100000, 1000000, {now}, {now})"
    );
    sqlx::query(&sql)
        .bind(id)
        .bind(tenant_id)
        .execute(pool)
        .await
        .map_err(|error| database_error(error.to_string()))?;
    Ok((100_000, 1_000_000))
}

async fn period_usage(
    state: &AppState,
    tenant_id: &str,
    start: chrono::DateTime<chrono::Utc>,
    now: chrono::DateTime<chrono::Utc>,
) -> Result<i64, Response> {
    let Some(pool) = state.identity_pool.as_ref() else {
        return Err(unavailable());
    };
    let start_text = format!("{}", start.format("%Y-%m-%d %H:%M:%S"));
    let now_text = format!("{}", now.format("%Y-%m-%d %H:%M:%S"));
    let settled = sqlx::query_scalar::<_, Option<i64>>(
        "SELECT SUM(consumed_tokens) FROM core_tenanttokenusage \
         WHERE CAST(tenant_id AS TEXT) = ? AND status = 'settled' \
         AND settled_at >= ? AND settled_at <= ?",
    )
    .bind(tenant_id)
    .bind(&start_text)
    .bind(&now_text)
    .fetch_one(pool)
    .await
    .map_err(|error| database_error(error.to_string()))?
    .unwrap_or(0);
    let reserved = sqlx::query_scalar::<_, Option<i64>>(
        "SELECT SUM(reserved_tokens) FROM core_tenanttokenusage \
         WHERE CAST(tenant_id AS TEXT) = ? AND status = 'reserved' AND expires_at > ?",
    )
    .bind(tenant_id)
    .bind(&now_text)
    .fetch_one(pool)
    .await
    .map_err(|error| database_error(error.to_string()))?
    .unwrap_or(0);
    Ok(settled + reserved)
}

async fn usage_history(state: &AppState, tenant_id: &str) -> Result<Value, Response> {
    let Some(pool) = state.identity_pool.as_ref() else {
        return Err(unavailable());
    };
    let period_expr = if is_postgres(state) {
        "to_char(settled_at AT TIME ZONE 'UTC', 'YYYY-MM')"
    } else {
        "strftime('%Y-%m', settled_at)"
    };
    let source_expr = "CASE WHEN scope_ref IS NOT NULL AND scope_ref <> '' THEN scope_ref \
         WHEN agent_run_id IS NOT NULL THEN 'agent' ELSE 'workflow' END";
    let sql = format!(
        "SELECT {period_expr} AS period, {source_expr} AS source, \
         SUM(consumed_tokens) AS consumed_tokens FROM core_tenanttokenusage \
         WHERE CAST(tenant_id AS TEXT) = ? AND status = 'settled' AND settled_at IS NOT NULL \
         GROUP BY period, source ORDER BY period DESC, source ASC"
    );
    let rows = sqlx::query(&sql)
        .bind(tenant_id)
        .fetch_all(pool)
        .await
        .map_err(|error| database_error(error.to_string()))?;
    Ok(Value::Array(
        rows.iter()
            .map(|row| {
                json!({
                    "period": row.try_get::<String, _>("period").unwrap_or_default(),
                    "source": row.try_get::<String, _>("source").unwrap_or_default(),
                    "consumed_tokens": row.try_get::<i64, _>("consumed_tokens").unwrap_or(0),
                })
            })
            .collect(),
    ))
}

async fn grant_history(state: &AppState, tenant_id: &str) -> Result<Value, Response> {
    let Some(pool) = state.identity_pool.as_ref() else {
        return Err(unavailable());
    };
    let expires = timestamp(state, "grant", "expires_at");
    let created = timestamp(state, "grant", "created_at");
    let sql = format!(
        "SELECT CAST(grant.id AS TEXT) AS id, CAST(grant.order_id AS TEXT) AS order_id, \
         grant.period, grant.tokens, {expires} AS expires, grant.reason, {created} AS created \
         FROM core_entitlementgrant grant WHERE CAST(grant.tenant_id AS TEXT) = ? \
         ORDER BY grant.created_at DESC"
    );
    let rows = sqlx::query(&sql)
        .bind(tenant_id)
        .fetch_all(pool)
        .await
        .map_err(|error| database_error(error.to_string()))?;
    Ok(Value::Array(
        rows.iter()
            .map(|row| {
                json!({
                    "id": row.try_get::<String, _>("id").unwrap_or_default(),
                    "order": row.try_get::<String, _>("order_id").unwrap_or_default(),
                    "period": row.try_get::<String, _>("period").unwrap_or_default(),
                    "tokens": row.try_get::<i64, _>("tokens").unwrap_or(0),
                    "expires_at": row.try_get::<Option<String>, _>("expires").ok().flatten(),
                    "reason": row.try_get::<String, _>("reason").unwrap_or_default(),
                    "created_at": row.try_get::<String, _>("created").unwrap_or_default(),
                })
            })
            .collect(),
    ))
}

async fn missing_slots(
    state: &AppState,
    channel: &str,
    env_names: &[&str],
    slots: &[&str],
) -> Vec<String> {
    let mut missing = Vec::new();
    for (index, slot) in slots.iter().enumerate() {
        let env_name = env_names.get(index).copied().unwrap_or_default();
        if stored_slot(state, channel, slot).await {
            continue;
        }
        if std::env::var(env_name)
            .map(|value| !value.trim().is_empty())
            .unwrap_or(false)
        {
            continue;
        }
        missing.push((*slot).to_string());
    }
    missing
}

async fn stored_slot(state: &AppState, channel: &str, slot: &str) -> bool {
    let Some(pool) = state.identity_pool.as_ref() else {
        return false;
    };
    let count = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_paymentchannelsecret WHERE channel = ? AND slot = ?",
    )
    .bind(channel)
    .bind(slot)
    .fetch_one(pool)
    .await
    .unwrap_or(0);
    count > 0
}

fn rfc3339(value: chrono::DateTime<chrono::Utc>) -> String {
    value.format("%Y-%m-%dT%H:%M:%S+00:00").to_string()
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
