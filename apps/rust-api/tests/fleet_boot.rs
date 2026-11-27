use std::time::Duration;

use ai_agent_stack_api_edge::config::Config;
use ai_agent_stack_api_edge::identity::encode_session_data;
use ai_agent_stack_api_edge::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-pxe-{}.sqlite3", uuid::Uuid::new_v4()));
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
        "CREATE TABLE core_agentnode (id TEXT PRIMARY KEY, name TEXT, tenant_id TEXT, project_id TEXT)",
        "CREATE TABLE core_agentmachine (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, agent_node_id TEXT, bmc_endpoint TEXT, bmc_credential_ref TEXT, lifecycle_state TEXT)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO auth_user (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1)").execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES ('m1', 'tenant', 1, 'owner', datetime('now'))").execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_project (id, tenant_id) VALUES ('project', 'tenant')")
        .execute(&pool)
        .await
        .unwrap();
    let session = encode_session_data(1, "password-hash", "secret").unwrap();
    sqlx::query("INSERT INTO django_session (session_key, session_data, expire_date) VALUES ('session-1', ?, datetime('now', '+1 day'))").bind(session).execute(&pool).await.unwrap();
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

async fn post(state: &AppState, payload: Value) -> (StatusCode, Value) {
    let response = router(state.clone())
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/tenants/tenant/projects/project/fleet/machines/pxe-boot/")
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
async fn requires_confirmation_when_executing() {
    let state = state().await;
    let (status, body) = post(&state, json!({"execution": true})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"]["code"], "EXECUTION_CONFIRMATION_REQUIRED");
}

#[tokio::test]
async fn reports_no_machines_without_bmc() {
    let state = state().await;
    let (status, body) = post(&state, json!({"execution": false})).await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{body}");
    assert_eq!(body["error"]["code"], "MACHINES_NOT_FOUND");
}

#[tokio::test]
async fn rejects_non_uuid_machine_ids() {
    let state = state().await;
    let (status, body) = post(&state, json!({"machine_ids": ["not-a-uuid"]})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"]["code"], "INVALID_MACHINE_IDS");
}

async fn post_discovery(state: &AppState, payload: Value) -> (StatusCode, Value) {
    let response = router(state.clone())
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/tenants/tenant/projects/project/fleet/machines/discovery/")
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
async fn discovery_requires_confirmation_when_executing() {
    let state = state().await;
    let (status, body) = post_discovery(&state, json!({"execution": true})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"]["code"], "EXECUTION_CONFIRMATION_REQUIRED");
}

#[tokio::test]
async fn discovery_reports_no_machines() {
    let state = state().await;
    let (status, body) = post_discovery(&state, json!({"execution": false})).await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{body}");
    assert_eq!(body["error"]["code"], "MACHINES_NOT_FOUND");
}
