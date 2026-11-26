use std::time::Duration;

use ai_agent_stack_api_edge::config::Config;
use ai_agent_stack_api_edge::identity::encode_session_data;
use ai_agent_stack_api_edge::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;

const SECRET: &str = "local-only-change-before-production";
const SESSION: &str = "preferences-session";

async fn state() -> AppState {
    let path =
        std::env::temp_dir().join(format!("rust-preferences-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE django_session (session_key VARCHAR(40) PRIMARY KEY, session_data TEXT NOT NULL, expire_date DATETIME NOT NULL)",
        "CREATE TABLE auth_user (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, email VARCHAR(254) NOT NULL, password VARCHAR(128) NOT NULL, is_staff INTEGER NOT NULL, is_active INTEGER NOT NULL)",
        "CREATE TABLE core_userpreference (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, namespace VARCHAR(32) NOT NULL, \"values\" TEXT NOT NULL, updated_by_id INTEGER, updated_at DATETIME NOT NULL, UNIQUE(user_id, namespace))",
        "CREATE TABLE core_auditevent (id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, actor_id INTEGER, event_type VARCHAR(80) NOT NULL, resource_type VARCHAR(80) NOT NULL, resource_id VARCHAR(160) NOT NULL, outcome VARCHAR(16) NOT NULL, request_id VARCHAR(64) NOT NULL, remote_addr VARCHAR(64), user_agent VARCHAR(300) NOT NULL, metadata TEXT NOT NULL, previous_hash VARCHAR(64) NOT NULL, event_hash VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO auth_user (id, username, email, password, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 'pbkdf2_sha256$demo', 0, 1)")
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

async fn call(
    state: &AppState,
    method: &str,
    uri: &str,
    body: Option<Value>,
) -> (StatusCode, Value) {
    let builder = Request::builder()
        .method(method)
        .uri(uri)
        .header(
            "cookie",
            format!(
                "sessionid={SESSION}; csrftoken={}",
                "0123456789abcdef0123456789abcdef"
            ),
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
async fn reads_defaults_and_updates_preferences() {
    let state = state().await;
    let (status, body) = call(
        &state,
        "GET",
        "/api/v1/users/me/preferences/appearance/",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["values"], json!({}));
    assert_eq!(body["effective"]["theme"], json!("system"));
    assert_eq!(body["sources"]["theme"], json!("default"));

    let (status, body) = call(
        &state,
        "PATCH",
        "/api/v1/users/me/preferences/appearance/",
        Some(json!({"theme": "dark"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["values"], json!({"theme": "dark"}));
    assert_eq!(body["effective"]["theme"], json!("dark"));
    assert_eq!(body["sources"]["theme"], json!("user"));

    let (status, body) = call(
        &state,
        "PATCH",
        "/api/v1/users/me/preferences/appearance/",
        Some(json!({"theme": "neon"})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], json!("SETTINGS_INVALID_VALUE"));
}

#[tokio::test]
async fn rejects_unknown_namespaces_and_shortcut_conflicts() {
    let state = state().await;
    let (status, _) = call(&state, "GET", "/api/v1/users/me/preferences/unknown/", None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);

    let (status, body) = call(
        &state,
        "PATCH",
        "/api/v1/users/me/preferences/shortcuts/",
        Some(json!({"unknown.action": "Mod+B"})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], json!("SETTINGS_UNKNOWN_KEY"));

    let (status, body) = call(
        &state,
        "PUT",
        "/api/v1/users/me/preferences/shortcuts/",
        Some(json!({"sidebar.toggle": "Mod+B", "command.palette": "mod+b"})),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["error"]["code"], json!("SHORTCUT_CONFLICT"));

    let (status, body) = call(
        &state,
        "PUT",
        "/api/v1/users/me/preferences/shortcuts/",
        Some(json!({"sidebar.toggle": "Mod+B"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["values"], json!({"sidebar.toggle": "Mod+B"}));
}
