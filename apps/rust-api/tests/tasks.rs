use std::time::Duration;

use ai_agent_stack_api_edge::config::Config;
use ai_agent_stack_api_edge::identity::encode_session_data;
use ai_agent_stack_api_edge::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;

const SECRET: &str = "local-only-change-before-production";
const TENANT: &str = "11111111-1111-1111-1111-111111111111";
const PROJECT: &str = "22222222-2222-2222-2222-222222222222";
const OWNER_SESSION: &str = "owner-session";
const VIEWER_SESSION: &str = "viewer-session";

async fn state() -> (AppState, String) {
    let path = std::env::temp_dir().join(format!("rust-tasks-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE django_session (session_key VARCHAR(40) PRIMARY KEY, session_data TEXT NOT NULL, expire_date DATETIME NOT NULL)",
        "CREATE TABLE auth_user (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, email VARCHAR(254) NOT NULL, is_staff INTEGER NOT NULL, is_active INTEGER NOT NULL)",
        "CREATE TABLE core_tenant (id TEXT PRIMARY KEY, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_tenantmembership (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_project (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_projectmembership (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_auditevent (id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, actor_id INTEGER, event_type VARCHAR(120) NOT NULL, resource_type VARCHAR(80) NOT NULL, resource_id VARCHAR(64) NOT NULL, outcome VARCHAR(16) NOT NULL, request_id VARCHAR(64) NOT NULL, remote_addr TEXT, user_agent VARCHAR(256) NOT NULL, metadata TEXT NOT NULL, previous_hash VARCHAR(64) NOT NULL, event_hash VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_task (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, task_type VARCHAR(120) NOT NULL, payload TEXT NOT NULL, result TEXT, error TEXT, status VARCHAR(24) NOT NULL, idempotency_key VARCHAR(128) NOT NULL, attempts INTEGER NOT NULL, max_attempts INTEGER NOT NULL, available_at DATETIME NOT NULL, lease_owner VARCHAR(120), lease_token TEXT, lease_expires_at DATETIME, heartbeat_at DATETIME, cancel_requested_at DATETIME, started_at DATETIME, finished_at DATETIME, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, UNIQUE (tenant_id, idempotency_key))",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    for (id, username) in [(1, "alice"), (2, "carol")] {
        sqlx::query(
            "INSERT INTO auth_user (id, username, email, is_staff, is_active) VALUES (?, ?, ?, 0, 1)",
        )
        .bind(id)
        .bind(username)
        .bind(format!("{username}@example.com"))
        .execute(&pool)
        .await
        .unwrap();
    }
    sqlx::query("INSERT INTO core_tenant (id, name, slug, created_at) VALUES (?, 'Acme', 'acme', datetime('now'))")
        .bind(TENANT)
        .execute(&pool)
        .await
        .unwrap();
    for (user, role) in [(1, "owner"), (2, "viewer")] {
        sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES (?, ?, ?, ?, datetime('now'))")
            .bind(uuid::Uuid::new_v4().to_string())
            .bind(TENANT)
            .bind(user)
            .bind(role)
            .execute(&pool)
            .await
            .unwrap();
    }
    sqlx::query("INSERT INTO core_project (id, tenant_id, name, slug, created_at) VALUES (?, ?, 'Console', 'console', datetime('now'))")
        .bind(PROJECT)
        .bind(TENANT)
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_projectmembership (id, project_id, user_id, role, created_at) VALUES (?, ?, 1, 'admin', datetime('now'))")
        .bind(uuid::Uuid::new_v4().to_string())
        .bind(PROJECT)
        .execute(&pool)
        .await
        .unwrap();
    for (session, user) in [(OWNER_SESSION, 1), (VIEWER_SESSION, 2)] {
        let data = encode_session_data(user, "password-hash", SECRET).unwrap();
        sqlx::query("INSERT INTO django_session (session_key, session_data, expire_date) VALUES (?, ?, datetime('now', '+1 day'))")
            .bind(session)
            .bind(data)
            .execute(&pool)
            .await
            .unwrap();
    }
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
    (AppState::new(config).unwrap(), path.display().to_string())
}

async fn call(
    state: &AppState,
    method: &str,
    uri: &str,
    session: &str,
    key: Option<&str>,
    body: Value,
) -> (StatusCode, Value) {
    let mut builder = Request::builder()
        .method(method)
        .uri(uri)
        .header("cookie", format!("sessionid={session}"))
        .header("content-type", "application/json");
    if let Some(key) = key {
        builder = builder.header("idempotency-key", key);
    }
    let response = router(state.clone())
        .oneshot(builder.body(Body::from(body.to_string())).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 1024 * 1024)
        .await
        .unwrap();
    let value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, value)
}

#[tokio::test]
async fn creates_tasks_idempotently_and_lists_them() {
    let (state, _path) = state().await;
    let uri = format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/tasks/");
    let request = json!({"task_type": "demo.echo", "payload": {"message": "hi"}});
    let (status, body) = call(
        &state,
        "POST",
        &uri,
        OWNER_SESSION,
        Some("key-1"),
        request.clone(),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(body["status"], "queued");
    assert_eq!(body["payload"]["message"], "hi");
    let task_id = body["id"].as_str().unwrap().to_string();

    let (status, body) = call(&state, "POST", &uri, OWNER_SESSION, Some("key-1"), request).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["id"], task_id);

    let (status, _) = call(
        &state,
        "POST",
        &uri,
        OWNER_SESSION,
        Some("key-1"),
        json!({"task_type": "demo.echo", "payload": {"message": "different"}}),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);

    let (status, body) = call(&state, "GET", &uri, OWNER_SESSION, None, json!({})).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["count"], 1);
    assert_eq!(body["results"][0]["id"], task_id);

    let (status, _) = call(
        &state,
        "POST",
        &uri,
        VIEWER_SESSION,
        Some("key-2"),
        json!({"task_type": "demo.echo", "payload": {}}),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn cancels_a_queued_task_and_records_the_audit_event() {
    let (state, path) = state().await;
    let uri = format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/tasks/");
    let (status, body) = call(
        &state,
        "POST",
        &uri,
        OWNER_SESSION,
        Some("cancel-key"),
        json!({"task_type": "demo.sleep", "payload": {"seconds": 1}}),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED);
    let task_id = body["id"].as_str().unwrap().to_string();

    let (status, body) = call(
        &state,
        "POST",
        &format!("/api/v1/tasks/{task_id}/cancel/"),
        OWNER_SESSION,
        None,
        json!({}),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(body["status"], "cancelled");

    let (status, body) = call(
        &state,
        "GET",
        &format!("/api/v1/tasks/{task_id}/"),
        VIEWER_SESSION,
        None,
        json!({}),
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert!(body["detail"].is_string());

    let pool = sqlx::AnyPool::connect(&format!("sqlite://{path}"))
        .await
        .unwrap();
    let events: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM core_auditevent WHERE event_type = 'task.cancelled'",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(events, 1);
}
