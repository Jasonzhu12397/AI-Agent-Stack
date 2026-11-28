use ai_agent_stack_api::config::Config;
use ai_agent_stack_api::identity::session_token_hash;
use ai_agent_stack_api::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::Value;
use tower::ServiceExt;

const SECRET: &str = "local-only-change-before-production";
const TENANT: &str = "11111111-1111-1111-1111-111111111111";
const PROJECT: &str = "22222222-2222-2222-2222-222222222222";
const SESSION: &str = "owner-session";

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-cloudvm-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE auth_user (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, email VARCHAR(254) NOT NULL, is_staff INTEGER NOT NULL, is_active INTEGER NOT NULL)",

        "CREATE TABLE platform_users (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL UNIQUE, email VARCHAR(254) NOT NULL DEFAULT '', is_staff INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1)",

        "CREATE TABLE platform_credentials (user_id INTEGER NOT NULL, credential_type VARCHAR(32) NOT NULL, credential_hash TEXT NOT NULL, PRIMARY KEY (user_id, credential_type))",

        "CREATE TABLE rust_sessions (token_hash CHAR(64) PRIMARY KEY, user_id INTEGER NOT NULL, expires_at DATETIME NOT NULL, created_at DATETIME NOT NULL, revoked_at DATETIME)",
        "CREATE TABLE core_tenant (id TEXT PRIMARY KEY, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_tenantmembership (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_project (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_projectmembership (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_cloudvminstance (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, provider_key VARCHAR(64) NOT NULL, server_id VARCHAR(128) NOT NULL, name VARCHAR(63) NOT NULL, flavor_ref VARCHAR(128) NOT NULL, image_ref VARCHAR(128) NOT NULL, network_ids TEXT NOT NULL, status VARCHAR(32) NOT NULL, ip_address VARCHAR(64), provider_metadata TEXT NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO platform_users (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1)")
        .execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenant (id, name, slug, created_at) VALUES (?, 'Acme', 'acme', datetime('now'))")
        .bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES (?, ?, 1, 'owner', datetime('now'))")
        .bind(uuid::Uuid::new_v4().to_string()).bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_project (id, tenant_id, name, slug, created_at) VALUES (?, ?, 'Console', 'console', datetime('now'))")
        .bind(PROJECT).bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_projectmembership (id, project_id, user_id, role, created_at) VALUES (?, ?, 1, 'editor', datetime('now'))")
        .bind(uuid::Uuid::new_v4().to_string()).bind(PROJECT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_cloudvminstance (id, tenant_id, project_id, created_by_id, provider_key, server_id, name, flavor_ref, image_ref, network_ids, status, ip_address, provider_metadata, created_at, updated_at) VALUES ('vm-1', ?, ?, 1, 'lab', 'srv-1', 'lab-vm', 'm1.small', 'ubuntu', '[\"net-1\"]', 'ACTIVE', '10.0.0.5', '{}', datetime('now'), datetime('now'))")
        .bind(TENANT).bind(PROJECT).execute(&pool).await.unwrap();
    let data = session_token_hash(SESSION);
    sqlx::query("INSERT INTO rust_sessions (token_hash, user_id, expires_at, created_at) VALUES (?, 1, datetime('now', '+1 day'), CURRENT_TIMESTAMP)").bind(&data).execute(&pool).await.unwrap();
    pool.close().await;
    let config = Config::for_test("127.0.0.1:0".into(), url, Some(SECRET.to_string()));
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
async fn lists_cloud_vm_instances() {
    let state = state().await;
    let (status, body) = call(
        &state,
        "GET",
        &format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/cloud-vms/"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["count"], 1);
    assert_eq!(body["results"][0]["name"], "lab-vm");
    assert_eq!(body["results"][0]["network_ids"][0], "net-1");
}

#[tokio::test]
async fn lists_configured_cloud_providers() {
    std::env::set_var(
        "CLOUD_VM_PROVIDERS",
        r#"{"lab":{"type":"openstack","display_name":"Lab","region":"lab-1"}}"#,
    );
    let state = state().await;
    let (status, body) = call(
        &state,
        "GET",
        &format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/cloud-vms/providers/"),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["count"], 1);
    assert_eq!(body["results"][0]["key"], "lab");
}

#[tokio::test]
async fn reads_cloud_vm_detail() {
    let state = state().await;
    let (status, body) = call(&state, "GET", "/api/v1/cloud-vms/vm-1/").await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["server_id"], "srv-1");
    assert_eq!(body["status"], "ACTIVE");
    assert_eq!(body["warnings"], Value::Array(vec![]));
}

async fn post_action(state: &AppState, uri: &str, body: Value) -> (StatusCode, Value) {
    let request = Request::builder()
        .method("POST")
        .uri(uri)
        .header("x-session-token", SESSION)
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
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
async fn rejects_unknown_cloud_vm_action() {
    let state = state().await;
    let (status, body) = post_action(
        &state,
        "/api/v1/cloud-vms/vm-1/actions/",
        serde_json::json!({"action": "delete"}),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["code"], "CLOUD_VM_ACTION_INVALID");
}

#[tokio::test]
async fn reports_incomplete_cloud_vm_provider_config() {
    let state = state().await;
    let (status, body) = post_action(
        &state,
        "/api/v1/cloud-vms/vm-1/actions/",
        serde_json::json!({"action": "start"}),
    )
    .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    assert_eq!(body["code"], "CLOUD_VM_PROVIDER_CONFIG_INVALID");
}

#[tokio::test]
async fn rejects_cloud_vm_create_without_networks() {
    let state = state().await;
    let (status, body) = post_action(
        &state,
        &format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/cloud-vms/"),
        serde_json::json!({
            "provider": "lab",
            "name": "web-1",
            "flavor_ref": "m1.small",
            "image_ref": "ubuntu-22.04",
            "network_ids": [],
        }),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["code"], "CLOUD_VM_REQUEST_INVALID");
}

#[tokio::test]
async fn reports_not_found_when_deleting_unknown_cloud_vm() {
    let state = state().await;
    let request = Request::builder()
        .method("DELETE")
        .uri("/api/v1/cloud-vms/missing-vm/")
        .header("x-session-token", SESSION)
        .body(Body::empty())
        .unwrap();
    let response = router(state).oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}
