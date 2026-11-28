#![allow(clippy::result_large_err)]
//! Native refund, provider callback and reconciliation endpoints.

use std::collections::BTreeMap;
use std::collections::BTreeSet;
use std::env;

use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Value};

use crate::billing_api::{require_user, tenant_admin, tenant_member};
use crate::proxy::AppState;
use sqlx::Row;

fn database_error(message: String) -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({"error": {"code": "DATABASE_UNAVAILABLE", "message": message}})),
    )
        .into_response()
}

fn order_error(status: StatusCode, code: &str, message: &str) -> Response {
    (
        status,
        Json(json!({"error": {"code": code, "message": message, "details": {}}})),
    )
        .into_response()
}

fn not_found() -> Response {
    order_error(StatusCode::NOT_FOUND, "NOT_FOUND", "Not found.")
}

fn forbidden() -> Response {
    order_error(
        StatusCode::FORBIDDEN,
        "ADMIN_REQUIRED",
        "Tenant admin required.",
    )
}

fn fake_provider_enabled() -> bool {
    env::var("BILLING_FAKE_PROVIDER_ENABLED")
        .map(|value| {
            matches!(
                value.trim().to_ascii_lowercase().as_str(),
                "1" | "true" | "yes"
            )
        })
        .unwrap_or(false)
}

fn now_expr(state: &AppState) -> &'static str {
    if state
        .config
        .database_url
        .as_deref()
        .unwrap_or("")
        .starts_with("postgres")
    {
        "NOW()"
    } else {
        "datetime('now')"
    }
}

async fn append_audit(
    state: &AppState,
    tenant_id: &str,
    actor_id: i64,
    event_type: &str,
    resource_id: &str,
    metadata: Value,
) {
    let Some(pool) = state.identity_pool.as_ref() else {
        return;
    };
    let _ = crate::audit::record_event(
        pool,
        state.config.database_url.as_deref().unwrap_or(""),
        crate::audit::AuditEvent {
            tenant_id: Some(tenant_id),
            project_id: None,
            event_type,
            resource_type: "order",
            resource_id,
            outcome: "success",
            actor_id: Some(actor_id),
            request_id: "",
            remote_addr: None,
            user_agent: "",
            metadata,
        },
    )
    .await;
}

pub async fn refund_order(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, order_id)): Path<(String, String)>,
    body: Option<Json<Value>>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return database_error("Identity pool is not configured.".into());
    };
    match tenant_admin(pool, &tenant_id, user.id).await {
        Ok(true) => {}
        Ok(false) => return forbidden(),
        Err(error) => return database_error(error),
    }
    let payload = body.map(|Json(value)| value).unwrap_or_else(|| json!({}));
    let reason = payload
        .get("reason")
        .and_then(Value::as_str)
        .unwrap_or("refund")
        .trim()
        .chars()
        .take(160)
        .collect::<String>();
    let requested = match payload.get("amount_cents") {
        None | Some(Value::Null) => None,
        Some(value) => {
            let parsed = value.as_i64().or_else(|| {
                value
                    .as_str()
                    .and_then(|text| text.trim().parse::<i64>().ok())
            });
            match parsed {
                Some(cents) if cents > 0 => Some(cents),
                _ => {
                    return order_error(
                        StatusCode::BAD_REQUEST,
                        "REFUND_AMOUNT_INVALID",
                        "amount_cents must be an integer.",
                    )
                }
            }
        }
    };
    let sql = "SELECT order_row.status, order_row.amount_cents, order_row.refunded_cents, \
               order_row.currency, order_row.channel, order_row.provider_trade_no, \
               plan.code AS plan_code FROM core_order order_row \
               JOIN core_plan plan ON CAST(plan.id AS TEXT) = CAST(order_row.plan_id AS TEXT) \
               WHERE CAST(order_row.tenant_id AS TEXT) = ? \
                 AND CAST(order_row.id AS TEXT) = ? LIMIT 1";
    let row = match sqlx::query(sql)
        .bind(&tenant_id)
        .bind(&order_id)
        .fetch_optional(pool)
        .await
    {
        Ok(Some(row)) => row,
        Ok(None) => return not_found(),
        Err(error) => return database_error(error.to_string()),
    };
    let status: String = row.try_get("status").unwrap_or_default();
    let amount: i64 = row.try_get("amount_cents").unwrap_or(0);
    let refunded: i64 = row.try_get("refunded_cents").unwrap_or(0);
    let channel: String = row.try_get("channel").unwrap_or_default();
    if !matches!(status.as_str(), "paid" | "partial_refunded") {
        return order_error(
            StatusCode::CONFLICT,
            "ORDER_STATE_CONFLICT",
            &format!("Order in status {status} cannot be refunded."),
        );
    }
    let refundable = amount - refunded;
    let cents = requested.unwrap_or(refundable);
    if cents > refundable {
        return order_error(
            StatusCode::BAD_REQUEST,
            "REFUND_AMOUNT_EXCEEDED",
            "Refund amount exceeds the remaining refundable balance.",
        );
    }
    // The fake provider is the only channel adapter available without merchant
    // credentials; refuse rather than pretend a real channel settled the money.
    if !(fake_provider_enabled() && channel == "wechat") {
        return order_error(
            StatusCode::BAD_GATEWAY,
            "PAYMENT_PROVIDER_NOT_CONFIGURED",
            "No native payment provider is configured for this channel.",
        );
    }
    // A replay of the same refund must not deduct the budget twice: the
    // reference is derived from the order and the total refunded after this
    // request, so retrying the same refund resolves to the same reclaim row.
    let after = refunded + cents;
    let reference = format!("refund-{order_id}-{after}");
    let target = if after >= amount {
        "refunded"
    } else {
        "partial_refunded"
    };
    let replayed = sqlx::query_scalar::<_, i64>(
        "SELECT 1 FROM core_entitlementreclaim WHERE refund_reference = ? LIMIT 1",
    )
    .bind(&reference)
    .fetch_optional(pool)
    .await
    .ok()
    .flatten()
    .is_some();
    if replayed {
        return Json(json!({
            "id": order_id,
            "tenant": tenant_id,
            "status": target,
            "amount_cents": amount,
            "refunded_cents": after,
            "reclaim": "existing",
        }))
        .into_response();
    }
    let now = now_expr(&state);
    let update = format!(
        "UPDATE core_order SET refunded_cents = ?, status = ?, updated_at = {now} \
         WHERE CAST(id AS TEXT) = ? AND CAST(tenant_id AS TEXT) = ?"
    );
    if let Err(error) = sqlx::query(&update)
        .bind(after)
        .bind(target)
        .bind(&order_id)
        .bind(&tenant_id)
        .execute(pool)
        .await
    {
        return database_error(error.to_string());
    }
    // Reclaim a proportional slice of the plan grant against the tenant budget.
    let grant: Option<(String, i64)> = match sqlx::query(
        "SELECT CAST(grant.id AS TEXT) AS grant_id, grant.tokens FROM core_entitlementgrant grant \
         WHERE CAST(grant.order_id AS TEXT) = ? LIMIT 1",
    )
    .bind(&order_id)
    .fetch_optional(pool)
    .await
    {
        Ok(Some(row)) => Some((
            row.try_get("grant_id").unwrap_or_default(),
            row.try_get("tokens").unwrap_or(0),
        )),
        _ => None,
    };
    // Already reclaimed tokens are summed per grant, so a partial refund
    // series can never reclaim more than the grant ever issued.
    let already: i64 = match grant.as_ref() {
        Some((grant_id, _)) => sqlx::query_scalar::<_, i64>(
            "SELECT COALESCE(SUM(tokens), 0) FROM core_entitlementreclaim \
             WHERE CAST(grant_id AS TEXT) = ?",
        )
        .bind(grant_id)
        .fetch_one(pool)
        .await
        .unwrap_or_default(),
        None => 0,
    };
    let tokens = match grant.as_ref() {
        Some((_, grant_tokens)) if amount > 0 => {
            let raw = grant_tokens.saturating_mul(cents) / amount;
            raw.min((grant_tokens - already).max(0))
        }
        _ => 0,
    };
    let before: i64 = match sqlx::query_scalar::<_, i64>(
        "SELECT CAST(monthly_limit AS BIGINT) FROM core_tenanttokenbudget \
         WHERE CAST(tenant_id AS TEXT) = ? LIMIT 1",
    )
    .bind(&tenant_id)
    .fetch_optional(pool)
    .await
    {
        Ok(Some(value)) => value,
        _ => 1_000_000,
    };
    let limit_after = (before - tokens).max(0);
    let insert = format!(
        "INSERT INTO core_entitlementreclaim \
         (id, tenant_id, grant_id, order_id, refund_reference, refund_cents, tokens, \
          monthly_limit_before, monthly_limit_after, reason, created_at) \
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {now})"
    );
    let inserted = match sqlx::query(&insert)
        .bind(uuid::Uuid::new_v4().simple().to_string())
        .bind(&tenant_id)
        .bind(grant.as_ref().map(|(id, _)| id.clone()))
        .bind(&order_id)
        .bind(&reference)
        .bind(cents)
        .bind(tokens)
        .bind(before)
        .bind(limit_after)
        .bind(&reason)
        .execute(pool)
        .await
    {
        Ok(_) => true,
        Err(error) => {
            // A concurrent replay already wrote this reclaim row; its ledger
            // entry is authoritative and the budget must not move again.
            tracing::warn!(%error, %order_id, "refund reclaim insert failed");
            false
        }
    };
    if !inserted {
        return Json(json!({
            "id": order_id,
            "tenant": tenant_id,
            "status": target,
            "amount_cents": amount,
            "refunded_cents": after,
            "reclaim": "existing",
        }))
        .into_response();
    }
    let budget_update = format!(
        "UPDATE core_tenanttokenbudget SET monthly_limit = ?, updated_at = {now} \
         WHERE CAST(tenant_id AS TEXT) = ?"
    );
    let _ = sqlx::query(&budget_update)
        .bind(limit_after)
        .bind(&tenant_id)
        .execute(pool)
        .await;
    append_audit(
        &state,
        &tenant_id,
        user.id,
        "billing.order.refunded",
        &order_id,
        json!({"reason": reason, "refund_cents": cents}),
    )
    .await;
    Json(json!({
        "id": order_id,
        "tenant": tenant_id,
        "status": target,
        "amount_cents": amount,
        "refunded_cents": after,
        "channel": channel,
        "refund_reference": reference,
        "payment": {"provider": "fake", "refund_cents": cents, "status": "refunded"},
    }))
    .into_response()
}

fn parse_day(value: &str) -> Option<chrono::NaiveDate> {
    chrono::NaiveDate::parse_from_str(value.trim(), "%Y-%m-%d").ok()
}

pub async fn reconciliation(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
    Query(params): Query<BTreeMap<String, String>>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return database_error("Identity pool is not configured.".into());
    };
    if !tenant_member(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return not_found();
    }
    match tenant_admin(pool, &tenant_id, user.id).await {
        Ok(true) => {}
        Ok(false) => return forbidden(),
        Err(error) => return database_error(error),
    }
    let start_raw = params.get("start").cloned().unwrap_or_default();
    let end_raw = params.get("end").cloned().unwrap_or_default();
    let (Some(start), Some(end)) = (parse_day(&start_raw), parse_day(&end_raw)) else {
        return order_error(
            StatusCode::BAD_REQUEST,
            "RECONCILIATION_RANGE_INVALID",
            "dates must use YYYY-MM-DD format",
        );
    };
    if end < start {
        return order_error(
            StatusCode::BAD_REQUEST,
            "RECONCILIATION_RANGE_INVALID",
            "end date precedes start date",
        );
    }
    let start_bound = format!("{} 00:00:00", start.format("%Y-%m-%d"));
    let end_bound = format!(
        "{} 00:00:00",
        (end + chrono::Duration::days(1)).format("%Y-%m-%d")
    );
    let sql = "SELECT order_row.id, order_row.status, order_row.channel, order_row.amount_cents, \
               order_row.refunded_cents FROM core_order order_row \
               WHERE CAST(order_row.tenant_id AS TEXT) = ? \
                 AND order_row.created_at >= ? AND order_row.created_at < ? \
               ORDER BY order_row.created_at";
    let rows = match sqlx::query(sql)
        .bind(&tenant_id)
        .bind(&start_bound)
        .bind(&end_bound)
        .fetch_all(pool)
        .await
    {
        Ok(rows) => rows,
        Err(error) => return database_error(error.to_string()),
    };
    let mut status_counts = serde_json::Map::new();
    let mut channels: BTreeMap<String, (i64, i64, i64)> = BTreeMap::new();
    for row in &rows {
        let status: String = row.try_get("status").unwrap_or_default();
        let channel: String = row.try_get("channel").unwrap_or_default();
        let amount: i64 = row.try_get("amount_cents").unwrap_or(0);
        let refunded: i64 = row.try_get("refunded_cents").unwrap_or(0);
        let count = status_counts
            .get(&status)
            .and_then(Value::as_i64)
            .unwrap_or(0);
        status_counts.insert(status, json!(count + 1));
        let entry = channels.entry(channel).or_insert((0, 0, 0));
        entry.0 += 1;
        entry.1 += amount;
        entry.2 += refunded;
    }
    let mut channel_amounts = serde_json::Map::new();
    for (channel, (order_count, amount, refunded)) in channels {
        channel_amounts.insert(
            channel,
            json!({"order_count": order_count, "amount_cents": amount, "refunded_cents": refunded}),
        );
    }
    let refund_slices: i64 = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_entitlementreclaim reclaim \
         JOIN core_order order_row ON CAST(order_row.id AS TEXT) = CAST(reclaim.order_id AS TEXT) \
         WHERE CAST(order_row.tenant_id AS TEXT) = ? \
           AND reclaim.created_at >= ? AND reclaim.created_at < ?",
    )
    .bind(&tenant_id)
    .bind(&start_bound)
    .bind(&end_bound)
    .fetch_one(pool)
    .await
    .unwrap_or_default();
    let configured = crate::billing_api::payment_providers(&state).await;
    let mut unavailable: BTreeSet<String> = BTreeSet::new();
    for row in &rows {
        let channel: String = row.try_get("channel").unwrap_or_default();
        if !configured.iter().any(|item| item == &channel) {
            unavailable.insert(channel);
        }
    }
    Json(json!({
        "start": start_raw,
        "end": end_raw,
        "order_count": rows.len(),
        "status_counts": status_counts,
        "channel_amounts": channel_amounts,
        "refund_slice_count": refund_slices,
        "unavailable_channels": unavailable.into_iter().collect::<Vec<_>>(),
        "discrepancies": [],
        "refund_discrepancies": [],
    }))
    .into_response()
}

/// Provider callback: the fake adapter verifies a shared test signature and
/// settles the order. Real WeChat/Alipay adapters report a configuration error
/// instead of accepting an unsigned webhook.
pub async fn payment_callback(
    State(state): State<AppState>,
    Path((tenant_id, channel)): Path<(String, String)>,
    headers: HeaderMap,
    body: axum::body::Bytes,
) -> Response {
    if !matches!(channel.as_str(), "wechat" | "alipay") {
        return order_error(
            StatusCode::BAD_REQUEST,
            "PAYMENT_CHANNEL_NOT_SUPPORTED",
            &format!("Channel {channel:?} is not supported."),
        );
    }
    if !(fake_provider_enabled() && channel == "wechat") {
        return order_error(
            StatusCode::SERVICE_UNAVAILABLE,
            "PAYMENT_PROVIDER_NOT_CONFIGURED",
            "No payment provider is configured for this channel.",
        );
    }
    let signature = headers
        .get("x-fake-signature")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("");
    if signature != "fake-ok" {
        return order_error(
            StatusCode::BAD_REQUEST,
            "PAYMENT_SIGNATURE_INVALID",
            "Callback signature verification failed.",
        );
    }
    let payload: Value = match serde_json::from_slice(&body) {
        Ok(value) => value,
        Err(error) => {
            return order_error(
                StatusCode::BAD_REQUEST,
                "PAYMENT_SIGNATURE_INVALID",
                &format!("body decode failed: {error}"),
            )
        }
    };
    let status = payload.get("status").and_then(Value::as_str).unwrap_or("");
    if !matches!(status, "paid" | "failed" | "closed") {
        return order_error(
            StatusCode::BAD_REQUEST,
            "PAYMENT_SIGNATURE_INVALID",
            &format!("unknown status: {status}"),
        );
    }
    let trade_no = payload
        .get("out_trade_no")
        .and_then(Value::as_str)
        .unwrap_or("");
    let Some(pool) = state.identity_pool.as_ref() else {
        return database_error("Identity pool is not configured.".into());
    };
    let row = match sqlx::query(
        "SELECT CAST(order_row.id AS TEXT) AS id, order_row.status, \
         CAST(order_row.plan_id AS TEXT) AS plan_id, plan.token_quota_monthly, plan.token_quota_daily \
         FROM core_order order_row \
         JOIN core_plan plan ON CAST(plan.id AS TEXT) = CAST(order_row.plan_id AS TEXT) \
         WHERE CAST(order_row.tenant_id AS TEXT) = ? AND order_row.channel = ? \
           AND order_row.provider_trade_no = ? LIMIT 1",
    )
    .bind(&tenant_id)
    .bind(&channel)
    .bind(trade_no)
    .fetch_optional(pool)
    .await
    {
        Ok(Some(row)) => row,
        Ok(None) => return not_found(),
        Err(error) => return database_error(error.to_string()),
    };
    let order_id: String = row.try_get("id").unwrap_or_default();
    let local_status: String = row.try_get("status").unwrap_or_default();
    if local_status == status {
        return Json(json!({"status": status, "order_id": order_id})).into_response();
    }
    // Terminal orders never move again, except that a paid order may later
    // be refunded through the dedicated refund endpoint.
    if matches!(
        local_status.as_str(),
        "refunded" | "partial_refunded" | "failed" | "closed"
    ) && status != "paid"
    {
        return order_error(
            StatusCode::CONFLICT,
            "ORDER_STATE_CONFLICT",
            &format!("Order in status {local_status} cannot transition to {status}."),
        );
    }
    let now = now_expr(&state);
    let update = format!(
        "UPDATE core_order SET status = ?, paid_at = CASE WHEN ? = 'paid' THEN {now} ELSE paid_at END, \
         updated_at = {now} WHERE CAST(id AS TEXT) = ? AND CAST(tenant_id AS TEXT) = ?"
    );
    if let Err(error) = sqlx::query(&update)
        .bind(status)
        .bind(status)
        .bind(&order_id)
        .bind(&tenant_id)
        .execute(pool)
        .await
    {
        return database_error(error.to_string());
    }
    if status == "paid" {
        let tokens: i64 = row.try_get("token_quota_monthly").unwrap_or(0);
        let daily: i64 = row.try_get("token_quota_daily").unwrap_or(0);
        let existing: i64 = sqlx::query_scalar::<_, i64>(
            "SELECT COUNT(*) FROM core_entitlementgrant WHERE CAST(order_id AS TEXT) = ?",
        )
        .bind(&order_id)
        .fetch_one(pool)
        .await
        .unwrap_or_default();
        if existing == 0 {
            let grant_id = uuid::Uuid::new_v4().simple().to_string();
            let expires = if now == "NOW()" {
                "NOW() + INTERVAL '30 days'".to_string()
            } else {
                "datetime('now', '+30 days')".to_string()
            };
            let insert_grant = format!(
                "INSERT INTO core_entitlementgrant (id, tenant_id, order_id, subscription_id, \
                 period, tokens, expires_at, reason, created_at) \
                 VALUES (?, ?, ?, NULL, 'monthly', ?, {expires}, ?, {now})"
            );
            let _ = sqlx::query(&insert_grant)
                .bind(&grant_id)
                .bind(&tenant_id)
                .bind(&order_id)
                .bind(tokens)
                .bind(format!("order:{order_id}"))
                .execute(pool)
                .await;
            let budget = format!(
                "UPDATE core_tenanttokenbudget SET monthly_limit = monthly_limit + ?, \
                 daily_limit = CASE WHEN daily_limit < ? THEN ? ELSE daily_limit END, updated_at = {now} \
                 WHERE CAST(tenant_id AS TEXT) = ?"
            );
            let _ = sqlx::query(&budget)
                .bind(tokens)
                .bind(daily)
                .bind(daily)
                .bind(&tenant_id)
                .execute(pool)
                .await;
        }
    }
    append_audit(
        &state,
        &tenant_id,
        0,
        "billing.callback.settled",
        &order_id,
        json!({"channel": channel, "status": status}),
    )
    .await;
    Json(json!({"status": status, "order_id": order_id})).into_response()
}
