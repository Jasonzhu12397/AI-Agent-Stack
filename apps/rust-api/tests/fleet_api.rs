use ai_agent_stack_api::config::Config;
use ai_agent_stack_api::identity::session_token_hash;
use ai_agent_stack_api::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-fleet-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE django_session (session_key VARCHAR(40) PRIMARY KEY, session_data TEXT NOT NULL, expire_date DATETIME NOT NULL)",
        "CREATE TABLE auth_user (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, email VARCHAR(254) NOT NULL, is_staff INTEGER NOT NULL, is_active INTEGER NOT NULL)",

        "CREATE TABLE platform_users (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL UNIQUE, email VARCHAR(254) NOT NULL DEFAULT '', is_staff INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1)",

        "CREATE TABLE platform_credentials (user_id INTEGER NOT NULL, credential_type VARCHAR(32) NOT NULL, credential_hash TEXT NOT NULL, PRIMARY KEY (user_id, credential_type))",

        "CREATE TABLE rust_sessions (token_hash CHAR(64) PRIMARY KEY, user_id INTEGER NOT NULL, expires_at DATETIME NOT NULL, created_at DATETIME NOT NULL, revoked_at DATETIME)",
        "CREATE TABLE core_tenantmembership (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_project (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL)",
        "CREATE TABLE core_projectmembership (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentmachine (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, bmc_endpoint TEXT, bmc_credential_ref TEXT, bmh_name TEXT, system_uuid TEXT, vendor TEXT, product_name TEXT, hardware_inventory TEXT, inventory_source TEXT, inventory_revision INTEGER, observed_at DATETIME, lifecycle_state TEXT NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO platform_users (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1)").execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES ('m1', 'tenant', 1, 'owner', datetime('now'))").execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_project (id, tenant_id) VALUES ('project', 'tenant')")
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_agentmachine (id, tenant_id, project_id, bmc_endpoint, bmh_name, system_uuid, hardware_inventory, inventory_source, inventory_revision, lifecycle_state, created_at, updated_at) VALUES ('m-1', 'tenant', 'project', 'https://bmc.example:8443', '', '', '{}', 'legacy_agent_label', 0, 'discovered', datetime('now'), datetime('now'))").execute(&pool).await.unwrap();
    let session = session_token_hash("session-1");
    sqlx::query("INSERT INTO rust_sessions (token_hash, user_id, expires_at, created_at) VALUES (?, 1, datetime('now', '+1 day'), CURRENT_TIMESTAMP)").bind(session).execute(&pool).await.unwrap();
    pool.close().await;
    let config = Config::for_test("127.0.0.1:0".into(), url, Some("secret".to_string()));
    AppState::new(config).unwrap()
}

async fn post(state: &AppState, payload: Value) -> (StatusCode, Value) {
    let response = router(state.clone())
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/tenants/tenant/projects/project/fleet/machines/bmh-inventory/")
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

fn host(pci: bool) -> Value {
    json!({"document": {"items": [{
        "metadata": {"name": "bmh-node-1", "namespace": "metal3"},
        "spec": {"bmc": {"address": "bmc.example:8443"}, "bootMACAddress": "AA:BB:CC:DD:EE:FF"},
        "status": {"hardware": {"systemUUID": "uuid-1", "manufacturer": "Dell", "productName": "R750", "nics": [{"name": "eno1", "mac": "aa:bb:cc:dd:ee:ff", "pciAddress": if pci {"0000:3b:00.0"} else {""}}]}}
    }]}})
}

#[tokio::test]
async fn merges_inspected_host_into_machine() {
    let state = state().await;
    let (status, body) = post(&state, host(true)).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["host_count"], 1);
    assert_eq!(body["matched"], 1);
    assert_eq!(body["updated"][0]["pci_inventory_adopted"], true);
    assert!(body["warnings"].as_array().unwrap().is_empty());
}

#[tokio::test]
async fn rejects_invalid_document() {
    let state = state().await;
    let (status, body) = post(&state, json!({"document": {"items": [{"metadata": {}}]}})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"]["code"], "BMH_NAME_REQUIRED");
}
