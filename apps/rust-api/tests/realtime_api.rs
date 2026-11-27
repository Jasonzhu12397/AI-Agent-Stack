use ai_agent_stack_api::config::Config;
use ai_agent_stack_api::identity::encode_session_data;
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
        "CREATE TABLE django_session (session_key VARCHAR(40) PRIMARY KEY, session_data TEXT NOT NULL, expire_date DATETIME NOT NULL)",
        "CREATE TABLE auth_user (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, email VARCHAR(254) NOT NULL, is_staff INTEGER NOT NULL, is_active INTEGER NOT NULL)",
        "CREATE TABLE core_tenantmembership (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_project (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL)",
        "CREATE TABLE core_projectmembership (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_task (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, status VARCHAR(24) NOT NULL, task_type VARCHAR(48) NOT NULL, attempts INTEGER NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_auditevent (id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, actor_id INTEGER, event_type VARCHAR(64) NOT NULL, resource_type VARCHAR(64) NOT NULL, resource_id TEXT, outcome VARCHAR(16) NOT NULL, request_id VARCHAR(64) NOT NULL, remote_addr VARCHAR(39), user_agent TEXT NOT NULL, metadata TEXT NOT NULL, previous_hash VARCHAR(64), event_hash VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO auth_user (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1)")
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
    let session = encode_session_data(1, "password-hash", "secret").unwrap();
    sqlx::query("INSERT INTO django_session (session_key, session_data, expire_date) VALUES ('session-1', ?, datetime('now', '+1 day'))")
        .bind(session).execute(&pool).await.unwrap();
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
