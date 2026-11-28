use ai_agent_stack_api::config::Config;
use ai_agent_stack_api::identity::session_token_hash;
use ai_agent_stack_api::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!(
        "rust-fleet-machine-{}.sqlite3",
        uuid::Uuid::new_v4()
    ));
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
        "CREATE TABLE core_tenant (id TEXT PRIMARY KEY, name TEXT)",
        "CREATE TABLE core_tenantmembership (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_project (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL)",
        "CREATE TABLE core_projectmembership (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentnode (id INTEGER PRIMARY KEY, name TEXT, labels TEXT)",
        "CREATE TABLE core_agentmachine (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, agent_node_id INTEGER, bmc_endpoint TEXT NOT NULL DEFAULT '', bmc_credential_ref TEXT NOT NULL DEFAULT '', bmc_trust_bundle_ref TEXT NOT NULL DEFAULT '', bmc_tls_verify INTEGER NOT NULL DEFAULT 1, bmc_configured_at DATETIME, bmc_identity_evidence TEXT NOT NULL DEFAULT '{}', bmh_name TEXT NOT NULL DEFAULT '', system_uuid TEXT NOT NULL DEFAULT '', identity_material TEXT NOT NULL DEFAULT '{}', identity_status TEXT NOT NULL DEFAULT '', identity_conflicts TEXT NOT NULL DEFAULT '[]', vendor TEXT NOT NULL DEFAULT '', product_name TEXT NOT NULL DEFAULT '', hardware_inventory TEXT NOT NULL DEFAULT '{}', inventory_source TEXT NOT NULL DEFAULT '', inventory_revision INTEGER NOT NULL DEFAULT 0, observed_at DATETIME, lifecycle_state TEXT NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_firmwareupdatejob (id TEXT PRIMARY KEY, image_url TEXT NOT NULL DEFAULT '', component TEXT NOT NULL DEFAULT '', apply_time TEXT NOT NULL DEFAULT 'Immediate', targets TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'submitted', task_monitor TEXT NOT NULL DEFAULT '', task_state TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '', idempotency_key TEXT NOT NULL DEFAULT '', created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, finished_at DATETIME, machine_id TEXT NOT NULL, project_id TEXT NOT NULL, requested_by_id INTEGER NOT NULL, tenant_id TEXT NOT NULL)",
        "CREATE TABLE core_infrastructurenetworkprofile (id TEXT PRIMARY KEY, profile TEXT NOT NULL DEFAULT '{}', created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, project_id TEXT NOT NULL, tenant_id TEXT NOT NULL, updated_by_id INTEGER)",
        "CREATE TABLE core_auditevent (id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, actor_id INTEGER, event_type TEXT NOT NULL, resource_type TEXT NOT NULL, resource_id TEXT NOT NULL, outcome TEXT NOT NULL, request_id TEXT NOT NULL, remote_addr TEXT, user_agent TEXT NOT NULL, metadata TEXT NOT NULL, previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL, created_at TEXT NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO platform_users (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1)").execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenant (id, name) VALUES ('tenant', 'Acme')")
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES ('tm', 'tenant', 1, 'owner', datetime('now'))").execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_project (id, tenant_id) VALUES ('project', 'tenant')")
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_projectmembership (id, project_id, user_id, role, created_at) VALUES ('pm', 'project', 1, 'admin', datetime('now'))").execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_agentnode (id, name, labels) VALUES (1, 'node-a', '{}')")
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_agentmachine (id, tenant_id, project_id, agent_node_id, vendor, product_name, lifecycle_state, created_at, updated_at) VALUES ('machine-1', 'tenant', 'project', 1, 'Supermicro', 'SYS-1', 'discovered', datetime('now'), datetime('now'))").execute(&pool).await.unwrap();
    let session = session_token_hash("session-1");
    sqlx::query("INSERT INTO rust_sessions (token_hash, user_id, expires_at, created_at) VALUES (?, 1, datetime('now', '+1 day'), CURRENT_TIMESTAMP)").bind(session).execute(&pool).await.unwrap();
    pool.close().await;
    let config = Config::for_test("127.0.0.1:0".into(), url, Some("secret".to_string()));
    AppState::new(config).unwrap()
}

async fn send(
    state: &AppState,
    method: &str,
    path: &str,
    body: Option<Value>,
) -> (StatusCode, Value) {
    let request = Request::builder()
        .method(method)
        .uri(path)
        .header(
            "cookie",
            "sessionid=session-1; csrftoken=0123456789abcdef0123456789abcdef",
        )
        .header(
            "x-csrftoken",
            format!("{}{}", "a".repeat(32), "0123456789abcdef0123456789abcdef"),
        )
        .header("content-type", "application/json")
        .body(
            body.map(|value| Body::from(value.to_string()))
                .unwrap_or_else(Body::empty),
        )
        .unwrap();
    let response = router(state.clone()).oneshot(request).await.unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 1024 * 1024)
        .await
        .unwrap();
    let value = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap_or(Value::Null)
    };
    (status, value)
}

#[tokio::test]
async fn lifecycle_review_appends_audit_event() {
    let state = state().await;
    let (status, body) = send(
        &state,
        "POST",
        "/api/v1/tenants/tenant/projects/project/fleet/machines/machine-1/lifecycle-review/",
        Some(json!({
            "expected_state": "discovered",
            "target_state": "identity_review",
            "reason": "handover from discovery to identity review",
        })),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["lifecycle_state"], "identity_review");
    let pool = state.identity_pool.as_ref().unwrap();
    let count = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_auditevent WHERE event_type = 'fleet.machine.lifecycle_reviewed'",
    )
    .fetch_one(pool)
    .await
    .unwrap();
    assert_eq!(count, 1);
}

#[tokio::test]
async fn lifecycle_review_rejects_unknown_transition() {
    let state = state().await;
    let (status, body) = send(
        &state,
        "POST",
        "/api/v1/tenants/tenant/projects/project/fleet/machines/machine-1/lifecycle-review/",
        Some(json!({
            "expected_state": "discovered",
            "target_state": "commissioned",
            "reason": "skip identity review entirely",
        })),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT, "{body}");
    assert_eq!(body["error"]["code"], "LIFECYCLE_TRANSITION_NOT_ENABLED");
}

#[tokio::test]
async fn bmc_config_accepts_private_origin_and_rejects_loopback() {
    let state = state().await;
    let path = "/api/v1/tenants/tenant/projects/project/fleet/machines/machine-1/bmc-config/";
    let (status, _) = send(
        &state,
        "PUT",
        path,
        Some(json!({
            "endpoint": "https://127.0.0.1",
            "credential_ref": "vault://secret/bmc/prod",
            "trust_bundle_ref": "",
        })),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    let (status, body) = send(
        &state,
        "PUT",
        path,
        Some(json!({
            "endpoint": "https://10.10.20.30",
            "credential_ref": "vault://secret/bmc/prod",
            "trust_bundle_ref": "",
        })),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["endpoint"], "https://10.10.20.30");
    assert_eq!(body["tls_verify"], true);
}

#[tokio::test]
async fn firmware_inventory_rejects_unsupported_vendor() {
    let state = state().await;
    let (status, body) = send(
        &state,
        "GET",
        "/api/v1/tenants/tenant/projects/project/fleet/machines/machine-1/firmware/",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::UNPROCESSABLE_ENTITY, "{body}");
    assert_eq!(body["error"]["code"], "FIRMWARE_VENDOR_UNSUPPORTED");
}

#[tokio::test]
async fn network_verify_checks_observed_host_nics() {
    let state = state().await;
    let pool = state.identity_pool.as_ref().unwrap();
    let profile = json!({"dns_servers": ["10.10.0.2"], "networks": [
        {"name": "control", "role": "control", "vlan": 70, "cidr": "10.10.0.0/24", "gateway_ipv4": "10.10.0.1", "ip_pools": [{"start": "10.10.0.10", "end": "10.10.0.20"}], "interface": "eno1", "host_roles": ["control_plane", "worker"], "default_route": true},
        {"name": "provisioning", "role": "provisioning", "vlan": 70, "cidr": "10.30.0.0/24", "ip_pools": [{"start": "10.30.0.10", "end": "10.30.0.30"}], "interface": "eno1", "host_roles": ["control_plane", "worker"]},
        {"name": "data", "role": "data", "vlan": 71, "cidr": "10.20.0.0/24", "gateway_ipv4": "10.20.0.1", "ip_pools": [{"start": "10.20.0.10", "end": "10.20.0.30"}], "bond": {"name": "bond_data", "interfaces": ["eno2", "eno3"], "mode": "802.3ad"}, "host_roles": ["worker", "storage"]}
    ]});
    sqlx::query("INSERT INTO core_infrastructurenetworkprofile (id, profile, created_at, updated_at, project_id, tenant_id) VALUES ('profile-1', ?, datetime('now'), datetime('now'), 'project', 'tenant')")
        .bind(profile.to_string())
        .execute(pool)
        .await
        .unwrap();
    sqlx::query("UPDATE core_agentmachine SET hardware_inventory = ? WHERE id = 'machine-1'")
        .bind(
            json!({"nics":[
            {"name":"eno1","mtu":1500,"vlans":[70]},
            {"name":"eno2","mtu":1500,"vlans":[71]},
            {"name":"eno3","mtu":1500,"vlans":[71]}
        ],"bonds":[{"name":"bond_data","mode":"802.3ad","interfaces":["eno2","eno3"]}]})
            .to_string(),
        )
        .execute(pool)
        .await
        .unwrap();
    let (status, body) = send(
        &state,
        "GET",
        "/api/v1/tenants/tenant/projects/project/fleet/machines/machine-1/network/verify/?host_role=worker",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["status"], "pass");
    assert_eq!(body["checked_networks"].as_array().unwrap().len(), 3);
}

#[tokio::test]
async fn firmware_inventory_returns_jobs_when_bmc_is_not_configured() {
    let state = state().await;
    let pool = state.identity_pool.as_ref().unwrap();
    sqlx::query("UPDATE core_agentmachine SET vendor = 'Dell', product_name = 'R750' WHERE id = 'machine-1'")
        .execute(pool)
        .await
        .unwrap();
    let (status, body) = send(
        &state,
        "GET",
        "/api/v1/tenants/tenant/projects/project/fleet/machines/machine-1/firmware/",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["bmc_configured"], false);
    assert!(body["jobs"].as_array().unwrap().is_empty());
}
