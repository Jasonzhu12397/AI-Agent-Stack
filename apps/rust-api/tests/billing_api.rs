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
        "CREATE TABLE core_plan (id TEXT PRIMARY KEY, code VARCHAR(64) NOT NULL, name VARCHAR(120) NOT NULL, description TEXT NOT NULL, interval VARCHAR(16) NOT NULL, price_cents INTEGER NOT NULL, currency VARCHAR(8) NOT NULL, token_quota_daily INTEGER NOT NULL, token_quota_monthly INTEGER NOT NULL, features TEXT NOT NULL, region_scope VARCHAR(16) NOT NULL, active BOOLEAN NOT NULL)",
        "CREATE TABLE core_subscription (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, plan_id TEXT NOT NULL, status VARCHAR(16) NOT NULL, started_at DATETIME, current_period_start DATETIME, current_period_end DATETIME, cancel_at_period_end BOOLEAN NOT NULL, renewal_mode VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_paymentchannelsecret (id TEXT PRIMARY KEY, channel VARCHAR(32) NOT NULL, slot VARCHAR(64) NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO auth_user (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1)")
        .execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenant (id, name, slug, created_at) VALUES (?, 'Acme', 'acme', datetime('now'))")
        .bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES (?, ?, 1, 'owner', datetime('now'))")
        .bind(uuid::Uuid::new_v4().to_string()).bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_plan (id, code, name, description, interval, price_cents, currency, token_quota_daily, token_quota_monthly, features, region_scope, active) VALUES ('plan-1', 'starter', 'Starter', 'Entry plan', 'monthly', 9900, 'CNY', 200000, 4000000, '[\"3 projects\"]', 'domestic', 1)")
        .execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_plan (id, code, name, description, interval, price_cents, currency, token_quota_daily, token_quota_monthly, features, region_scope, active) VALUES ('plan-2', 'archived', 'Archived', 'Old plan', 'monthly', 19900, 'CNY', 1, 1, '[]', 'domestic', 0)")
        .execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_subscription (id, tenant_id, plan_id, status, started_at, current_period_start, current_period_end, cancel_at_period_end, renewal_mode, created_at, updated_at) VALUES ('sub-1', ?, 'plan-1', 'active', datetime('now'), datetime('now'), datetime('now', '+30 days'), 0, 'manual', datetime('now'), datetime('now'))")
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
    let request = Request::builder()
        .method("GET")
        .uri(uri)
        .header("cookie", format!("sessionid={SESSION}"))
        .body(Body::empty())
        .unwrap();
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
