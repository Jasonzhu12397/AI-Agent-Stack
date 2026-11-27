use std::net::SocketAddr;

use axum::extract::ConnectInfo;

use ai_agent_stack_api::config::Config;
use ai_agent_stack_api::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;

const SECRET: &str = "local-only-change-before-production";

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-register-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE django_session (session_key VARCHAR(40) PRIMARY KEY, session_data TEXT NOT NULL, expire_date DATETIME NOT NULL)",
        "CREATE TABLE auth_user (id INTEGER PRIMARY KEY AUTOINCREMENT, password VARCHAR(128) NOT NULL, last_login DATETIME, is_superuser BOOLEAN NOT NULL, username VARCHAR(150) NOT NULL, first_name VARCHAR(150) NOT NULL, last_name VARCHAR(150) NOT NULL, email VARCHAR(254) NOT NULL, is_staff BOOLEAN NOT NULL, is_active BOOLEAN NOT NULL, date_joined DATETIME NOT NULL)",
        "CREATE TABLE core_tenant (id TEXT PRIMARY KEY, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_tenantmembership (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_project (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_projectmembership (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_sessionactivity (session_key VARCHAR(40) PRIMARY KEY, user_id INTEGER NOT NULL, created_at DATETIME NOT NULL, last_activity DATETIME NOT NULL, revoked_at DATETIME)",
        "CREATE TABLE core_auditevent (id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, actor_id INTEGER, event_type VARCHAR(64) NOT NULL, resource_type VARCHAR(64) NOT NULL, resource_id TEXT, outcome VARCHAR(16) NOT NULL, request_id VARCHAR(64) NOT NULL, remote_addr VARCHAR(39), user_agent TEXT NOT NULL, metadata TEXT NOT NULL, previous_hash VARCHAR(64), event_hash VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    pool.close().await;
    let config = Config::for_test("127.0.0.1:0".into(), url, Some(SECRET.to_string()));
    AppState::new(config).unwrap()
}

async fn post(state: &AppState, body: Value) -> (StatusCode, Value) {
    let request = Request::builder()
        .method("POST")
        .uri("/api/v1/auth/register/")
        .header("content-type", "application/json")
        .header("cookie", "csrftoken=0123456789abcdef0123456789abcdef")
        .header(
            "x-csrftoken",
            format!("{}{}", "a".repeat(32), "0123456789abcdef0123456789abcdef"),
        )
        .extension(ConnectInfo(SocketAddr::from(([127, 0, 0, 1], 40000))))
        .body(Body::from(body.to_string()))
        .unwrap();
    let response = router(state.clone()).oneshot(request).await.unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 65536)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

#[tokio::test]
async fn registers_email_account_with_workspace() {
    let state = state().await;
    let (status, body) = post(
        &state,
        json!({"identifier": "new@example.com", "password": "strong-pass-1", "display_name": "New User"}),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED, "{body}");
    assert_eq!(body["username"], "new@example.com");
    assert!(body["tenant_id"].as_str().unwrap().len() == 32);
    assert!(body["project_id"].as_str().unwrap().len() == 32);
}

#[tokio::test]
async fn rejects_short_password_and_duplicate_account() {
    let state = state().await;
    let (status, _body) = post(
        &state,
        json!({"identifier": "new@example.com", "password": "short"}),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    let (status, body) = post(
        &state,
        json!({"identifier": "new@example.com", "password": "strong-pass-1"}),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED, "{body}");
    let (status, _body) = post(
        &state,
        json!({"identifier": "new@example.com", "password": "strong-pass-2"}),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
}
