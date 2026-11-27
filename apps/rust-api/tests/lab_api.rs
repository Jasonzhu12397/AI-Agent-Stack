
use ai_agent_stack_api::config::Config;
use ai_agent_stack_api::identity::encode_session_data;
use ai_agent_stack_api::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use tower::ServiceExt;

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-lab-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    sqlx::query("CREATE TABLE django_session (session_key VARCHAR(40) PRIMARY KEY, session_data TEXT NOT NULL, expire_date DATETIME NOT NULL)")
        .execute(&pool).await.unwrap();
    sqlx::query("CREATE TABLE auth_user (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, email VARCHAR(254) NOT NULL, is_staff INTEGER NOT NULL, is_active INTEGER NOT NULL)")
        .execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO auth_user (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1)")
        .execute(&pool).await.unwrap();
    let session = encode_session_data(1, "password-hash", "secret").unwrap();
    sqlx::query("INSERT INTO django_session (session_key, session_data, expire_date) VALUES ('session-1', ?, datetime('now', '+1 day'))")
        .bind(session).execute(&pool).await.unwrap();
    pool.close().await;
    let config = Config::for_test("127.0.0.1:0".into(), url, Some("secret".to_string()));
    AppState::new(config).unwrap()
}

#[tokio::test]
async fn reports_missing_lab_workflow_instead_of_failing() {
    let state = state().await;
    let response = router(state)
        .oneshot(
            Request::builder()
                .uri("/api/v1/metal3/lab-workflow/")
                .header("cookie", "sessionid=session-1")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(response.into_body(), 64 * 1024)
        .await
        .unwrap();
    let body: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(body["available"], false);
    assert_eq!(body["reason"], "LAB_WORKFLOW_NOT_PRESENT");
    assert_eq!(body["secret_presence"], "not_observable");
}
