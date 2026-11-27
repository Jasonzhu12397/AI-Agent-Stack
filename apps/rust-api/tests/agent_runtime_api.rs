use std::time::Duration;

use ai_agent_stack_api_edge::config::Config;
use ai_agent_stack_api_edge::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use tower::ServiceExt;

const NODE: &str = "11111111111111111111111111111111";
const COMMAND: &str = "22222222222222222222222222222222";
const LEASE: &str = "33333333333333333333333333333333";
const TOKEN: &str = "runtime-node-token";

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-runtime-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE django_session (session_key VARCHAR(40) PRIMARY KEY, session_data TEXT NOT NULL, expire_date DATETIME NOT NULL)",
        "CREATE TABLE auth_user (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, email VARCHAR(254) NOT NULL, is_staff INTEGER NOT NULL, is_active INTEGER NOT NULL)",
        "CREATE TABLE core_agentnode (id TEXT PRIMARY KEY, name VARCHAR(120) NOT NULL, node_type VARCHAR(24) NOT NULL, cluster_name VARCHAR(120) NOT NULL, agent_version VARCHAR(40) NOT NULL, capabilities TEXT NOT NULL, credential_hash VARCHAR(64) NOT NULL, status VARCHAR(16) NOT NULL, last_seen_at DATETIME, last_seen_ip VARCHAR(39), revoked_at DATETIME, client_certificate_revoked_at DATETIME, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentcommand (id TEXT PRIMARY KEY, command_type VARCHAR(64) NOT NULL, payload TEXT NOT NULL, status VARCHAR(24) NOT NULL, idempotency_key VARCHAR(128) NOT NULL, lease_token VARCHAR(32), lease_expires_at DATETIME, result TEXT, error TEXT, attempts INTEGER NOT NULL, max_attempts INTEGER NOT NULL, approved_plan_hash VARCHAR(64), approved_at DATETIME, started_at DATETIME, finished_at DATETIME, deployment_id TEXT, node_id TEXT, tenant_id TEXT, project_id TEXT, requested_by_id INTEGER, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO auth_user (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1)")
        .execute(&pool).await.unwrap();
    let hash = hex::encode(Sha256::digest(TOKEN.as_bytes()));
    sqlx::query("INSERT INTO core_agentnode (id, name, node_type, cluster_name, agent_version, capabilities, credential_hash, status, created_at, updated_at) VALUES (?, 'node-a', 'target_cluster', 'lab', '1.0.0', '[\"exec\"]', ?, 'online', datetime('now'), datetime('now'))")
        .bind(NODE).bind(hash).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_agentcommand (id, command_type, payload, status, idempotency_key, lease_token, lease_expires_at, attempts, max_attempts, created_at, updated_at, node_id, tenant_id, project_id, requested_by_id) VALUES (?, 'cluster.scale', '{}', 'running', 'key-1', ?, datetime('now', '+5 minutes'), 1, 3, datetime('now'), datetime('now'), ?, 'tenant', 'project', 1)")
         .bind(COMMAND).bind(LEASE).bind(NODE).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_agentcommand (id, command_type, payload, status, idempotency_key, attempts, max_attempts, created_at, updated_at, node_id, tenant_id, project_id, requested_by_id) VALUES ('44444444444444444444444444444444', 'cluster.observe', '{}', 'queued', 'key-2', 0, 3, datetime('now'), datetime('now'), ?, 'tenant', 'project', 1)")
        .bind(NODE).execute(&pool).await.unwrap();
    pool.close().await;
    let config = Config::new(
        "127.0.0.1:0".into(),
        "http://127.0.0.1:1".into(),
        Duration::from_secs(2),
        Duration::from_secs(10),
    )
    .unwrap()
    .with_native_identity(true, Some(url), Some("secret".to_string()))
    .unwrap();
    AppState::new(config).unwrap()
}

async fn post(
    state: &AppState,
    uri: &str,
    body: Value,
    lease: Option<&str>,
) -> (StatusCode, Value) {
    let mut request = Request::builder()
        .method("POST")
        .uri(uri)
        .header("authorization", format!("Bearer {TOKEN}"))
        .header("content-type", "application/json");
    if let Some(lease) = lease {
        request = request.header("x-agent-lease", lease);
    }
    let response = router(state.clone())
        .oneshot(request.body(Body::from(body.to_string())).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 4096)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

#[tokio::test]
async fn accepts_heartbeat_and_rejects_bad_credentials() {
    let state = state().await;
    let (status, body) = post(
        &state,
        "/api/v1/agent-runtime/heartbeat/",
        json!({"node_id": NODE, "agent_version": "1.1.0", "capabilities": ["exec", "observe"]}),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["status"], "online");
    let (status, _) = post(
        &state,
        "/api/v1/agent-runtime/heartbeat/",
        json!({"node_id": NODE}),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let request = Request::builder()
        .method("POST")
        .uri("/api/v1/agent-runtime/heartbeat/")
        .header("authorization", "Bearer wrong")
        .header("content-type", "application/json")
        .body(Body::from(json!({"node_id": NODE}).to_string()))
        .unwrap();
    let response = router(state).oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}

#[tokio::test]
async fn renews_lease_and_records_terminal_result() {
    let state = state().await;
    let (status, body) = post(
        &state,
        &format!("/api/v1/agent-runtime/commands/{COMMAND}/lease/renew/"),
        json!({"node_id": NODE}),
        Some(LEASE),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["id"], COMMAND);

    let (status, body) = post(
        &state,
        &format!("/api/v1/agent-runtime/commands/{COMMAND}/result/"),
        json!({"node_id": NODE, "outcome": "succeeded", "result": {"replicas": 3}}),
        Some(LEASE),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["status"], "succeeded");

    let (status, body) = post(
        &state,
        &format!("/api/v1/agent-runtime/commands/{COMMAND}/result/"),
        json!({"node_id": NODE, "outcome": "succeeded", "result": {"replicas": 3}}),
        Some(LEASE),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "replay: {body}");
    assert_eq!(body["status"], "succeeded");
}

#[tokio::test]
async fn polls_queued_command_and_issues_lease() {
    let state = state().await;
    let (status, body) = post(
        &state,
        "/api/v1/agent-runtime/commands/poll/",
        json!({"node_id": NODE}),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["command"]["id"], "44444444444444444444444444444444");
    assert_eq!(body["command"]["command_type"], "cluster.observe");
    assert_eq!(body["command"]["attempt"], 1);
    assert_eq!(body["command"]["lease_token"].as_str().unwrap().len(), 32);
}
