use ai_agent_stack_api::config::Config;
use ai_agent_stack_api::identity::session_token_hash;
use ai_agent_stack_api::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use tower::ServiceExt;

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-realtime-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE auth_user (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, email VARCHAR(254) NOT NULL, is_staff INTEGER NOT NULL, is_active INTEGER NOT NULL)",

        "CREATE TABLE platform_users (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL UNIQUE, email VARCHAR(254) NOT NULL DEFAULT '', is_staff INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1)",

        "CREATE TABLE platform_credentials (user_id INTEGER NOT NULL, credential_type VARCHAR(32) NOT NULL, credential_hash TEXT NOT NULL, PRIMARY KEY (user_id, credential_type))",

        "CREATE TABLE rust_sessions (token_hash CHAR(64) PRIMARY KEY, user_id INTEGER NOT NULL, expires_at DATETIME NOT NULL, created_at DATETIME NOT NULL, revoked_at DATETIME)",
        "CREATE TABLE core_tenantmembership (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_project (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL)",
        "CREATE TABLE core_projectmembership (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_task (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, status VARCHAR(24) NOT NULL, task_type VARCHAR(48) NOT NULL, attempts INTEGER NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_auditevent (id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, actor_id INTEGER, event_type VARCHAR(64) NOT NULL, resource_type VARCHAR(64) NOT NULL, resource_id TEXT, outcome VARCHAR(16) NOT NULL, request_id VARCHAR(64) NOT NULL, remote_addr VARCHAR(39), user_agent TEXT NOT NULL, metadata TEXT NOT NULL, previous_hash VARCHAR(64), event_hash VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO platform_users (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1)")
        .execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES ('m1', 'tenant', 1, 'owner', datetime('now'))")
        .execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_project (id, tenant_id) VALUES ('project', 'tenant')")
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_task (id, tenant_id, project_id, status, task_type, attempts, updated_at) VALUES ('task-1', 'tenant', 'project', 'running', 'demo.echo', 1, datetime('now'))")
        .execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_auditevent (id, tenant_id, project_id, event_type, resource_type, resource_id, outcome, request_id, user_agent, metadata, event_hash, created_at) VALUES ('audit-1', 'tenant', 'project', 'task.created', 'task', 'task-1', 'success', '', '', '{}', 'hash', datetime('now'))")
        .execute(&pool).await.unwrap();
    let session = session_token_hash("session-1");
    sqlx::query("INSERT INTO rust_sessions (token_hash, user_id, expires_at, created_at) VALUES (?, 1, datetime('now', '+1 day'), CURRENT_TIMESTAMP)").bind(session).execute(&pool).await.unwrap();
    pool.close().await;
    let config = Config::for_test("127.0.0.1:0".into(), url, Some("secret".to_string()));
    AppState::new(config).unwrap()
}

#[tokio::test]
async fn streams_bounded_task_and_audit_events() {
    let state = state().await;
    let response = router(state)
        .oneshot(
            Request::builder()
                .uri("/api/v1/tenants/tenant/projects/project/events/stream/?since=1970-01-01T00:00:00Z&max_events=2")
                .header("cookie", "sessionid=session-1")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(response.headers()["content-type"], "text/event-stream");
    let bytes = axum::body::to_bytes(response.into_body(), 64 * 1024)
        .await
        .unwrap();
    let body = String::from_utf8_lossy(&bytes);
    assert!(body.contains("retry: 3000"), "{body}");
    assert!(body.contains("event: ready"), "{body}");
    assert!(body.contains("event: task"), "{body}");
    assert!(body.contains("event: audit"), "{body}");
    // Every id must be a parseable RFC3339 instant: the client hands the last
    // one back as Last-Event-ID, and a malformed stamp silently resets the
    // stream to "now".
    for line in body.lines().filter_map(|line| line.strip_prefix("id: ")) {
        if line.is_empty() {
            continue;
        }
        assert!(
            chrono::DateTime::parse_from_rfc3339(line).is_ok(),
            "event id is not RFC3339: {line}"
        );
    }
}

#[tokio::test]
async fn rejects_out_of_range_max_events() {
    let state = state().await;
    let response = router(state)
        .oneshot(
            Request::builder()
                .uri("/api/v1/tenants/tenant/projects/project/events/stream/?max_events=101")
                .header("cookie", "sessionid=session-1")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn websocket_route_is_registered_for_real_upgrade_handshakes() {
    let state = state().await;
    let response = router(state)
        .oneshot(
            Request::builder()
                .uri("/ws/tenants/tenant/projects/project/events/")
                .header("upgrade", "websocket")
                .header("connection", "Upgrade")
                .header("sec-websocket-key", "dGhlIHNhbXBsZSBub25jZQ==")
                .header("sec-websocket-version", "13")
                .header("sec-websocket-protocol", "aiagentstack")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    // The in-process test request has no hyper upgrade extension. A real
    // handshake supplies that extension; then the handler performs session
    // and project authorization before accepting the WebSocket.
    assert_eq!(response.status(), StatusCode::UPGRADE_REQUIRED);
}
