use std::time::Duration;

use ai_agent_stack_api_edge::config::Config;
use ai_agent_stack_api_edge::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::Value;
use tower::ServiceExt;

const SECRET: &str = "local-only-change-before-production";
// Produced by Django's SessionStore with the same secret; this guards the
// Rust decoder against drifting from the Python signed-session format.
const SESSION_DATA: &str = ".eJxVjDkOwjAURO_iGll28EpJzxmsv9g4gBwpTirE3UmkFKDp5r2Zt0iwLjWtPc9pZHERWpx-OwR65rYDfkC7T5Kmtswjyl2RB-3yNnF-XQ_376BCr9taleDJRR-cN5FUtgCQWaGzxEYj0xY0gzM4ABQNgZwhLFqdbQixoPh8AQDzOO0:1xWUj2:x-QmQrHXaBBm46F7Czw8lBe1DgCrvkJ61KQUuuef3Fc";
const SESSION_KEY: &str = "vqbfwzqggr2tugiw32s0ocwznnqm56db";

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
        "CREATE TABLE django_session (\
         session_key VARCHAR(40) PRIMARY KEY, \
         session_data TEXT NOT NULL, \
         expire_date DATETIME NOT NULL)",
    )
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query(
        "CREATE TABLE auth_user (\
         id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, \
         email VARCHAR(254) NOT NULL, is_staff INTEGER NOT NULL, \
         is_active INTEGER NOT NULL)",
    )
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO auth_user (id, username, email, is_staff, is_active) \
         VALUES (1, 'edge-check@example.com', 'edge-check@example.com', 0, 1)",
    )
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO django_session (session_key, session_data, expire_date) \
         VALUES (?, ?, datetime('now', '+1 day'))",
    )
    .bind(SESSION_KEY)
    .bind(SESSION_DATA)
    .execute(&pool)
    .await
    .unwrap();
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
    let state = AppState::new(config).unwrap();
    (state, path.display().to_string())
}

#[tokio::test]
async fn serves_current_user_from_a_django_session_cookie() {
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
