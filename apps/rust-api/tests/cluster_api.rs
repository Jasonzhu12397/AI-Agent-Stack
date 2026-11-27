use std::time::Duration;

use ai_agent_stack_api_edge::config::Config;
use ai_agent_stack_api_edge::identity::encode_session_data;
use ai_agent_stack_api_edge::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::Value;
use tower::ServiceExt;

const SECRET: &str = "local-only-change-before-production";
const TENANT: &str = "11111111-1111-1111-1111-111111111111";
const PROJECT: &str = "22222222-2222-2222-2222-222222222222";
const NODE: &str = "33333333-3333-3333-3333-333333333333";
const SESSION: &str = "owner-session";

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-cluster-{}.sqlite3", uuid::Uuid::new_v4()));
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
        "CREATE TABLE core_agentmachine (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, inventory_revision INTEGER NOT NULL, observed_at DATETIME)",
        "CREATE TABLE core_clusterdeployment (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, eph_node_id TEXT NOT NULL, target_node_id TEXT, requested_by_id INTEGER NOT NULL, cluster_name VARCHAR(120) NOT NULL, kubernetes_version VARCHAR(40) NOT NULL, spec TEXT NOT NULL, plan_result TEXT NOT NULL, preflight_result TEXT NOT NULL, status VARCHAR(32) NOT NULL, idempotency_key VARCHAR(128) NOT NULL, error TEXT NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, started_at DATETIME, finished_at DATETIME)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO auth_user (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1)")
        .execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenant (id, name, slug, created_at) VALUES (?, 'Acme', 'acme', datetime('now'))")
        .bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES (?, ?, 1, 'owner', datetime('now'))")
        .bind(uuid::Uuid::new_v4().to_string()).bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_project (id, tenant_id, name, slug, created_at) VALUES (?, ?, 'Console', 'console', datetime('now'))")
        .bind(PROJECT).bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query(r#"INSERT INTO core_clusterdeployment (id, tenant_id, project_id, eph_node_id, target_node_id, requested_by_id, cluster_name, kubernetes_version, spec, plan_result, preflight_result, status, idempotency_key, error, created_at, updated_at, started_at, finished_at) VALUES ('deploy-1', ?, ?, ?, NULL, 1, 'lab-cluster', 'v1.31.0', '{"cluster_name":"lab-cluster","network_spec_version":2}', '{"cloud_control":{"deployment_id":"deploy-1"}}', '{"ok":true}', 'provisioning', 'key-1', '{}', datetime('now'), datetime('now'), NULL, NULL)"#)
        .bind(TENANT).bind(PROJECT).bind(NODE).execute(&pool).await.unwrap();
    let data = encode_session_data(1, "password-hash", SECRET).unwrap();
    sqlx::query("INSERT INTO django_session (session_key, session_data, expire_date) VALUES (?, ?, datetime('now', '+1 day'))")
        .bind(SESSION).bind(data).execute(&pool).await.unwrap();
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
    AppState::new(config).unwrap()
}

async fn call(state: &AppState, method: &str, uri: &str) -> (StatusCode, Value) {
    let request = Request::builder()
        .method(method)
        .uri(uri)
        .header("x-session-token", SESSION)
        .body(Body::empty())
        .unwrap();
    let response = router(state.clone()).oneshot(request).await.unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 4 * 1024 * 1024)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

#[tokio::test]
async fn lists_cluster_deployments() {
    let state = state().await;
    let uri = format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/cluster-deployments/");
    let (status, body) = call(&state, "GET", &uri).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["count"], 1);
    assert_eq!(body["results"][0]["cluster_name"], "lab-cluster");
    assert_eq!(
        body["results"][0]["cloud_control"]["deployment_id"],
        "deploy-1"
    );
    assert_eq!(body["results"][0]["network_spec_status"]["version"], 2);
}

#[tokio::test]
async fn reads_cluster_deployment_detail() {
    let state = state().await;
    let (status, body) = call(&state, "GET", "/api/v1/cluster-deployments/deploy-1/").await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["id"], "deploy-1");
    assert_eq!(body["status"], "provisioning");
    assert_eq!(body["network_evidence_status"], "missing");
}

#[tokio::test]
async fn reports_missing_network_evidence() {
    let state = state().await;
    let (status, body) = call(
        &state,
        "POST",
        "/api/v1/cluster-deployments/deploy-1/network/evidence/verify/",
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT, "{body}");
    assert_eq!(body["status"], "missing");
}
