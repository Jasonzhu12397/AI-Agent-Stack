use ai_agent_stack_api::config::Config;
use ai_agent_stack_api::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::Value;
use tower::ServiceExt;

const SECRET: &str = "billing-ext-test-secret";
const SESSION: &str = "billing-session";
const TENANT: &str = "tenant-billing";
const PLAN: &str = "plan-pro";
const PAID_ORDER: &str = "order-paid";
const PENDING_ORDER: &str = "order-pending";

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("billing-ext-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE platform_users (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, email VARCHAR(254) NOT NULL DEFAULT '', is_staff INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1)",
        "CREATE TABLE platform_credentials (user_id INTEGER NOT NULL, credential_type VARCHAR(32) NOT NULL, credential_hash TEXT NOT NULL)",
        "CREATE TABLE rust_sessions (token_hash CHAR(64) PRIMARY KEY, user_id INTEGER NOT NULL, expires_at DATETIME NOT NULL, created_at DATETIME NOT NULL, revoked_at DATETIME)",
        "CREATE TABLE platform_session_activity (token_hash CHAR(64) PRIMARY KEY, user_id INTEGER NOT NULL, created_at DATETIME NOT NULL, last_activity DATETIME NOT NULL, revoked_at DATETIME)",
        "CREATE TABLE core_tenant (id TEXT PRIMARY KEY, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_tenantmembership (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_plan (id TEXT PRIMARY KEY, code VARCHAR(32) NOT NULL UNIQUE, name VARCHAR(120) NOT NULL, token_quota_daily INTEGER NOT NULL, token_quota_monthly INTEGER NOT NULL, interval VARCHAR(16) NOT NULL DEFAULT 'monthly', amount_cents INTEGER NOT NULL DEFAULT 0, currency VARCHAR(8) NOT NULL DEFAULT 'CNY')",
        "CREATE TABLE core_order (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, plan_id TEXT NOT NULL, status VARCHAR(20) NOT NULL, amount_cents INTEGER NOT NULL, refunded_cents INTEGER NOT NULL DEFAULT 0, currency VARCHAR(8) NOT NULL, channel VARCHAR(16) NOT NULL, provider_trade_no VARCHAR(128) NOT NULL DEFAULT '', payment_instruction TEXT, paid_at DATETIME, idempotency_key VARCHAR(128) NOT NULL DEFAULT '', created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_tenanttokenbudget (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, daily_limit INTEGER NOT NULL, monthly_limit INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_entitlementgrant (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, subscription_id TEXT, order_id TEXT, period VARCHAR(16) NOT NULL, tokens INTEGER NOT NULL, expires_at DATETIME, reason VARCHAR(160) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_entitlementreclaim (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, grant_id TEXT, order_id TEXT NOT NULL, refund_reference VARCHAR(160) NOT NULL UNIQUE, refund_cents INTEGER NOT NULL, tokens INTEGER NOT NULL, monthly_limit_before INTEGER NOT NULL, monthly_limit_after INTEGER NOT NULL, reason VARCHAR(160) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_auditevent (id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, actor_id INTEGER, event_type VARCHAR(120) NOT NULL, resource_type VARCHAR(80) NOT NULL, resource_id TEXT, outcome VARCHAR(16) NOT NULL, request_id VARCHAR(64) NOT NULL, remote_addr VARCHAR(39), user_agent TEXT NOT NULL, metadata TEXT NOT NULL, previous_hash VARCHAR(64), event_hash VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO platform_users (id, username, email, is_staff, is_active) VALUES (1, 'admin', 'admin@example.com', 0, 1)")
        .execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenant (id, name, slug, created_at, updated_at) VALUES (?, 'T', 't', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
        .bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES ('m1', ?, 1, 'admin', CURRENT_TIMESTAMP)")
        .bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_plan (id, code, name, token_quota_daily, token_quota_monthly, interval, amount_cents, currency) VALUES (?, 'pro', 'Pro', 1000000, 20000000, 'monthly', 9900, 'CNY')")
        .bind(PLAN).execute(&pool).await.unwrap();
    for (id, status) in [(PAID_ORDER, "paid"), (PENDING_ORDER, "pending")] {
        sqlx::query("INSERT INTO core_order (id, tenant_id, plan_id, status, amount_cents, refunded_cents, currency, channel, provider_trade_no, paid_at, idempotency_key, created_at, updated_at) VALUES (?, ?, ?, ?, 9900, 0, 'CNY', 'wechat', ?, CURRENT_TIMESTAMP, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
            .bind(id).bind(TENANT).bind(PLAN).bind(status)
            .bind(format!("fake-{id}"))
            .bind(id).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO core_tenanttokenbudget (id, tenant_id, daily_limit, monthly_limit, created_at, updated_at) VALUES ('b1', ?, 1000000, 20000000, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
        .bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_entitlementgrant (id, tenant_id, order_id, period, tokens, reason, created_at) VALUES ('g1', ?, ?, 'monthly', 20000000, 'plan:pro', CURRENT_TIMESTAMP)")
        .bind(TENANT).bind(PAID_ORDER).execute(&pool).await.unwrap();
    let hash = ai_agent_stack_api::identity::session_token_hash(SESSION);
    sqlx::query("INSERT INTO rust_sessions (token_hash, user_id, expires_at, created_at) VALUES (?, 1, datetime('now', '+1 day'), CURRENT_TIMESTAMP)")
        .bind(hash).execute(&pool).await.unwrap();
    pool.close().await;
    let config = Config::for_test("127.0.0.1:0".into(), url, Some(SECRET.to_string()));
    AppState::new(config).unwrap()
}

fn with_session(builder: axum::http::request::Builder) -> Request<Body> {
    builder
        .header(
            "cookie",
            format!("sessionid={SESSION}; csrftoken=0123456789abcdef0123456789abcdef"),
        )
        .header(
            "x-csrftoken",
            "a".repeat(32) + "0123456789abcdef0123456789abcdef",
        )
        .body(Body::empty())
        .unwrap()
}

#[tokio::test]
async fn refunds_a_paid_order_and_reclaims_quota() {
    std::env::set_var("BILLING_FAKE_PROVIDER_ENABLED", "true");
    let app = router(state().await);
    let request = with_session(
        Request::builder()
            .method("POST")
            .uri(format!(
                "/api/v1/tenants/{TENANT}/billing/orders/{PAID_ORDER}/refund/"
            ))
            .header("content-type", "application/json"),
    );
    let body = Body::from(r#"{"amount_cents": 4950, "reason": "test"}"#);
    let mut request = request;
    *request.body_mut() = body;
    let response = app.oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body: Value = serde_json::from_slice(
        &axum::body::to_bytes(response.into_body(), 4096)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(body["status"], "partial_refunded");
    assert_eq!(body["refunded_cents"], 4950);
}

#[tokio::test]
async fn reconciliation_requires_a_valid_day_range() {
    let app = router(state().await);
    let response = app
        .oneshot(with_session(Request::builder().uri(format!(
            "/api/v1/tenants/{TENANT}/billing/reconciliation/?start=2026-01-01&end=2025-01-01"
        ))))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn reconciliation_reports_order_aggregates() {
    let state = state().await;
    let app = router(state);
    let response = app
        .oneshot(with_session(Request::builder().uri(format!(
            "/api/v1/tenants/{TENANT}/billing/reconciliation/?start=2026-01-01&end=2026-12-31"
        ))))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body: Value = serde_json::from_slice(
        &axum::body::to_bytes(response.into_body(), 4096)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(body["order_count"], 2);
}

#[tokio::test]
async fn fake_callback_settles_a_pending_order() {
    std::env::set_var("BILLING_FAKE_PROVIDER_ENABLED", "true");
    let app = router(state().await);
    let request = Request::builder()
        .method("POST")
        .uri(format!(
            "/api/v1/tenants/{TENANT}/billing/payments/wechat/callback/"
        ))
        .header("content-type", "application/json")
        .header("x-fake-signature", "fake-ok")
        .body(Body::from(format!(
            r#"{{"status":"paid","out_trade_no":"fake-{PENDING_ORDER}"}}"#
        )))
        .unwrap();
    let response = app.oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body: Value = serde_json::from_slice(
        &axum::body::to_bytes(response.into_body(), 4096)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(body["status"], "paid");
}

#[tokio::test]
async fn callback_rejects_missing_signature() {
    let app = router(state().await);
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!(
                    "/api/v1/tenants/{TENANT}/billing/payments/wechat/callback/"
                ))
                .header("content-type", "application/json")
                .body(Body::from(r#"{"status":"paid"}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}
