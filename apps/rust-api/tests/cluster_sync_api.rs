use ai_agent_stack_api::config::Config;
use ai_agent_stack_api::identity::session_token_hash;
use ai_agent_stack_api::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-eph-{}.sqlite3", uuid::Uuid::new_v4()));
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
        "CREATE TABLE core_projectmembership (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_clusterdeployment (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, preflight_result TEXT, updated_at DATETIME)",
        "CREATE TABLE core_infrastructurenetworkprofile (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, profile TEXT NOT NULL, created_at DATETIME, updated_at DATETIME)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO platform_users (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1)").execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES ('m1', 'tenant', 1, 'owner', datetime('now'))").execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_projectmembership (id, project_id, user_id, role, created_at) VALUES ('pm1', 'project', 1, 'editor', datetime('now'))").execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_clusterdeployment (id, tenant_id, project_id, preflight_result) VALUES ('deploy-1', 'tenant', 'project', '{}')").execute(&pool).await.unwrap();
    let session = session_token_hash("session-1");
    sqlx::query("INSERT INTO rust_sessions (token_hash, user_id, expires_at, created_at) VALUES (?, 1, datetime('now', '+1 day'), CURRENT_TIMESTAMP)").bind(session).execute(&pool).await.unwrap();
    pool.close().await;
    let config = Config::for_test("127.0.0.1:0".into(), url, Some("secret".to_string()));
    AppState::new(config).unwrap()
}

async fn post(state: &AppState, id: &str) -> (StatusCode, Value) {
    let response = router(state.clone())
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!(
                    "/api/v1/cluster-deployments/{id}/preflight/eph/sync/"
                ))
                .header(
                    "cookie",
                    "sessionid=session-1; csrftoken=0123456789abcdef0123456789abcdef",
                )
                .header(
                    "x-csrftoken",
                    format!("{}{}", "a".repeat(32), "0123456789abcdef0123456789abcdef"),
                )
                .body(Body::empty())
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
async fn unknown_deployment_is_not_found() {
    let state = state().await;
    let (status, body) = post(&state, "missing").await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{body}");
}

#[tokio::test]
async fn unconfigured_cloud_control_is_service_unavailable() {
    let state = state().await;
    let (status, body) = post(&state, "deploy-1").await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    assert_eq!(body["error"]["code"], "EPH_PREFLIGHT_UNAVAILABLE");
}

#[tokio::test]
async fn bmh_sync_is_unavailable_without_cloud_control() {
    let state = state().await;
    let response = router(state.clone())
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/cluster-deployments/deploy-1/bmh-inventory/sync/")
                .header(
                    "cookie",
                    "sessionid=session-1; csrftoken=0123456789abcdef0123456789abcdef",
                )
                .header(
                    "x-csrftoken",
                    format!("{}{}", "a".repeat(32), "0123456789abcdef0123456789abcdef"),
                )
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
}

#[tokio::test]
async fn renders_ironic_template_from_profile() {
    let state = state().await;
    let bucket = json!({"infra_network_profile": {"dns_servers": ["10.10.0.2"], "networks": [
        {"name": "control", "role": "control", "vlan": 70, "cidr": "10.10.0.0/24", "gateway_ipv4": "10.10.0.1", "ip_pools": [{"start": "10.10.0.10", "end": "10.10.0.20"}], "interface": "eno1", "host_roles": ["control_plane", "worker"], "default_route": true},
        {"name": "provisioning", "role": "provisioning", "vlan": 70, "cidr": "10.30.0.0/24", "ip_pools": [{"start": "10.30.0.10", "end": "10.30.0.30"}], "interface": "eno1", "host_roles": ["control_plane", "worker"]},
        {"name": "data", "role": "data", "vlan": 71, "cidr": "10.20.0.0/24", "gateway_ipv4": "10.20.0.1", "ip_pools": [{"start": "10.20.0.10", "end": "10.20.0.30"}], "bond": {"name": "bond_data", "interfaces": ["eno2", "eno3"], "mode": "802.3ad"}, "host_roles": ["worker", "storage"]}
    ]}});
    let response = router(state.clone())
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/tenants/tenant/projects/project/cluster-deployments/ironic-template/")
                .header(
                    "cookie",
                    "sessionid=session-1; csrftoken=0123456789abcdef0123456789abcdef",
                )
                .header(
                    "x-csrftoken",
                    format!("{}{}", "a".repeat(32), "0123456789abcdef0123456789abcdef"),
                )
                .header("content-type", "application/json")
                .body(Body::from(bucket.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(response.into_body(), 1024 * 1024)
        .await
        .unwrap();
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(body["provisioning_network"], "provisioning");
    assert_eq!(body["configuration"]["networking"]["interface"], "eno1.70");
    assert_eq!(
        body["configuration"]["networking"]["ip_address"],
        "10.30.0.2"
    );
    assert_eq!(body["warnings"].as_array().unwrap().len(), 0);
}
