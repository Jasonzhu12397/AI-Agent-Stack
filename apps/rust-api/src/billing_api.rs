#![allow(clippy::result_large_err)]

use aes_gcm::aead::{Aead, KeyInit, Payload};
use aes_gcm::{Aes256Gcm, Nonce};
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use base64::Engine;
use chrono::Datelike;
use serde_json::{json, Value};
use sha2::Digest;
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

pub async fn get_payment_credentials(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, channel)): Path<(String, String)>,
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
    let Some((env_names, slots)) = channel_slots(&channel) else {
        return (StatusCode::NOT_FOUND, Json(json!({"error": {"code": "PAYMENT_CHANNEL_NOT_SUPPORTED", "message": "Unsupported channel.", "details": {}}}))).into_response();
    };
    credentials_report(&state, &channel, &env_names, &slots).await
}

pub async fn put_payment_credentials(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, channel)): Path<(String, String)>,
    body: Result<Json<Value>, axum::extract::rejection::JsonRejection>,
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
    if !tenant_admin(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return (StatusCode::FORBIDDEN, Json(json!({"error": {"code": "TENANT_ADMIN_REQUIRED", "message": "Tenant owner or admin role is required to change payment credentials.", "details": {}}}))).into_response();
    }
    let Some((env_names, slots)) = channel_slots(&channel) else {
        return (StatusCode::NOT_FOUND, Json(json!({"error": {"code": "PAYMENT_CHANNEL_NOT_SUPPORTED", "message": "Unsupported channel.", "details": {}}}))).into_response();
    };
    let Json(payload) = match body {
        Ok(body) => body,
        Err(_) => return (StatusCode::BAD_REQUEST, Json(json!({"error": {"code": "INVALID_CREDENTIALS", "message": "Request body must be a JSON object.", "details": {}}}))).into_response(),
    };
    let Some(values) = payload.get("values").and_then(Value::as_object) else {
        return (StatusCode::BAD_REQUEST, Json(json!({"error": {"code": "INVALID_CREDENTIALS", "message": "values must be a non-empty object.", "details": {}}}))).into_response();
    };
    if values.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"error": {"code": "INVALID_CREDENTIALS", "message": "values must be a non-empty object.", "details": {}}}))).into_response();
    }
    let unknown: Vec<&str> = values
        .keys()
        .filter(|slot| !slots.contains(&slot.as_str()))
        .map(String::as_str)
        .collect();
    if !unknown.is_empty() {
        return (StatusCode::BAD_REQUEST, Json(json!({"error": {"code": "INVALID_CREDENTIALS", "message": format!("Unknown slot(s) for {channel}: {}.", unknown.join(", ")), "details": {}}}))).into_response();
    }
    let Some(secret_key) = state.config.django_secret_key.as_deref() else {
        return unavailable();
    };
    for (slot, value) in values {
        let text = value.as_str().map(str::trim).unwrap_or_default();
        let result = if text.is_empty() {
            sqlx::query("DELETE FROM core_paymentchannelsecret WHERE channel = ? AND slot = ?")
                .bind(&channel)
                .bind(slot)
                .execute(pool)
                .await
                .map(|_| ())
                .map_err(|error| error.to_string())
        } else {
            match seal_credential(secret_key, text) {
                Ok(sealed) => upsert_secret(pool, &state, &channel, slot, &sealed, user.id).await,
                Err(response) => return response,
            }
        };
        if let Err(error) = result {
            return database_error(error);
        }
    }
    if let Some(database_url) = state.config.database_url.as_deref() {
        let metadata =
            json!({"channel": channel, "slots": values.keys().cloned().collect::<Vec<_>>()});
        let event = crate::audit::AuditEvent {
            tenant_id: Some(&tenant_id),
            project_id: None,
            event_type: "billing.payment.credentials",
            resource_type: "payment_channel",
            resource_id: &channel,
            outcome: "success",
            actor_id: Some(user.id),
            request_id: "",
            remote_addr: None,
            user_agent: "",
            metadata,
        };
        if let Err(error) = crate::audit::record_event(pool, database_url, event).await {
            tracing::error!(%error, "rust billing api could not append the credential audit event");
            return unavailable();
        }
    }
    credentials_report(&state, &channel, &env_names, &slots).await
}

async fn credentials_report(
    state: &AppState,
    channel: &str,
    env_names: &[&str],
    slots: &[&str],
) -> Response {
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let rows = sqlx::query_scalar::<_, String>(
        "SELECT slot FROM core_paymentchannelsecret WHERE channel = ?",
    )
    .bind(channel)
    .fetch_all(pool)
    .await
    .unwrap_or_default();
    let mut filled: Vec<String> = rows;
    for (index, env_name) in env_names.iter().enumerate() {
        if std::env::var(env_name)
            .map(|value| !value.trim().is_empty())
            .unwrap_or(false)
        {
            if let Some(slot) = slots.get(index) {
                filled.push((*slot).to_string());
            }
        }
    }
    filled.sort();
    filled.dedup();
    let missing: Vec<String> = slots
        .iter()
        .filter(|slot| !filled.iter().any(|filled_slot| filled_slot == *slot))
        .map(|slot| (*slot).to_string())
        .collect();
    Json(json!({
        "channel": channel,
        "required": slots,
        "slots": slots,
        "filled": filled,
        "missing": missing,
        "configured": missing.is_empty(),
    }))
    .into_response()
}

fn channel_slots(channel: &str) -> Option<(Vec<&'static str>, Vec<&'static str>)> {
    match channel {
        "alipay" => Some((
            vec![
                "ALIPAY_APP_ID",
                "ALIPAY_PRIVATE_KEY",
                "ALIPAY_PUBLIC_KEY",
                "ALIPAY_NOTIFY_URL",
            ],
            vec!["app_id", "private_key", "public_key", "notify_url"],
        )),
        "wechat" => Some((
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
        )),
        _ => None,
    }
}

async fn tenant_admin(pool: &AnyPool, tenant_id: &str, user_id: i64) -> Result<bool, String> {
    let role = sqlx::query_scalar::<_, String>(
        "SELECT role FROM core_tenantmembership \
         WHERE CAST(tenant_id AS TEXT) = ? AND user_id = ? LIMIT 1",
    )
    .bind(tenant_id)
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())?;
    Ok(matches!(role.as_deref(), Some("owner") | Some("admin")))
}

fn seal_credential(secret_key: &str, plaintext: &str) -> Result<String, Response> {
    let scope = b"payment-channel-secret";
    let key_bytes = sha2::Sha256::digest(format!("payment-channel-secret:{secret_key}").as_bytes());
    let cipher = Aes256Gcm::new_from_slice(&key_bytes)
        .map_err(|_| database_error("invalid payment credential key".to_string()))?;
    let uuid = uuid::Uuid::new_v4();
    let nonce_bytes = &uuid.as_bytes()[..12];
    let nonce = Nonce::from_slice(nonce_bytes);
    let ciphertext = cipher
        .encrypt(
            nonce,
            Payload {
                msg: plaintext.as_bytes(),
                aad: scope,
            },
        )
        .map_err(|_| database_error("failed to seal the payment credential".to_string()))?;
    let mut blob = nonce_bytes.to_vec();
    blob.extend_from_slice(&ciphertext);
    Ok(base64::engine::general_purpose::URL_SAFE.encode(blob))
}

async fn upsert_secret(
    pool: &AnyPool,
    state: &AppState,
    channel: &str,
    slot: &str,
    sealed: &str,
    user_id: i64,
) -> Result<(), String> {
    let now = if is_postgres(state) {
        "NOW()".to_string()
    } else {
        "datetime('now')".to_string()
    };
    let update = format!(
        "UPDATE core_paymentchannelsecret SET sealed_value = ?, updated_by_id = ?, updated_at = {now} \
         WHERE channel = ? AND slot = ?"
    );
    let updated = sqlx::query(&update)
        .bind(sealed)
        .bind(user_id)
        .bind(channel)
        .bind(slot)
        .execute(pool)
        .await
        .map_err(|error| error.to_string())?
        .rows_affected();
    if updated > 0 {
        return Ok(());
    }
    let insert = format!(
        "INSERT INTO core_paymentchannelsecret (id, channel, slot, sealed_value, updated_by_id, updated_at) \
         VALUES (?, ?, ?, ?, ?, {now})"
    );
    sqlx::query(&insert)
        .bind(uuid::Uuid::new_v4().to_string())
        .bind(channel)
        .bind(slot)
        .bind(sealed)
        .bind(user_id)
        .execute(pool)
        .await
        .map(|_| ())
        .map_err(|error| error.to_string())
}

pub async fn start_subscription_trial(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
    body: Result<Json<Value>, axum::extract::rejection::JsonRejection>,
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
    if !tenant_admin(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return admin_required();
    }
    let Json(payload) = match body {
        Ok(body) => body,
        Err(_) => {
            return subscription_error(
                "SUBSCRIPTION_PLAN_REQUIRED",
                "plan_code is required.",
                400,
                json!({}),
            )
        }
    };
    let plan_code = payload
        .get("plan_code")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim()
        .to_string();
    if plan_code.is_empty() {
        return subscription_error(
            "SUBSCRIPTION_PLAN_REQUIRED",
            "plan_code is required.",
            400,
            json!({}),
        );
    }
    let plan = match plan_row(pool, &plan_code).await {
        Ok(plan) => plan,
        Err(response) => return response,
    };
    let Some(plan) = plan else {
        return not_found();
    };
    if !plan.active {
        return subscription_error(
            "SUBSCRIPTION_PLAN_UNAVAILABLE",
            "The selected plan is not available.",
            400,
            json!({}),
        );
    }
    if plan.trial_days <= 0 {
        return subscription_error(
            "SUBSCRIPTION_PLAN_NO_TRIAL",
            "The selected plan does not offer a free trial.",
            400,
            json!({"plan_code": plan.code}),
        );
    }
    let active = sqlx::query_scalar::<_, String>(
        "SELECT CAST(id AS TEXT) FROM core_subscription WHERE CAST(tenant_id AS TEXT) = ? \
         AND status IN ('trialing', 'active', 'past_due') ORDER BY created_at DESC LIMIT 1",
    )
    .bind(&tenant_id)
    .fetch_optional(pool)
    .await;
    match active {
        Ok(Some(existing)) => {
            let existing_status = sqlx::query_scalar::<_, String>(
                "SELECT status FROM core_subscription WHERE CAST(id AS TEXT) = ? LIMIT 1",
            )
            .bind(&existing)
            .fetch_optional(pool)
            .await
            .ok()
            .flatten()
            .unwrap_or_default();
            return subscription_error(
                "SUBSCRIPTION_ALREADY_ACTIVE",
                "The tenant already has an active subscription.",
                409,
                json!({"existing_subscription": existing, "existing_status": existing_status}),
            );
        }
        Ok(None) => {}
        Err(error) => return database_error(error.to_string()),
    }
    let now = chrono::Utc::now();
    let start = now.format("%Y-%m-%d %H:%M:%S").to_string();
    let end = (now + chrono::Duration::days(plan.trial_days))
        .format("%Y-%m-%d %H:%M:%S")
        .to_string();
    let subscription_id = uuid::Uuid::new_v4().to_string();
    let insert = if is_postgres(&state) {
        "INSERT INTO core_subscription (id, tenant_id, plan_id, status, started_at, current_period_start, current_period_end, cancel_at_period_end, renewal_mode, created_at, updated_at) VALUES (?::uuid, ?::uuid, ?::uuid, 'trialing', ?::timestamptz, ?::timestamptz, ?::timestamptz, FALSE, 'manual', NOW(), NOW())"
    } else {
        "INSERT INTO core_subscription (id, tenant_id, plan_id, status, started_at, current_period_start, current_period_end, cancel_at_period_end, renewal_mode, created_at, updated_at) VALUES (?, ?, ?, 'trialing', ?, ?, ?, 0, 'manual', datetime('now'), datetime('now'))"
    };
    if let Err(error) = sqlx::query(insert)
        .bind(&subscription_id)
        .bind(&tenant_id)
        .bind(&plan.id)
        .bind(&start)
        .bind(&start)
        .bind(&end)
        .execute(pool)
        .await
    {
        return database_error(error.to_string());
    }
    if let Err(response) =
        set_budget_limits(&state, &tenant_id, plan.quota_daily, plan.quota_monthly).await
    {
        return response;
    }
    let metadata = json!({"plan_code": plan.code, "trial_days": plan.trial_days});
    if let Err(response) = append_billing_audit(
        &state,
        &tenant_id,
        "billing.subscription.trial_started",
        "subscription",
        &subscription_id,
        user.id,
        metadata,
    )
    .await
    {
        return response;
    }
    match subscription_payload(&state, pool, &subscription_id).await {
        Ok(Value::Null) => not_found(),
        Ok(value) => (StatusCode::CREATED, Json(json!({"subscription": value}))).into_response(),
        Err(response) => response,
    }
}

pub async fn cancel_subscription(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
    body: Result<Json<Value>, axum::extract::rejection::JsonRejection>,
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
    if !tenant_admin(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return admin_required();
    }
    let mode = body
        .ok()
        .and_then(|Json(value)| {
            value
                .get("mode")
                .and_then(Value::as_str)
                .map(str::trim)
                .map(str::to_string)
        })
        .filter(|mode| !mode.is_empty())
        .unwrap_or_else(|| "period_end".to_string());
    if mode != "immediate" && mode != "period_end" {
        return subscription_error(
            "SUBSCRIPTION_CANCEL_MODE_INVALID",
            "Cancel mode must be 'immediate' or 'period_end'.",
            400,
            json!({"mode": [mode]}),
        );
    }
    let subscription = match latest_subscription(&state, pool, &tenant_id).await {
        Ok(Some(subscription)) => subscription,
        Ok(None) => return not_found(),
        Err(error) => return database_error(error.to_string()),
    };
    if subscription.status == "cancelled" || subscription.status == "expired" {
        return subscription_error(
            "SUBSCRIPTION_INVALID_TRANSITION",
            &format!(
                "Subscription in status {} cannot be cancelled.",
                subscription.status
            ),
            409,
            json!({"status": subscription.status}),
        );
    }
    let now_expr = if is_postgres(&state) {
        "NOW()"
    } else {
        "datetime('now')"
    };
    if mode == "immediate" {
        let sql = format!(
            "UPDATE core_subscription SET status = 'cancelled', updated_at = {now_expr} \
             WHERE CAST(id AS TEXT) = ?"
        );
        if let Err(error) = sqlx::query(&sql).bind(&subscription.id).execute(pool).await {
            return database_error(error.to_string());
        }
    } else {
        let sql = format!(
            "UPDATE core_subscription SET cancel_at_period_end = {} , updated_at = {now_expr} \
             WHERE CAST(id AS TEXT) = ?",
            if is_postgres(&state) { "TRUE" } else { "1" }
        );
        if let Err(error) = sqlx::query(&sql).bind(&subscription.id).execute(pool).await {
            return database_error(error.to_string());
        }
    }
    let event_type = if mode == "immediate" {
        "billing.subscription.cancelled_immediately"
    } else {
        "billing.subscription.cancel_at_period_end"
    };
    let metadata = if mode == "immediate" {
        json!({"plan_code": subscription.plan_code})
    } else {
        json!({"plan_code": subscription.plan_code, "effective_at": subscription.current_period_end})
    };
    if let Err(response) = append_billing_audit(
        &state,
        &tenant_id,
        event_type,
        "subscription",
        &subscription.id,
        user.id,
        metadata,
    )
    .await
    {
        return response;
    }
    match subscription_payload(&state, pool, &subscription.id).await {
        Ok(Value::Null) => not_found(),
        Ok(value) => Json(json!({"subscription": value})).into_response(),
        Err(response) => response,
    }
}

pub async fn resume_subscription(
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
    if !tenant_admin(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return admin_required();
    }
    let subscription = match latest_subscription(&state, pool, &tenant_id).await {
        Ok(Some(subscription)) => subscription,
        Ok(None) => return not_found(),
        Err(error) => return database_error(error.to_string()),
    };
    if subscription.status == "cancelled" || subscription.status == "expired" {
        return subscription_error(
            "SUBSCRIPTION_INVALID_TRANSITION",
            &format!(
                "Subscription in status {} cannot be resumed.",
                subscription.status
            ),
            409,
            json!({"status": subscription.status}),
        );
    }
    if !subscription.cancel_at_period_end {
        return subscription_error(
            "SUBSCRIPTION_NOT_SCHEDULED_FOR_CANCEL",
            "This subscription is not scheduled for cancellation.",
            409,
            json!({"status": subscription.status}),
        );
    }
    let now_expr = if is_postgres(&state) {
        "NOW()"
    } else {
        "datetime('now')"
    };
    let sql = format!(
        "UPDATE core_subscription SET cancel_at_period_end = {}, updated_at = {now_expr} \
         WHERE CAST(id AS TEXT) = ?",
        if is_postgres(&state) { "FALSE" } else { "0" }
    );
    if let Err(error) = sqlx::query(&sql).bind(&subscription.id).execute(pool).await {
        return database_error(error.to_string());
    }
    let metadata = json!({"plan_code": subscription.plan_code, "current_period_end": subscription.current_period_end});
    if let Err(response) = append_billing_audit(
        &state,
        &tenant_id,
        "billing.subscription.resume_scheduled_cancel",
        "subscription",
        &subscription.id,
        user.id,
        metadata,
    )
    .await
    {
        return response;
    }
    match subscription_payload(&state, pool, &subscription.id).await {
        Ok(Value::Null) => not_found(),
        Ok(value) => Json(json!({"subscription": value})).into_response(),
        Err(response) => response,
    }
}

pub async fn settle_subscriptions(
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
    if !tenant_admin(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return admin_required();
    }
    let now = chrono::Utc::now().format("%Y-%m-%d %H:%M:%S").to_string();
    let rows = match sqlx::query(
        "SELECT CAST(id AS TEXT) AS id, CAST(plan_id AS TEXT) AS plan_id, status, \
         CAST(cancel_at_period_end AS INTEGER) AS cancel_int FROM core_subscription \
         WHERE CAST(tenant_id AS TEXT) = ? AND status IN ('trialing', 'active', 'past_due') \
         AND current_period_end <= ? ORDER BY created_at ASC",
    )
    .bind(&tenant_id)
    .bind(&now)
    .fetch_all(pool)
    .await
    {
        Ok(rows) => rows,
        Err(error) => return database_error(error.to_string()),
    };
    let mut settled = Vec::new();
    for row in rows {
        let id: String = row.try_get("id").unwrap_or_default();
        let plan_id: String = row.try_get("plan_id").unwrap_or_default();
        let cancel = row.try_get::<i64, _>("cancel_int").unwrap_or(0) != 0;
        let target = if cancel { "cancelled" } else { "expired" };
        let plan = match plan_limits(pool, &plan_id).await {
            Ok(Some(plan)) => plan,
            Ok(None) => continue,
            Err(error) => return database_error(error),
        };
        if let Err(response) = set_budget_limits(
            &state,
            &tenant_id,
            plan.quota_daily_expired,
            plan.quota_monthly_expired,
        )
        .await
        {
            return response;
        }
        let update = format!(
            "UPDATE core_subscription SET status = '{}', updated_at = {} WHERE CAST(id AS TEXT) = ?",
            target,
            if is_postgres(&state) { "NOW()" } else { "datetime('now')" }
        );
        if let Err(error) = sqlx::query(&update).bind(&id).execute(pool).await {
            return database_error(error.to_string());
        }
        let event_type = if cancel {
            "billing.subscription.cancelled_at_period_end"
        } else {
            "billing.subscription.expired"
        };
        let metadata = json!({"plan_code": plan.code});
        if let Err(response) = append_billing_audit(
            &state,
            &tenant_id,
            event_type,
            "subscription",
            &id,
            user.id,
            metadata,
        )
        .await
        {
            return response;
        }
        if let Ok(value) = subscription_payload(&state, pool, &id).await {
            settled.push(value);
        }
    }
    Json(json!({"count": settled.len(), "settled": settled})).into_response()
}

struct PlanRow {
    id: String,
    code: String,
    active: bool,
    trial_days: i64,
    quota_daily: i64,
    quota_monthly: i64,
}

struct PlanLimits {
    code: String,
    quota_daily_expired: i64,
    quota_monthly_expired: i64,
}

struct SubscriptionRow {
    id: String,
    plan_code: String,
    status: String,
    cancel_at_period_end: bool,
    current_period_end: Option<String>,
}

async fn plan_row(pool: &AnyPool, code: &str) -> Result<Option<PlanRow>, Response> {
    let row = sqlx::query(
        "SELECT CAST(id AS TEXT) AS id, code, CAST(active AS INTEGER) AS active_int, trial_days, \
         token_quota_daily, token_quota_monthly FROM core_plan WHERE code = ? LIMIT 1",
    )
    .bind(code)
    .fetch_optional(pool)
    .await
    .map_err(|error| database_error(error.to_string()))?;
    Ok(row.map(|row| PlanRow {
        id: row.try_get::<String, _>("id").unwrap_or_default(),
        code: row.try_get::<String, _>("code").unwrap_or_default(),
        active: row.try_get::<i64, _>("active_int").unwrap_or(0) != 0,
        trial_days: row.try_get::<i64, _>("trial_days").unwrap_or(0),
        quota_daily: row.try_get::<i64, _>("token_quota_daily").unwrap_or(0),
        quota_monthly: row.try_get::<i64, _>("token_quota_monthly").unwrap_or(0),
    }))
}

async fn plan_limits(pool: &AnyPool, plan_id: &str) -> Result<Option<PlanLimits>, String> {
    let row = sqlx::query(
        "SELECT code, token_quota_daily_expired, token_quota_monthly_expired FROM core_plan \
         WHERE CAST(id AS TEXT) = ? LIMIT 1",
    )
    .bind(plan_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())?;
    Ok(row.map(|row| PlanLimits {
        code: row.try_get::<String, _>("code").unwrap_or_default(),
        quota_daily_expired: row
            .try_get::<i64, _>("token_quota_daily_expired")
            .unwrap_or(0),
        quota_monthly_expired: row
            .try_get::<i64, _>("token_quota_monthly_expired")
            .unwrap_or(0),
    }))
}

async fn latest_subscription(
    state: &AppState,
    pool: &AnyPool,
    tenant_id: &str,
) -> Result<Option<SubscriptionRow>, String> {
    let period_end = timestamp(state, "subscription", "current_period_end");
    let sql = format!(
        "SELECT CAST(subscription.id AS TEXT) AS id, plan.code AS plan_code, subscription.status, \
         CAST(subscription.cancel_at_period_end AS INTEGER) AS cancel_int, \
         {period_end} AS period_end FROM core_subscription subscription \
         JOIN core_plan plan ON CAST(plan.id AS TEXT) = CAST(subscription.plan_id AS TEXT) \
         WHERE CAST(subscription.tenant_id AS TEXT) = ? ORDER BY subscription.created_at DESC LIMIT 1"
    );
    let row = sqlx::query(&sql)
        .bind(tenant_id)
        .fetch_optional(pool)
        .await
        .map_err(|error| error.to_string())?;
    Ok(row.map(|row| SubscriptionRow {
        id: row.try_get::<String, _>("id").unwrap_or_default(),
        plan_code: row.try_get::<String, _>("plan_code").unwrap_or_default(),
        status: row.try_get::<String, _>("status").unwrap_or_default(),
        cancel_at_period_end: row.try_get::<i64, _>("cancel_int").unwrap_or(0) != 0,
        current_period_end: raw_timestamp(&row, "period_end"),
    }))
}

async fn subscription_payload(
    state: &AppState,
    pool: &AnyPool,
    id: &str,
) -> Result<Value, Response> {
    let started = timestamp(state, "subscription", "started_at");
    let period_start = timestamp(state, "subscription", "current_period_start");
    let period_end = timestamp(state, "subscription", "current_period_end");
    let sql = format!(
        "SELECT CAST(subscription.id AS TEXT) AS id, plan.code AS plan_code, subscription.status, \
         {started} AS started, {period_start} AS period_start, {period_end} AS period_end, \
         CAST(subscription.cancel_at_period_end AS INTEGER) AS cancel_int, \
         subscription.renewal_mode FROM core_subscription subscription \
         JOIN core_plan plan ON CAST(plan.id AS TEXT) = CAST(subscription.plan_id AS TEXT) \
         WHERE CAST(subscription.id AS TEXT) = ? LIMIT 1"
    );
    let row = sqlx::query(&sql)
        .bind(id)
        .fetch_optional(pool)
        .await
        .map_err(|error| database_error(error.to_string()))?;
    let Some(row) = row else {
        return Ok(Value::Null);
    };
    let started_at: Option<String> = row.try_get("started").ok();
    let period_start_at: Option<String> = row.try_get("period_start").ok();
    let period_end_at: Option<String> = row.try_get("period_end").ok();
    let cancel = row.try_get::<i64, _>("cancel_int").unwrap_or(0) != 0;
    Ok(json!({
        "id": row.try_get::<String, _>("id").unwrap_or_default(),
        "plan": row.try_get::<String, _>("plan_code").unwrap_or_default(),
        "status": row.try_get::<String, _>("status").unwrap_or_default(),
        "started_at": started_at.as_deref().map(normalize_timestamp),
        "current_period_start": period_start_at.as_deref().map(normalize_timestamp),
        "current_period_end": period_end_at.as_deref().map(normalize_timestamp),
        "cancel_at_period_end": cancel,
        "renewal_mode": row.try_get::<String, _>("renewal_mode").unwrap_or_default(),
        "cancel_effective_at": if cancel { period_end_at.as_deref().map(normalize_timestamp) } else { None },
    }))
}

fn raw_timestamp(row: &sqlx::any::AnyRow, column: &str) -> Option<String> {
    row.try_get::<Option<String>, _>(column).ok().flatten()
}

async fn set_budget_limits(
    state: &AppState,
    tenant_id: &str,
    daily: i64,
    monthly: i64,
) -> Result<(), Response> {
    let Some(pool) = state.identity_pool.as_ref() else {
        return Err(unavailable());
    };
    let now_expr = if is_postgres(state) {
        "NOW()"
    } else {
        "datetime('now')"
    };
    let update = format!(
        "UPDATE core_tenanttokenbudget SET daily_limit = ?, monthly_limit = ?, updated_at = {now_expr} \
         WHERE CAST(tenant_id AS TEXT) = ?"
    );
    let updated = sqlx::query(&update)
        .bind(daily)
        .bind(monthly)
        .bind(tenant_id)
        .execute(pool)
        .await
        .map_err(|error| database_error(error.to_string()))?
        .rows_affected();
    if updated > 0 {
        return Ok(());
    }
    let insert = format!(
        "INSERT INTO core_tenanttokenbudget (id, tenant_id, daily_limit, monthly_limit, created_at, updated_at) \
         VALUES (?, ?, ?, ?, {now_expr}, {now_expr})"
    );
    sqlx::query(&insert)
        .bind(uuid::Uuid::new_v4().to_string())
        .bind(tenant_id)
        .bind(daily)
        .bind(monthly)
        .execute(pool)
        .await
        .map(|_| ())
        .map_err(|error| database_error(error.to_string()))
}

async fn append_billing_audit(
    state: &AppState,
    tenant_id: &str,
    event_type: &str,
    resource_type: &str,
    resource_id: &str,
    actor_id: i64,
    metadata: Value,
) -> Result<(), Response> {
    let Some(pool) = state.identity_pool.as_ref() else {
        return Err(unavailable());
    };
    let Some(database_url) = state.config.database_url.as_deref() else {
        return Ok(());
    };
    let event = crate::audit::AuditEvent {
        tenant_id: Some(tenant_id),
        project_id: None,
        event_type,
        resource_type,
        resource_id,
        outcome: "success",
        actor_id: Some(actor_id),
        request_id: "",
        remote_addr: None,
        user_agent: "",
        metadata,
    };
    crate::audit::record_event(pool, database_url, event)
        .await
        .map_err(|error| {
            tracing::error!(%error, "rust billing api could not append the subscription audit event");
            unavailable()
        })
}

fn admin_required() -> Response {
    (StatusCode::FORBIDDEN, Json(json!({"error": {"code": "TENANT_ADMIN_REQUIRED", "message": "Tenant owner or admin role is required to change payment credentials.", "details": {}}}))).into_response()
}

fn subscription_error(code: &str, message: &str, http_status: u16, details: Value) -> Response {
    (
        StatusCode::from_u16(http_status).unwrap_or(StatusCode::BAD_REQUEST),
        Json(json!({"error": {"code": code, "message": message, "details": details}})),
    )
        .into_response()
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
