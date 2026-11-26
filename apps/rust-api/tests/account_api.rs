use std::time::Duration;

use ai_agent_stack_api_edge::config::Config;
use ai_agent_stack_api_edge::identity::encode_session_data;
use ai_agent_stack_api_edge::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;

const SECRET: &str = "local-only-change-before-production";
const SESSION: &str = "account-session";

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-account-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE django_session (session_key VARCHAR(40) PRIMARY KEY, session_data TEXT NOT NULL, expire_date DATETIME NOT NULL)",
        "CREATE TABLE auth_user (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, email VARCHAR(254) NOT NULL, is_staff INTEGER NOT NULL, is_active INTEGER NOT NULL)",
        "CREATE TABLE core_useraccountprofile (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE, display_name VARCHAR(160) NOT NULL, timezone VARCHAR(64) NOT NULL, locale VARCHAR(16) NOT NULL, updated_by_id INTEGER, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_auditevent (id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, actor_id INTEGER, event_type VARCHAR(80) NOT NULL, resource_type VARCHAR(80) NOT NULL, resource_id VARCHAR(160) NOT NULL, outcome VARCHAR(16) NOT NULL, request_id VARCHAR(64) NOT NULL, remote_addr VARCHAR(64), user_agent VARCHAR(300) NOT NULL, metadata TEXT NOT NULL, previous_hash VARCHAR(64) NOT NULL, event_hash VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO auth_user (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1)")
        .execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO auth_user (id, username, email, is_staff, is_active) VALUES (2, 'bob', 'bob@example.com', 0, 1)")
        .execute(&pool).await.unwrap();
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

async fn request(state: &AppState, method: &str, body: Option<Value>) -> (StatusCode, Value) {
    let builder = Request::builder()
        .method(method)
        .uri("/api/v1/users/me/")
        .header(
            "cookie",
            format!("sessionid={SESSION}; csrftoken=0123456789abcdef0123456789abcdef"),
        )
        .header(
            "x-csrftoken",
            format!("{}{}", "a".repeat(32), "0123456789abcdef0123456789abcdef"),
        );
    let request = match body {
        Some(body) => builder
            .header("content-type", "application/json")
            .body(Body::from(body.to_string()))
            .unwrap(),
        None => builder.body(Body::empty()).unwrap(),
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
async fn reads_and_updates_the_account_profile() {
    let state = state().await;
    let (status, body) = request(&state, "GET", None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["id"], "1");
    assert_eq!(body["username"], "alice");
    assert_eq!(body["display_name"], "");
    assert_eq!(body["timezone"], "UTC");
    assert_eq!(body["locale"], "zh-hans");

    let (status, body) = request(
        &state,
        "PATCH",
        Some(json!({
            "display_name": "Alice Chen",
            "email": "  ALICE@EXAMPLE.COM ",
            "timezone": "Asia/Shanghai",
            "locale": "en",
        })),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["display_name"], "Alice Chen");
    assert_eq!(body["email"], "alice@example.com");
    assert_eq!(body["timezone"], "Asia/Shanghai");
    assert_eq!(body["locale"], "en");

    let (status, body) = request(&state, "PATCH", Some(json!({"email": "bob@example.com"}))).await;
    assert_eq!(status, StatusCode::CONFLICT, "{body}");
    assert_eq!(body["error"]["code"], "ACCOUNT_EMAIL_TAKEN");

    let (status, body) = request(&state, "PATCH", Some(json!({"timezone": "  "}))).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"]["code"], "ACCOUNT_INVALID_VALUE");

    let (status, body) = request(&state, "PATCH", Some(json!({"ignored": "x"}))).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"]["code"], "ACCOUNT_NO_FIELDS");
}
