use std::time::Duration;

use ai_agent_stack_api_edge::config::Config;
use ai_agent_stack_api_edge::identity::encode_session_data;
use ai_agent_stack_api_edge::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;

const SECRET: &str = "local-only-change-before-production";
const TENANT: &str = "11111111-1111-1111-1111-111111111111";
const SESSION: &str = "billing-session";
const TRIAL_TENANT: &str = "33333333-3333-3333-3333-333333333333";
const SETTLE_TENANT: &str = "44444444-4444-4444-4444-444444444444";

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-billing-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE django_session (session_key VARCHAR(40) PRIMARY KEY, session_data TEXT NOT NULL, expire_date DATETIME NOT NULL)",
        "CREATE TABLE auth_user (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, email VARCHAR(254) NOT NULL, is_staff INTEGER NOT NULL, is_active INTEGER NOT NULL)",
        "CREATE TABLE core_tenant (id TEXT PRIMARY KEY, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_tenantmembership (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_plan (id TEXT PRIMARY KEY, code VARCHAR(64) NOT NULL, name VARCHAR(120) NOT NULL, description TEXT NOT NULL, interval VARCHAR(16) NOT NULL, price_cents INTEGER NOT NULL, currency VARCHAR(8) NOT NULL, token_quota_daily INTEGER NOT NULL, token_quota_monthly INTEGER NOT NULL, token_quota_daily_expired INTEGER NOT NULL, token_quota_monthly_expired INTEGER NOT NULL, trial_days INTEGER NOT NULL, features TEXT NOT NULL, region_scope VARCHAR(16) NOT NULL, active BOOLEAN NOT NULL)",
        "CREATE TABLE core_subscription (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, plan_id TEXT NOT NULL, status VARCHAR(16) NOT NULL, started_at DATETIME, current_period_start DATETIME, current_period_end DATETIME, cancel_at_period_end BOOLEAN NOT NULL, renewal_mode VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_paymentchannelsecret (id TEXT PRIMARY KEY, channel VARCHAR(32) NOT NULL, slot VARCHAR(64) NOT NULL, sealed_value TEXT NOT NULL, updated_by_id INTEGER, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_tenanttokenbudget (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL UNIQUE, daily_limit INTEGER NOT NULL, monthly_limit INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_tenanttokenusage (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, status VARCHAR(16) NOT NULL, reserved_tokens INTEGER NOT NULL, consumed_tokens INTEGER NOT NULL, expires_at DATETIME NOT NULL, settled_at DATETIME, scope_ref TEXT, agent_run_id TEXT, workflow_run_id TEXT)",
        "CREATE TABLE core_entitlementgrant (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, order_id TEXT NOT NULL, subscription_id TEXT, period VARCHAR(16) NOT NULL, tokens INTEGER NOT NULL, expires_at DATETIME, reason TEXT NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_order (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, plan_id TEXT NOT NULL, status VARCHAR(16) NOT NULL, amount_cents INTEGER NOT NULL, refunded_cents INTEGER NOT NULL, currency VARCHAR(8) NOT NULL, channel VARCHAR(16) NOT NULL, provider_trade_no VARCHAR(128) NOT NULL, payment_instruction TEXT NOT NULL, paid_at DATETIME, idempotency_key VARCHAR(160) NOT NULL, created_by_id INTEGER, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_modelprovider (id TEXT PRIMARY KEY, tenant_id TEXT, name VARCHAR(120) NOT NULL, provider_type VARCHAR(32) NOT NULL, base_url VARCHAR(300) NOT NULL, credential_ref VARCHAR(300) NOT NULL, config TEXT NOT NULL, enabled BOOLEAN NOT NULL, deployment_region VARCHAR(32) NOT NULL)",
        "CREATE TABLE core_auditevent (id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, actor_id INTEGER, event_type VARCHAR(80) NOT NULL, resource_type VARCHAR(80) NOT NULL, resource_id VARCHAR(160) NOT NULL, outcome VARCHAR(16) NOT NULL, request_id VARCHAR(64) NOT NULL, remote_addr VARCHAR(64), user_agent VARCHAR(300) NOT NULL, metadata TEXT NOT NULL, previous_hash VARCHAR(64) NOT NULL, event_hash VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO auth_user (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1)")
        .execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenant (id, name, slug, created_at) VALUES (?, 'Acme', 'acme', datetime('now'))")
        .bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES (?, ?, 1, 'owner', datetime('now'))")
        .bind(uuid::Uuid::new_v4().to_string()).bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_plan (id, code, name, description, interval, price_cents, currency, token_quota_daily, token_quota_monthly, token_quota_daily_expired, token_quota_monthly_expired, trial_days, features, region_scope, active) VALUES ('plan-1', 'starter', 'Starter', 'Entry plan', 'monthly', 9900, 'CNY', 200000, 4000000, 0, 0, 14, '[\"3 projects\"]', 'domestic', 1)")
        .execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_plan (id, code, name, description, interval, price_cents, currency, token_quota_daily, token_quota_monthly, token_quota_daily_expired, token_quota_monthly_expired, trial_days, features, region_scope, active) VALUES ('plan-2', 'archived', 'Archived', 'Old plan', 'monthly', 19900, 'CNY', 1, 1, 0, 0, 0, '[]', 'domestic', 0)")
        .execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_subscription (id, tenant_id, plan_id, status, started_at, current_period_start, current_period_end, cancel_at_period_end, renewal_mode, created_at, updated_at) VALUES ('sub-1', ?, 'plan-1', 'active', datetime('now'), datetime('now'), datetime('now', '+30 days'), 0, 'manual', datetime('now'), datetime('now'))")
        .bind(TENANT).execute(&pool).await.unwrap();
    for tenant in [TRIAL_TENANT, SETTLE_TENANT] {
        sqlx::query("INSERT INTO core_tenant (id, name, slug, created_at) VALUES (?, 'Other', 'other', datetime('now'))")
            .bind(tenant).execute(&pool).await.unwrap();
        sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES (?, ?, 1, 'owner', datetime('now'))")
            .bind(uuid::Uuid::new_v4().to_string()).bind(tenant).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO core_subscription (id, tenant_id, plan_id, status, started_at, current_period_start, current_period_end, cancel_at_period_end, renewal_mode, created_at, updated_at) VALUES ('sub-due', ?, 'plan-1', 'active', datetime('now', '-60 days'), datetime('now', '-60 days'), datetime('now', '-1 day'), 1, 'manual', datetime('now', '-60 days'), datetime('now', '-60 days'))")
        .bind(SETTLE_TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenanttokenbudget (id, tenant_id, daily_limit, monthly_limit, created_at, updated_at) VALUES ('budget-1', ?, 1000, 5000, datetime('now'), datetime('now'))")
        .bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenanttokenusage (id, tenant_id, status, reserved_tokens, consumed_tokens, expires_at, settled_at, scope_ref) VALUES ('usage-1', ?, 'settled', 0, 18, datetime('now'), datetime('now'), 'agent-chat:thread-1')")
        .bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenanttokenusage (id, tenant_id, status, reserved_tokens, consumed_tokens, expires_at, settled_at, scope_ref) VALUES ('usage-2', ?, 'reserved', 7, 0, datetime('now', '+1 hour'), NULL, NULL)")
        .bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_order (id, tenant_id, plan_id, status, amount_cents, refunded_cents, currency, channel, provider_trade_no, payment_instruction, paid_at, idempotency_key, created_at, updated_at) VALUES ('order-1', ?, 'plan-1', 'pending', 9900, 0, 'CNY', 'alipay', 'trade-1', '{\"qr_code\":\"https://pay.example/qr\"}', NULL, 'idem-1', datetime('now'), datetime('now'))")
        .bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_entitlementgrant (id, tenant_id, order_id, period, tokens, reason, created_at) VALUES ('grant-1', ?, 'order-1', 'monthly', 500, 'purchase', datetime('now'))")
        .bind(TENANT).execute(&pool).await.unwrap();
    let data = encode_session_data(1, "password-hash", SECRET).unwrap();
    sqlx::query("INSERT INTO django_session (session_key, session_data, expire_date) VALUES (?, ?, datetime('now', '+1 day'))")
        .bind(SESSION).bind(data).execute(&pool).await.unwrap();
    pool.close().await;
    let config = Config::new(
        "127.0.0.1:0".into(),
        "http://127.0.0.1:1".into(),
        Duration::from_secs(2),
        Duration::from_secs(10),
    )
    .unwrap()
    .with_native_identity(true, Some(url), Some(SECRET.to_string()))
    .unwrap();
    AppState::new(config).unwrap()
}

async fn call(state: &AppState, uri: &str) -> (StatusCode, Value) {
    request(state, "GET", uri, None).await
}

async fn request(
    state: &AppState,
    method: &str,
    uri: &str,
    body: Option<Value>,
) -> (StatusCode, Value) {
    let request = Request::builder()
        .method(method)
        .uri(uri)
        .header(
            "cookie",
            format!("sessionid={SESSION}; csrftoken=0123456789abcdef0123456789abcdef"),
        )
        .header(
            "x-csrftoken",
            format!("{}{}", "a".repeat(32), "0123456789abcdef0123456789abcdef"),
        );
    let request = match body {
        Some(body) => request
            .header("content-type", "application/json")
            .body(Body::from(body.to_string()))
            .unwrap(),
        None => request.body(Body::empty()).unwrap(),
    };
    let response = router(state.clone()).oneshot(request).await.unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 4 * 1024 * 1024)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

#[tokio::test]
async fn lists_purchasable_plans_and_authoritative_subscription() {
    let state = state().await;
    let (status, body) = call(&state, &format!("/api/v1/tenants/{TENANT}/billing/plans/")).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let results = body["results"].as_array().unwrap();
    assert_eq!(results.len(), 1);
    assert_eq!(results[0]["code"], "starter");
    assert_eq!(results[0]["features"], json!(["3 projects"]));
    assert_eq!(results[0]["purchasable"], false);
    assert_eq!(results[0]["reason_code"], "PAYMENT_PROVIDER_NOT_CONFIGURED");

    let (status, body) = call(
        &state,
        &format!("/api/v1/tenants/{TENANT}/billing/subscription/"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["source"], "authoritative");
    assert_eq!(body["subscription"]["id"], "sub-1");
    assert_eq!(body["subscription"]["plan"], "starter");
    assert_eq!(body["subscription"]["status"], "active");
    assert_eq!(body["subscription"]["cancel_at_period_end"], false);
    assert!(body["subscription"]["current_period_end"].is_string());
}

#[tokio::test]
async fn reports_usage_ledger_grants_and_payment_capabilities() {
    let state = state().await;
    let (status, body) = call(&state, &format!("/api/v1/tenants/{TENANT}/billing/usage/")).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["daily_limit"], 1000);
    assert_eq!(body["monthly_limit"], 5000);
    assert_eq!(body["daily_used"], 25);
    assert_eq!(body["monthly_used"], 25);
    assert_eq!(body["remaining_daily"], 975);
    assert_eq!(body["remaining_monthly"], 4975);
    assert_eq!(body["limits_source"], "tenant_budget");
    assert_eq!(body["history"][0]["source"], "agent-chat:thread-1");
    assert_eq!(body["history"][0]["consumed_tokens"], 18);
    assert_eq!(body["grants"][0]["id"], "grant-1");
    assert_eq!(body["grants"][0]["tokens"], 500);

    let (status, body) = call(
        &state,
        &format!("/api/v1/tenants/{TENANT}/billing/capabilities/"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["payment"]["available"], false);
    assert_eq!(
        body["payment"]["reason_code"],
        "PAYMENT_PROVIDER_NOT_CONFIGURED"
    );
    let diagnostics = body["payment"]["diagnostics"].as_array().unwrap();
    assert_eq!(diagnostics.len(), 2);
    assert_eq!(diagnostics[0]["channel"], "alipay");
    assert_eq!(diagnostics[0]["missing"].as_array().unwrap().len(), 4);
}

#[tokio::test]
async fn reads_tenant_orders_and_payment_instructions() {
    let state = state().await;
    let (status, body) = call(&state, &format!("/api/v1/tenants/{TENANT}/billing/orders/")).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let results = body["results"].as_array().unwrap();
    assert_eq!(results.len(), 1);
    assert_eq!(results[0]["id"], "order-1");
    assert_eq!(results[0]["plan"], "starter");
    assert_eq!(results[0]["status"], "pending");
    assert_eq!(results[0]["amount_cents"], 9900);
    assert!(results[0].get("payment").is_none());

    let (status, body) = call(
        &state,
        &format!("/api/v1/tenants/{TENANT}/billing/orders/order-1/"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["payment"]["qr_code"], "https://pay.example/qr");
    assert_eq!(body["provider_trade_no"], "trade-1");

    let (status, body) = call(
        &state,
        &format!("/api/v1/tenants/{TENANT}/billing/orders/missing/"),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(body["detail"], "Not found.");
}

#[tokio::test]
async fn seals_and_reports_payment_channel_credentials() {
    let state = state().await;
    let uri = format!("/api/v1/tenants/{TENANT}/billing/payments/credentials/alipay/");
    let (status, body) = call(&state, &uri).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["channel"], "alipay");
    assert_eq!(body["configured"], false);
    assert_eq!(body["missing"].as_array().unwrap().len(), 4);
    assert_eq!(body["filled"].as_array().unwrap().len(), 0);

    let (status, body) = request(
        &state,
        "PUT",
        &uri,
        Some(json!({"values": {
            "app_id": "2021000000000000",
            "private_key": "private-pem",
            "public_key": "public-pem",
            "notify_url": "https://example.com/callback",
        }})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["configured"], true);
    assert_eq!(body["filled"].as_array().unwrap().len(), 4);
    assert_eq!(body["missing"].as_array().unwrap().len(), 0);

    let (status, body) = request(
        &state,
        "PUT",
        &format!("/api/v1/tenants/{TENANT}/billing/payments/credentials/unknown/"),
        Some(json!({"values": {"app_id": "x"}})),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{body}");
    assert_eq!(body["error"]["code"], "PAYMENT_CHANNEL_NOT_SUPPORTED");

    let (status, body) = request(
        &state,
        "PUT",
        &uri,
        Some(json!({"values": {"not_a_slot": "x"}})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"]["code"], "INVALID_CREDENTIALS");
}

#[tokio::test]
async fn runs_subscription_trial_cancel_resume_and_settlement() {
    let state = state().await;
    let trial_uri = format!("/api/v1/tenants/{TRIAL_TENANT}/billing/subscription/trial/");
    let (status, body) = request(
        &state,
        "POST",
        &trial_uri,
        Some(json!({"plan_code": "starter"})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED, "{body}");
    assert_eq!(body["subscription"]["status"], "trialing");
    assert_eq!(body["subscription"]["plan"], "starter");
    assert!(body["subscription"]["current_period_end"].is_string());

    let (status, body) = request(
        &state,
        "POST",
        &trial_uri,
        Some(json!({"plan_code": "starter"})),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT, "{body}");
    assert_eq!(body["error"]["code"], "SUBSCRIPTION_ALREADY_ACTIVE");

    let cancel_uri = format!("/api/v1/tenants/{TENANT}/billing/subscription/cancel/");
    let (status, body) = request(
        &state,
        "POST",
        &cancel_uri,
        Some(json!({"mode": "period_end"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["subscription"]["cancel_at_period_end"], true);
    assert!(body["subscription"]["cancel_effective_at"].is_string());

    let resume_uri = format!("/api/v1/tenants/{TENANT}/billing/subscription/resume/");
    let (status, body) = request(&state, "POST", &resume_uri, None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["subscription"]["cancel_at_period_end"], false);

    let (status, body) = request(&state, "POST", &resume_uri, None).await;
    assert_eq!(status, StatusCode::CONFLICT, "{body}");
    assert_eq!(
        body["error"]["code"],
        "SUBSCRIPTION_NOT_SCHEDULED_FOR_CANCEL"
    );

    let (status, body) = request(
        &state,
        "POST",
        &cancel_uri,
        Some(json!({"mode": "immediate"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["subscription"]["status"], "cancelled");

    let settle_uri = format!("/api/v1/tenants/{SETTLE_TENANT}/billing/subscription/settle/");
    let (status, body) = request(&state, "POST", &settle_uri, None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["count"], 1);
    assert_eq!(body["settled"][0]["status"], "cancelled");
    let (_, budget) = call(
        &state,
        &format!("/api/v1/tenants/{SETTLE_TENANT}/billing/usage/"),
    )
    .await;
    assert_eq!(budget["daily_limit"], 0);
    assert_eq!(budget["monthly_limit"], 0);
}

#[tokio::test]
async fn renews_subscription_from_a_paid_order() {
    let state = state().await;
    let pool = state.identity_pool.as_ref().unwrap();
    sqlx::query("INSERT INTO core_order (id, tenant_id, plan_id, status, amount_cents, refunded_cents, currency, channel, provider_trade_no, payment_instruction, paid_at, idempotency_key, created_at, updated_at) VALUES ('order-renew', ?, 'plan-1', 'paid', 9900, 0, 'CNY', 'alipay', 'trade-renew', '{}', datetime('now'), 'idem-renew', datetime('now'), datetime('now'))")
        .bind(TENANT).execute(pool).await.unwrap();
    let (status, body) = request(
        &state,
        "POST",
        &format!("/api/v1/tenants/{TENANT}/billing/subscription/renew/"),
        Some(json!({"order_id": "order-renew"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["grant"]["tokens"], 4000000);
    assert_eq!(body["subscription"]["status"], "active");
    let grant = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_entitlementgrant WHERE order_id = 'order-renew'",
    )
    .fetch_one(pool)
    .await
    .unwrap();
    assert_eq!(grant, 1);
    let limits = sqlx::query_as::<_, (i64, i64)>(
        "SELECT daily_limit, monthly_limit FROM core_tenanttokenbudget WHERE CAST(tenant_id AS TEXT) = ?",
    )
    .bind(TENANT)
    .fetch_one(pool)
    .await
    .unwrap();
    assert_eq!(limits, (200000, 4000000));
    let (status, body) = request(
        &state,
        "POST",
        &format!("/api/v1/tenants/{TENANT}/billing/subscription/renew/"),
        Some(json!({"order_id": "order-renew"})),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["error"]["code"], "SUBSCRIPTION_GRANT_ALREADY_ISSUED");
}

#[tokio::test]
async fn creates_order_rejects_unconfigured_payment_provider() {
    let state = state().await;
    let (status, body) = request(
        &state,
        "POST",
        &format!("/api/v1/tenants/{TENANT}/billing/orders/"),
        Some(
            json!({"plan_code": "starter", "channel": "wechat", "idempotency_key": "order-key-1"}),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT, "{body}");
    assert_eq!(body["error"]["code"], "PAYMENT_PROVIDER_NOT_CONFIGURED");

    let (status, body) = request(
        &state,
        "POST",
        &format!("/api/v1/tenants/{TENANT}/billing/orders/"),
        Some(
            json!({"plan_code": "starter", "channel": "stripe", "idempotency_key": "order-key-2"}),
        ),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"]["code"], "PAYMENT_CHANNEL_NOT_SUPPORTED");
}

#[tokio::test]
async fn creates_order_with_fake_provider_when_enabled() {
    if std::env::var("BILLING_FAKE_PROVIDER_ENABLED").as_deref() != Ok("true") {
        return;
    }
    let state = state().await;
    let (status, body) = request(
        &state,
        "POST",
        &format!("/api/v1/tenants/{TENANT}/billing/orders/"),
        Some(json!({"plan_code": "starter", "channel": "wechat", "idempotency_key": "fake-key-1"})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED, "{body}");
    assert_eq!(body["status"], "pending");
    assert_eq!(body["amount_cents"], 9900);
    assert_eq!(body["payment"]["provider"], "fake");
    assert!(body["provider_trade_no"]
        .as_str()
        .unwrap()
        .starts_with("fake-"));

    let (status, body) = request(
        &state,
        "POST",
        &format!("/api/v1/tenants/{TENANT}/billing/orders/"),
        Some(json!({"plan_code": "starter", "channel": "wechat", "idempotency_key": "fake-key-1"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["idempotency_key"], "fake-key-1");
}

#[tokio::test]
async fn reports_order_status_and_refuses_unconfigured_close() {
    let state = state().await;
    let (status, body) = call(
        &state,
        &format!("/api/v1/tenants/{TENANT}/billing/orders/order-1/status/"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["status"], "pending");
    assert_eq!(body["provider_status"], "unknown");
    assert_eq!(body["payment"]["qr_code"], "https://pay.example/qr");

    let (status, body) = request(
        &state,
        "POST",
        &format!("/api/v1/tenants/{TENANT}/billing/orders/order-1/close/"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT, "{body}");
    assert_eq!(body["error"]["code"], "PAYMENT_PROVIDER_NOT_CONFIGURED");
}
