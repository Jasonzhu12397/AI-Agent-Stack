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
const OWNER_SESSION: &str = "owner-session";
const MEMBER_SESSION: &str = "member-session";

async fn state() -> (AppState, String) {
    let path = std::env::temp_dir().join(format!("rust-budget-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE django_session (session_key VARCHAR(40) PRIMARY KEY, session_data TEXT NOT NULL, expire_date DATETIME NOT NULL)",
        "CREATE TABLE auth_user (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, email VARCHAR(254) NOT NULL, is_staff INTEGER NOT NULL, is_active INTEGER NOT NULL)",
        "CREATE TABLE core_tenant (id TEXT PRIMARY KEY, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_tenantmembership (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_tenanttokenbudget (id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL, daily_limit INTEGER NOT NULL, monthly_limit INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_tenanttokenusage (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, status VARCHAR(16) NOT NULL, consumed_tokens INTEGER NOT NULL, reserved_tokens INTEGER NOT NULL, expires_at DATETIME, settled_at DATETIME, scope_ref VARCHAR(160))",
        "CREATE TABLE core_auditevent (id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, actor_id INTEGER, event_type VARCHAR(120) NOT NULL, resource_type VARCHAR(80) NOT NULL, resource_id VARCHAR(64) NOT NULL, outcome VARCHAR(16) NOT NULL, request_id VARCHAR(64) NOT NULL, remote_addr TEXT, user_agent VARCHAR(256) NOT NULL, metadata TEXT NOT NULL, previous_hash VARCHAR(64) NOT NULL, event_hash VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    for (id, username) in [(1, "alice"), (2, "carol")] {
        sqlx::query("INSERT INTO auth_user (id, username, email, is_staff, is_active) VALUES (?, ?, ?, 0, 1)")
            .bind(id).bind(username).bind(format!("{username}@example.com"))
            .execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO core_tenant (id, name, slug, created_at) VALUES (?, 'Acme', 'acme', datetime('now'))")
        .bind(TENANT).execute(&pool).await.unwrap();
    for (user, role) in [(1, "owner"), (2, "member")] {
        sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES (?, ?, ?, ?, datetime('now'))")
            .bind(uuid::Uuid::new_v4().to_string()).bind(TENANT).bind(user).bind(role)
            .execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO core_tenanttokenusage (id, tenant_id, status, consumed_tokens, reserved_tokens, expires_at, settled_at) VALUES (?, ?, 'settled', 100, 0, NULL, datetime('now'))")
        .bind(uuid::Uuid::new_v4().to_string()).bind(TENANT)
        .execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenanttokenusage (id, tenant_id, status, consumed_tokens, reserved_tokens, expires_at, settled_at) VALUES (?, ?, 'reserved', 0, 50, datetime('now', '+1 hour'), NULL)")
        .bind(uuid::Uuid::new_v4().to_string()).bind(TENANT)
        .execute(&pool).await.unwrap();
    for (session, user) in [(OWNER_SESSION, 1), (MEMBER_SESSION, 2)] {
        let data = encode_session_data(user, "password-hash", SECRET).unwrap();
        sqlx::query("INSERT INTO django_session (session_key, session_data, expire_date) VALUES (?, ?, datetime('now', '+1 day'))")
            .bind(session).bind(data).execute(&pool).await.unwrap();
    }
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
    (AppState::new(config).unwrap(), path.display().to_string())
}

async fn call(state: &AppState, method: &str, session: &str, body: Value) -> (StatusCode, Value) {
    let uri = format!("/api/v1/tenants/{TENANT}/token-budget/");
    let response = router(state.clone())
        .oneshot(
            Request::builder()
                .method(method)
                .uri(uri)
                .header("cookie", format!("sessionid={session}"))
                .header("content-type", "application/json")
                .body(Body::from(body.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 1024 * 1024)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

#[tokio::test]
async fn returns_limits_and_usage_then_updates_them() {
    let (state, _path) = state().await;
    let (status, body) = call(&state, "GET", OWNER_SESSION, json!({})).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["daily_limit"], 100000);
    assert_eq!(body["monthly_limit"], 1000000);
    assert_eq!(body["daily_used"], 150);
    assert_eq!(body["monthly_used"], 150);

    let (status, body) = call(&state, "PATCH", OWNER_SESSION, json!({"daily_limit": 200})).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["daily_limit"], 200);
    assert_eq!(body["monthly_limit"], 1000000);

    let (status, _) = call(&state, "PUT", OWNER_SESSION, json!({"daily_limit": 300})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    let (status, _) = call(&state, "PATCH", OWNER_SESSION, json!({"daily_limit": true})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn members_read_but_cannot_update_and_changes_are_audited() {
    let (state, _path) = state().await;
    let (status, body) = call(&state, "GET", MEMBER_SESSION, json!({})).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["daily_used"], 150);

    let (status, body) = call(&state, "PATCH", MEMBER_SESSION, json!({"daily_limit": 500})).await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    assert!(body["detail"].is_string());

    let (status, _) = call(
        &state,
        "PATCH",
        OWNER_SESSION,
        json!({"daily_limit": 500, "monthly_limit": 900}),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let pool = sqlx::AnyPool::connect(&format!("sqlite://{_path}"))
        .await
        .unwrap();
    let events: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM core_auditevent WHERE event_type = 'token_budget.updated'",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(events, 1);
}
