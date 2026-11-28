use ai_agent_stack_api::config::Config;
use ai_agent_stack_api::identity::session_token_hash;
use ai_agent_stack_api::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::Value;
use tower::ServiceExt;

const SECRET: &str = "local-only-change-before-production";
const SESSION: &str = "roadmap-session";

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-roadmap-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE auth_user (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, email VARCHAR(254) NOT NULL, is_staff INTEGER NOT NULL, is_active INTEGER NOT NULL)",

        "CREATE TABLE platform_users (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL UNIQUE, email VARCHAR(254) NOT NULL DEFAULT '', is_staff INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1)",

        "CREATE TABLE platform_credentials (user_id INTEGER NOT NULL, credential_type VARCHAR(32) NOT NULL, credential_hash TEXT NOT NULL, PRIMARY KEY (user_id, credential_type))",

        "CREATE TABLE rust_sessions (token_hash CHAR(64) PRIMARY KEY, user_id INTEGER NOT NULL, expires_at DATETIME NOT NULL, created_at DATETIME NOT NULL, revoked_at DATETIME)",
        "CREATE TABLE platform_session_activity (token_hash CHAR(64) PRIMARY KEY, user_id INTEGER NOT NULL, created_at DATETIME NOT NULL, last_activity DATETIME NOT NULL, revoked_at DATETIME)",
        "CREATE TABLE core_task (id TEXT PRIMARY KEY, status VARCHAR(16) NOT NULL)",
        "CREATE TABLE core_agentversion (id TEXT PRIMARY KEY)",
        "CREATE TABLE core_agentrun (id TEXT PRIMARY KEY, task_id TEXT NOT NULL)",
        "CREATE TABLE core_workflowversion (id TEXT PRIMARY KEY)",
        "CREATE TABLE core_workflowsteprun (id TEXT PRIMARY KEY, status VARCHAR(16) NOT NULL, workflow_run_id TEXT NOT NULL)",
        "CREATE TABLE core_agentnode (id TEXT PRIMARY KEY, status VARCHAR(16) NOT NULL)",
        "CREATE TABLE core_agentcommand (id TEXT PRIMARY KEY, status VARCHAR(16) NOT NULL, approved_at DATETIME, command_type VARCHAR(32) NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO platform_users (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1)")
        .execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_task (id, status) VALUES ('task-1', 'succeeded')")
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_agentversion (id) VALUES ('av-1')")
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_agentrun (id, task_id) VALUES ('ar-1', 'task-1')")
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_workflowversion (id) VALUES ('wv-1')")
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_workflowsteprun (id, status, workflow_run_id) VALUES ('step-1', 'succeeded', 'run-1')")
        .execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_agentnode (id, status) VALUES ('node-1', 'online')")
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_agentcommand (id, status, approved_at, command_type) VALUES ('cmd-1', 'succeeded', datetime('now'), 'exec')")
        .execute(&pool).await.unwrap();
    let data = session_token_hash(SESSION);
    sqlx::query("INSERT INTO rust_sessions (token_hash, user_id, expires_at, created_at) VALUES (?, 1, datetime('now', '+1 day'), CURRENT_TIMESTAMP)").bind(&data).execute(&pool).await.unwrap();
    pool.close().await;
    let config = Config::for_test("127.0.0.1:0".into(), url, Some(SECRET.to_string()));
    AppState::new(config).unwrap()
}

#[tokio::test]
async fn reports_roadmap_from_live_rows() {
    let state = state().await;
    let request = Request::builder()
        .method("GET")
        .uri("/api/v1/platform/roadmap/")
        .header(
            "cookie",
            format!("sessionid={SESSION}; csrftoken=0123456789abcdef0123456789abcdef"),
        )
        .body(Body::empty())
        .unwrap();
    let response = router(state).oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body: Value = serde_json::from_slice(
        &axum::body::to_bytes(response.into_body(), 4096)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(body["items"].as_array().unwrap().len(), 4);
    assert_eq!(body["items"][1]["status"], "verified");
    assert_eq!(body["items"][1]["evidence"]["succeeded_runs"], 1);
    assert_eq!(body["items"][2]["status"], "verified");
    assert_eq!(body["items"][2]["evidence"]["runs"], 1);
    assert_eq!(body["items"][3]["status"], "verified");
    assert_eq!(body["items"][3]["evidence"]["enrolled_nodes"], 1);
}
