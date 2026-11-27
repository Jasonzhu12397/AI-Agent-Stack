
use ai_agent_stack_api::config::Config;
use ai_agent_stack_api::identity::encode_session_data;
use ai_agent_stack_api::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-infra-{}.sqlite3", uuid::Uuid::new_v4()));
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
        "CREATE TABLE core_infrastructurenetworkprofile (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, profile TEXT NOT NULL, updated_by_id INTEGER, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
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
    let config = Config::for_test("127.0.0.1:0".into(), url, Some("secret".to_string()));
    AppState::new(config).unwrap()
}

fn profile_body() -> Value {
    json!({"profile": {"dns_servers": ["10.10.0.2"], "networks": [
        {"name": "control", "role": "control", "vlan": 70, "cidr": "10.10.0.0/24", "gateway_ipv4": "10.10.0.1", "ip_pools": [{"start": "10.10.0.10", "end": "10.10.0.20"}], "interface": "eno1", "host_roles": ["control_plane", "worker"], "default_route": true},
        {"name": "provisioning", "role": "provisioning", "vlan": 70, "cidr": "10.30.0.0/24", "ip_pools": [{"start": "10.30.0.10", "end": "10.30.0.30"}], "interface": "eno1", "host_roles": ["control_plane", "worker"]},
        {"name": "data", "role": "data", "vlan": 71, "cidr": "10.20.0.0/24", "gateway_ipv4": "10.20.0.1", "ip_pools": [{"start": "10.20.0.10", "end": "10.20.0.30"}], "bond": {"name": "bond_data", "interfaces": ["eno2", "eno3"], "mode": "802.3ad"}, "host_roles": ["worker", "storage"]}
    ]}})
}

async fn call(state: &AppState, method: &str, body: Option<Value>) -> (StatusCode, Value) {
    let mut builder = Request::builder()
        .method(method)
        .uri("/api/v1/tenants/tenant/projects/project/infrastructure-networks/")
        .header(
            "cookie",
            "sessionid=session-1; csrftoken=0123456789abcdef0123456789abcdef",
        )
        .header(
            "x-csrftoken",
            format!("{}{}", "a".repeat(32), "0123456789abcdef0123456789abcdef"),
        );
    if body.is_some() {
        builder = builder.header("content-type", "application/json");
    }
    let response = router(state.clone())
        .oneshot(
            builder
                .body(
                    body.map(|value| Body::from(value.to_string()))
                        .unwrap_or_else(Body::empty),
                )
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 1024 * 1024)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

#[tokio::test]
async fn stores_valid_profile_and_renders_network_data() {
    let state = state().await;
    let (status, body) = call(&state, "GET", None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["generated"], Value::Null);
    let (status, body) = call(&state, "PUT", Some(profile_body())).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["profile"]["networks"].as_array().unwrap().len(), 3);
    assert_eq!(
        body["generated"]["role_network_data"]["worker"]["links"]["vlans"][0]["vlanId"],
        70
    );
    assert_eq!(body["generated"]["ip_pools"].as_array().unwrap().len(), 3);
    let (status, body) = call(&state, "GET", None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert!(body["generated"].is_object());
}

#[tokio::test]
async fn rejects_overlapping_control_and_data_cidrs() {
    let state = state().await;
    let mut payload = profile_body();
    payload["profile"]["networks"][2]["cidr"] = json!("10.10.0.128/25");
    payload["profile"]["networks"][2]["gateway_ipv4"] = json!("10.10.0.129");
    payload["profile"]["networks"][2]["ip_pools"] =
        json!([{"start": "10.10.0.130", "end": "10.10.0.140"}]);
    let (status, body) = call(&state, "PUT", Some(payload)).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["detail"], "INFRA_NETWORK_CIDR_OVERLAP");
}
