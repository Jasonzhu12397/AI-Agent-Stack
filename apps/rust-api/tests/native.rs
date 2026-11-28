use ai_agent_stack_api::config::Config;
use ai_agent_stack_api::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::Value;
use tower::ServiceExt;

const SECRET: &str = "local-only-change-before-production";
const SESSION_KEY: &str = "native-session";

async fn state() -> (AppState, String) {
    let path = std::env::temp_dir().join(format!(
        "rust-native-identity-{}.sqlite3",
        uuid::Uuid::new_v4()
    ));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    sqlx::query(
        "CREATE TABLE platform_users (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, \
         email VARCHAR(254) NOT NULL, is_staff INTEGER NOT NULL, is_active INTEGER NOT NULL)",
    )
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query(
        "CREATE TABLE rust_sessions (token_hash CHAR(64) PRIMARY KEY, user_id INTEGER NOT NULL, \
         expires_at DATETIME NOT NULL, created_at DATETIME NOT NULL, revoked_at DATETIME)",
    )
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query(
        "CREATE TABLE platform_session_activity (token_hash CHAR(64) PRIMARY KEY, \
         user_id INTEGER NOT NULL, created_at DATETIME NOT NULL, \
         last_activity DATETIME NOT NULL, revoked_at DATETIME)",
    )
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO platform_users (id, username, email, is_staff, is_active) \
         VALUES (1, 'edge-check@example.com', 'edge-check@example.com', 0, 1)",
    )
    .execute(&pool)
    .await
    .unwrap();
    let hash = ai_agent_stack_api::identity::session_token_hash(SESSION_KEY);
    sqlx::query(
        "INSERT INTO rust_sessions (token_hash, user_id, expires_at, created_at) \
         VALUES (?, 1, datetime('now', '+1 day'), CURRENT_TIMESTAMP)",
    )
    .bind(hash)
    .execute(&pool)
    .await
    .unwrap();
    pool.close().await;

    let config = Config::for_test("127.0.0.1:0".into(), url, Some(SECRET.to_string()));
    let state = AppState::new(config).unwrap();
    (state, path.display().to_string())
}

#[tokio::test]
async fn serves_current_user_from_a_native_session_cookie() {
    let (state, path) = state().await;
    let response = router(state)
        .oneshot(
            Request::builder()
                .uri("/api/v1/auth/me/")
                .header("cookie", format!("csrftoken=abc; sessionid={SESSION_KEY}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body: Value = serde_json::from_slice(
        &axum::body::to_bytes(response.into_body(), 1024 * 1024)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(
        body,
        serde_json::json!({
            "id": 1,
            "username": "edge-check@example.com",
            "email": "edge-check@example.com",
            "is_staff": false,
        })
    );
    let _ = std::fs::remove_file(path);
}

#[tokio::test]
async fn accepts_the_session_token_header_instead_of_the_cookie() {
    let (state, path) = state().await;
    let response = router(state)
        .oneshot(
            Request::builder()
                .uri("/api/v1/auth/me/")
                .header("x-session-token", SESSION_KEY)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let _ = std::fs::remove_file(path);
}

#[tokio::test]
async fn rejects_a_missing_or_expired_session() {
    let (state, path) = state().await;
    let response = router(state)
        .oneshot(
            Request::builder()
                .uri("/api/v1/auth/me/")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    assert_eq!(
        response.headers().get("www-authenticate").unwrap(),
        "Session"
    );
    let _ = std::fs::remove_file(path);
}
