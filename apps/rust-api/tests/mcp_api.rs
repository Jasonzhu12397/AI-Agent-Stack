use ai_agent_stack_api::config::Config;
use ai_agent_stack_api::identity::session_token_hash;
use ai_agent_stack_api::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-mcp-{}.sqlite3", uuid::Uuid::new_v4()));
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
        "CREATE TABLE core_tenantmembership (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_project (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL)",
        "CREATE TABLE core_projectmembership (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_task (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, task_type VARCHAR(120) NOT NULL, payload TEXT NOT NULL, result TEXT, error TEXT, status VARCHAR(24) NOT NULL, idempotency_key VARCHAR(128) NOT NULL, attempts INTEGER NOT NULL, max_attempts INTEGER NOT NULL, available_at DATETIME NOT NULL, lease_owner VARCHAR(120), lease_token TEXT, lease_expires_at DATETIME, heartbeat_at DATETIME, cancel_requested_at DATETIME, started_at DATETIME, finished_at DATETIME, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, UNIQUE (tenant_id, idempotency_key))",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO platform_users (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1)").execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES ('m1', 'tenant', 1, 'owner', datetime('now'))").execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_project (id, tenant_id) VALUES ('project', 'tenant')")
        .execute(&pool)
        .await
        .unwrap();
    let session = session_token_hash("session-1");
    sqlx::query("INSERT INTO rust_sessions (token_hash, user_id, expires_at, created_at) VALUES (?, 1, datetime('now', '+1 day'), CURRENT_TIMESTAMP)").bind(session).execute(&pool).await.unwrap();
    pool.close().await;
    let config = Config::for_test("127.0.0.1:0".into(), url, Some("secret".to_string()));
    AppState::new(config).unwrap()
}

async fn rpc(state: &AppState, payload: Value) -> (StatusCode, Value) {
    let response = router(state.clone())
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/tenants/tenant/projects/project/mcp/")
                .header(
                    "cookie",
                    "sessionid=session-1; csrftoken=0123456789abcdef0123456789abcdef",
                )
                .header(
                    "x-csrftoken",
                    format!("{}{}", "a".repeat(32), "0123456789abcdef0123456789abcdef"),
                )
                .header("content-type", "application/json")
                .body(Body::from(payload.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 1024 * 1024)
        .await
        .unwrap();
    (status, serde_json::from_slice(&bytes).unwrap())
}

#[tokio::test]
async fn initializes_and_lists_tools() {
    let state = state().await;
    let (status, body) = rpc(&state, json!({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["result"]["serverInfo"]["name"], "ai-agent-stack");
    let (status, body) = rpc(
        &state,
        json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["result"]["tools"].as_array().unwrap().len(), 5);
}

#[tokio::test]
async fn creates_and_lists_task_through_mcp() {
    let state = state().await;
    let (status, body) = rpc(&state, json!({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "platform_create_task", "arguments": {"task_type": "demo.echo", "payload": {"message": "hi"}, "idempotency_key": "mcp-key-1"}}})).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let structured = &body["result"]["structuredContent"];
    assert_eq!(structured["created"], true, "{body}");
    assert_eq!(structured["task"]["status"], "queued");
    let (status, body) = rpc(&state, json!({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "platform_list_tasks", "arguments": {"limit": 10}}})).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(
        body["result"]["structuredContent"]["tasks"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    let (_, body) = rpc(&state, json!({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "platform_get_task", "arguments": {"task_id": structured["task"]["id"]}}})).await;
    assert_eq!(
        body["result"]["structuredContent"]["task"]["payload"]["message"],
        "hi"
    );
}

#[tokio::test]
async fn rejects_batch_requests() {
    let state = state().await;
    let (status, body) = rpc(
        &state,
        json!([{"jsonrpc": "2.0", "id": 1, "method": "ping"}]),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["error"]["code"], -32600);
}
