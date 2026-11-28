use ai_agent_stack_api::config::Config;
use ai_agent_stack_api::identity::session_token_hash;
use ai_agent_stack_api::proxy::{router, AppState};
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
        "CREATE TABLE auth_user (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, email VARCHAR(254) NOT NULL, is_staff INTEGER NOT NULL, is_active INTEGER NOT NULL)",

        "CREATE TABLE platform_users (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL UNIQUE, email VARCHAR(254) NOT NULL DEFAULT '', is_staff INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1)",

        "CREATE TABLE platform_credentials (user_id INTEGER NOT NULL, credential_type VARCHAR(32) NOT NULL, credential_hash TEXT NOT NULL, PRIMARY KEY (user_id, credential_type))",

        "CREATE TABLE rust_sessions (token_hash CHAR(64) PRIMARY KEY, user_id INTEGER NOT NULL, expires_at DATETIME NOT NULL, created_at DATETIME NOT NULL, revoked_at DATETIME)",
        "CREATE TABLE core_agentnode (id TEXT PRIMARY KEY, name VARCHAR(120) NOT NULL, node_type VARCHAR(24) NOT NULL, cluster_name VARCHAR(120) NOT NULL, agent_version VARCHAR(40) NOT NULL, capabilities TEXT NOT NULL, labels TEXT, credential_hash VARCHAR(64) NOT NULL, status VARCHAR(16) NOT NULL, last_seen_at DATETIME, last_seen_ip VARCHAR(39), revoked_at DATETIME, client_certificate_fingerprint TEXT, client_certificate_bound_at DATETIME, client_certificate_revoked_at DATETIME, tenant_id TEXT, project_id TEXT, created_by_id INTEGER, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_project (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL)",
        "CREATE TABLE core_projectmembership (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_tenantmembership (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_auditevent (id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, actor_id INTEGER, event_type VARCHAR(64) NOT NULL, resource_type VARCHAR(64) NOT NULL, resource_id TEXT, outcome VARCHAR(16) NOT NULL, request_id VARCHAR(64) NOT NULL, remote_addr VARCHAR(39), user_agent TEXT NOT NULL, metadata TEXT NOT NULL, previous_hash VARCHAR(64), event_hash VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentmachine (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, agent_node_id TEXT NOT NULL, vendor VARCHAR(256) NOT NULL, product_name VARCHAR(256) NOT NULL, system_uuid VARCHAR(128) NOT NULL, identity_material TEXT NOT NULL, identity_fingerprint VARCHAR(64) NOT NULL, identity_status VARCHAR(16) NOT NULL, identity_conflicts TEXT NOT NULL, hardware_inventory TEXT NOT NULL, inventory_source VARCHAR(48) NOT NULL, inventory_revision INTEGER NOT NULL, observed_at DATETIME, bmc_credential_ref VARCHAR(160) NOT NULL, bmc_endpoint VARCHAR(500) NOT NULL, bmc_trust_bundle_ref VARCHAR(160) NOT NULL, bmc_tls_verify BOOLEAN NOT NULL, bmc_configured_at DATETIME, bmc_identity_evidence TEXT NOT NULL, bmh_name VARCHAR(253) NOT NULL, capi_machine_name VARCHAR(253) NOT NULL, kubernetes_node_name VARCHAR(253) NOT NULL, lifecycle_state VARCHAR(32) NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_clusterdeployment (id TEXT PRIMARY KEY, cluster_name VARCHAR(63) NOT NULL, spec TEXT NOT NULL, status VARCHAR(32) NOT NULL, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, target_node_id TEXT, requested_by_id INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentcommand (id TEXT PRIMARY KEY, command_type VARCHAR(64) NOT NULL, payload TEXT NOT NULL, status VARCHAR(24) NOT NULL, idempotency_key VARCHAR(128) NOT NULL, lease_token VARCHAR(32), lease_expires_at DATETIME, result TEXT, error TEXT, attempts INTEGER NOT NULL, max_attempts INTEGER NOT NULL, plan_hash VARCHAR(64), approved_plan_hash VARCHAR(64), approved_at DATETIME, approved_by_id INTEGER, started_at DATETIME, finished_at DATETIME, deployment_id TEXT, node_id TEXT, tenant_id TEXT, project_id TEXT, requested_by_id INTEGER, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO platform_users (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1)")
        .execute(&pool).await.unwrap();
    let hash = hex::encode(Sha256::digest(TOKEN.as_bytes()));
    sqlx::query("INSERT INTO core_agentnode (id, name, node_type, cluster_name, agent_version, capabilities, credential_hash, status, tenant_id, project_id, created_at, updated_at) VALUES (?, 'node-a', 'target_cluster', 'lab', '1.0.0', '[\"exec\"]', ?, 'online', 'tenant', 'project', datetime('now'), datetime('now'))")
        .bind(NODE).bind(hash).execute(&pool).await.unwrap();
    sqlx::query(r#"INSERT INTO core_agentmachine (id, tenant_id, project_id, agent_node_id, vendor, product_name, system_uuid, identity_material, identity_fingerprint, identity_status, identity_conflicts, hardware_inventory, inventory_source, inventory_revision, bmc_credential_ref, bmc_endpoint, bmc_trust_bundle_ref, bmc_tls_verify, bmc_identity_evidence, bmh_name, capi_machine_name, kubernetes_node_name, lifecycle_state, created_at, updated_at) VALUES ('machine-1', 'tenant', 'project', ?, 'Dell', 'R750', 'uuid-1', '{}', '', 'verified', '[]', '{"cpu": 2}', 'agent_report', 3, 'bmc-cred', 'https://bmc.example.com', '', 1, '{}', 'bmh-1', '', '', 'commissioned', datetime('now'), datetime('now'))"#)
        .bind(NODE).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_clusterdeployment (id, cluster_name, spec, status, tenant_id, project_id, target_node_id, requested_by_id, created_at, updated_at) VALUES ('66666666666666666666666666666666', 'lab', '{\"kubernetes\": {\"directory\": \"/var/lib/etcd/backups\", \"keep_count\": 8}}', 'succeeded', 'tenant', 'project', ?, 1, datetime('now'), datetime('now'))")
        .bind(NODE).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_agentcommand (id, command_type, payload, status, idempotency_key, lease_token, lease_expires_at, attempts, max_attempts, created_at, updated_at, node_id, tenant_id, project_id, requested_by_id, deployment_id) VALUES (?, 'cluster.scale', '{}', 'running', 'key-1', ?, datetime('now', '+5 minutes'), 1, 3, datetime('now'), datetime('now'), ?, 'tenant', 'project', 1, '66666666666666666666666666666666')")
         .bind(COMMAND).bind(LEASE).bind(NODE).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_agentcommand (id, command_type, payload, status, idempotency_key, attempts, max_attempts, created_at, updated_at, node_id, tenant_id, project_id, requested_by_id) VALUES ('44444444444444444444444444444444', 'cluster.observe', '{}', 'queued', 'key-2', 0, 3, datetime('now'), datetime('now'), ?, 'tenant', 'project', 1)")
        .bind(NODE).execute(&pool).await.unwrap();
    sqlx::query(r#"INSERT INTO core_agentcommand (id, command_type, payload, status, idempotency_key, plan_hash, attempts, max_attempts, created_at, updated_at, node_id, tenant_id, project_id, requested_by_id) VALUES ('55555555555555555555555555555555', 'cluster.scale', '{"replicas":3}', 'waiting_approval', 'key-3', '70a2d11a46c7f689657269bbad9587111b8f38c00cddca74b550a2d0b803a0c9', 0, 3, datetime('now'), datetime('now'), ?, 'tenant', 'project', 2)"#)
        .bind(NODE).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_project (id, tenant_id) VALUES ('project', 'tenant')")
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES ('m1', 'tenant', 1, 'owner', datetime('now'))").execute(&pool).await.unwrap();
    let session = session_token_hash("approver-session");
    sqlx::query("INSERT INTO rust_sessions (token_hash, user_id, expires_at, created_at) VALUES (?, 1, datetime('now', '+1 day'), CURRENT_TIMESTAMP)").bind(session).execute(&pool).await.unwrap();
    pool.close().await;
    let config = Config::for_test("127.0.0.1:0".into(), url, Some("secret".to_string()));
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
        json!({"node_id": NODE, "outcome": "succeeded", "result": {"scale_performed": true}}),
        Some(LEASE),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["status"], "succeeded");
    let backups: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM core_agentcommand WHERE command_type = 'cluster.kubernetes.backup.run'",
    )
    .fetch_one(state.identity_pool.as_ref().unwrap())
    .await
    .unwrap();
    assert_eq!(backups, 1);

    let pool = state.identity_pool.as_ref().unwrap();
    let now = "2026-11-28 10:00:00";
    assert_eq!(
        ai_agent_stack_api::backup_queue::dispatch_hourly_backups(pool, now)
            .await
            .unwrap(),
        1
    );
    assert_eq!(
        ai_agent_stack_api::backup_queue::dispatch_hourly_backups(pool, now)
            .await
            .unwrap(),
        0
    );
    let backups: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM core_agentcommand WHERE command_type = 'cluster.kubernetes.backup.run'",
    )
    .fetch_one(pool)
    .await
    .unwrap();
    assert_eq!(backups, 2);

    let (status, body) = post(
        &state,
        &format!("/api/v1/agent-runtime/commands/{COMMAND}/result/"),
        json!({"node_id": NODE, "outcome": "succeeded", "result": {"scale_performed": true}}),
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

#[tokio::test]
async fn approves_waiting_command_with_matching_plan_hash() {
    let state = state().await;
    let request = Request::builder()
        .method("POST")
        .uri("/api/v1/agent-commands/55555555555555555555555555555555/approve/")
        .header(
            "cookie",
            "sessionid=approver-session; csrftoken=0123456789abcdef0123456789abcdef",
        )
        .header(
            "x-csrftoken",
            format!("{}{}", "a".repeat(32), "0123456789abcdef0123456789abcdef"),
        )
        .header("content-type", "application/json")
        .body(Body::from(
            json!({"plan_hash": "70a2d11a46c7f689657269bbad9587111b8f38c00cddca74b550a2d0b803a0c9"})
                .to_string(),
        ))
        .unwrap();
    let response = router(state).oneshot(request).await.unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 4096)
        .await
        .unwrap();
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["status"], "queued");
    assert_eq!(body["approved_at"].as_str().unwrap().len(), 19);
}

#[tokio::test]
async fn lists_commands_for_tenant_admin() {
    let state = state().await;
    let request = Request::builder()
        .method("GET")
        .uri(format!("/api/v1/agent-nodes/{NODE}/commands/"))
        .header(
            "cookie",
            "sessionid=approver-session; csrftoken=0123456789abcdef0123456789abcdef",
        )
        .body(Body::empty())
        .unwrap();
    let response = router(state).oneshot(request).await.unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 8192)
        .await
        .unwrap();
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["count"], 3);
    assert_eq!(body["results"][0]["node_id"], NODE);
}

#[tokio::test]
async fn lists_and_enrolls_agent_nodes() {
    let state = state().await;
    let request = Request::builder()
        .method("GET")
        .uri("/api/v1/tenants/tenant/projects/project/agent-nodes/")
        .header("cookie", "sessionid=approver-session")
        .body(Body::empty())
        .unwrap();
    let response = router(state.clone()).oneshot(request).await.unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 8192)
        .await
        .unwrap();
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["count"], 1);
    assert_eq!(body["results"][0]["status"], "online");

    let request = Request::builder()
        .method("POST")
        .uri("/api/v1/tenants/tenant/projects/project/agent-nodes/")
        .header(
            "cookie",
            "sessionid=approver-session; csrftoken=0123456789abcdef0123456789abcdef",
        )
        .header(
            "x-csrftoken",
            format!("{}{}", "a".repeat(32), "0123456789abcdef0123456789abcdef"),
        )
        .header("content-type", "application/json")
        .body(Body::from(
            json!({"name": "node-b", "node_type": "eph_node", "cluster_name": "lab", "capabilities": ["exec"]})
                .to_string(),
        ))
        .unwrap();
    let response = router(state).oneshot(request).await.unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 8192)
        .await
        .unwrap();
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(status, StatusCode::CREATED, "{body}");
    assert_eq!(body["name"], "node-b");
    assert_eq!(body["enrollment_token"].as_str().unwrap().len(), 64);
}

#[tokio::test]
async fn lists_project_commands_with_pagination() {
    let state = state().await;
    let request = Request::builder()
        .method("GET")
        .uri("/api/v1/tenants/tenant/projects/project/agent-commands/?limit=2&offset=0")
        .header("cookie", "sessionid=approver-session")
        .body(Body::empty())
        .unwrap();
    let response = router(state).oneshot(request).await.unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 8192)
        .await
        .unwrap();
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["count"], 3);
    assert_eq!(body["limit"], 2);
    assert_eq!(body["results"].as_array().unwrap().len(), 2);
}

async fn admin_post(state: &AppState, uri: &str) -> (StatusCode, Value) {
    let request = Request::builder()
        .method("POST")
        .uri(uri)
        .header(
            "cookie",
            "sessionid=approver-session; csrftoken=0123456789abcdef0123456789abcdef",
        )
        .header(
            "x-csrftoken",
            format!("{}{}", "a".repeat(32), "0123456789abcdef0123456789abcdef"),
        )
        .header("content-type", "application/json")
        .body(Body::from("{}"))
        .unwrap();
    let response = router(state.clone()).oneshot(request).await.unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 8192)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

#[tokio::test]
async fn blocks_credential_rotation_while_command_runs() {
    let state = state().await;
    let (status, body) = admin_post(
        &state,
        &format!("/api/v1/agent-nodes/{NODE}/credentials/rotate/"),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT, "{body}");
}

#[tokio::test]
async fn revokes_node_and_cancels_pending_commands() {
    let state = state().await;
    let (status, body) = admin_post(&state, &format!("/api/v1/agent-nodes/{NODE}/revoke/")).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["status"], "revoked");
    let (status, body) = admin_post(
        &state,
        &format!("/api/v1/agent-nodes/{NODE}/credentials/rotate/"),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT, "{body}");
}

async fn admin_request(
    state: &AppState,
    method: &str,
    uri: &str,
    body: Option<Value>,
) -> (StatusCode, Value) {
    let builder = Request::builder()
        .method(method)
        .uri(uri)
        .header(
            "cookie",
            "sessionid=approver-session; csrftoken=0123456789abcdef0123456789abcdef",
        )
        .header(
            "x-csrftoken",
            format!("{}{}", "a".repeat(32), "0123456789abcdef0123456789abcdef"),
        );
    let request = match body {
        Some(body) => builder
            .header("content-type", "application/json")
            .body(Body::from(body.to_string()))
            .unwrap(),
        None => builder.body(Body::empty()).unwrap(),
    };
    let response = router(state.clone()).oneshot(request).await.unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 8192)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

#[tokio::test]
async fn binds_and_revokes_client_certificate() {
    let state = state().await;
    let fingerprint = "a".repeat(64);
    let (status, body) = admin_request(
        &state,
        "PUT",
        &format!("/api/v1/agent-nodes/{NODE}/client-certificate/"),
        Some(json!({"fingerprint_sha256": fingerprint})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["client_certificate"]["status"], "active");
    assert_eq!(
        body["client_certificate"]["fingerprint_sha256"],
        fingerprint
    );

    let (status, body) = admin_request(
        &state,
        "DELETE",
        &format!("/api/v1/agent-nodes/{NODE}/client-certificate/"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["client_certificate"]["status"], "revoked");
}

async fn collect_network(state: &AppState) -> (StatusCode, Value) {
    let request = Request::builder()
        .method("POST")
        .uri("/api/v1/tenants/tenant/projects/project/fleet/network/collect/")
        .header(
            "cookie",
            "sessionid=approver-session; csrftoken=0123456789abcdef0123456789abcdef",
        )
        .header(
            "x-csrftoken",
            format!("{}{}", "a".repeat(32), "0123456789abcdef0123456789abcdef"),
        )
        .header("idempotency-key", "collect-1")
        .header("content-type", "application/json")
        .body(Body::from("{}"))
        .unwrap();
    let response = router(state.clone()).oneshot(request).await.unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 8192)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

#[tokio::test]
async fn queues_fleet_network_collection_idempotently() {
    let state = state().await;
    let (status, body) = collect_network(&state).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    assert_eq!(body["queued_count"], 1);
    let (status, body) = collect_network(&state).await;
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    assert_eq!(body["queued_count"], 0);
    assert_eq!(body["skipped_count"], 1);
}

#[tokio::test]
async fn lists_fleet_machines_with_identity_and_lifecycle() {
    let state = state().await;
    let request = Request::builder()
        .method("GET")
        .uri("/api/v1/tenants/tenant/projects/project/fleet/machines/?limit=10")
        .header(
            "cookie",
            "sessionid=approver-session; csrftoken=0123456789abcdef0123456789abcdef",
        )
        .body(Body::empty())
        .unwrap();
    let response = router(state).oneshot(request).await.unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 65536)
        .await
        .unwrap();
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["count"], 1);
    assert_eq!(body["results"][0]["machine_id"], "machine-1");
    assert_eq!(body["results"][0]["identity"]["system_uuid"], "uuid-1");
    assert_eq!(body["results"][0]["lifecycle_targets"][0], "retired");
    assert_eq!(body["results"][0]["bmc_configured"], true);
}

async fn inventory(state: &AppState, payload: Value) -> (StatusCode, Value) {
    let response = router(state.clone())
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/agent-runtime/inventory/")
                .header("authorization", format!("Bearer {TOKEN}"))
                .header("content-type", "application/json")
                .body(Body::from(payload.to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 65536)
        .await
        .unwrap();
    (status, serde_json::from_slice(&bytes).unwrap())
}

#[tokio::test]
async fn ingests_observed_inventory_and_reports_identity() {
    let state = state().await;
    let (status, body) = inventory(
        &state,
        json!({
            "node_id": NODE,
            "hardware_inventory": {
                "status": "observed",
                "system_identity": {
                    "sys_vendor": "Dell Inc.",
                    "product_name": "PowerEdge R750",
                    "product_uuid": "4c4c4544-0044-3510-8051-b4c04f4d4e31"
                },
                "nics": [{"name": "eno1"}]
            }
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["machine_id"], "machine-1");
    assert_eq!(body["agent_node_id"], NODE);
    assert_eq!(body["attestation"], "unverified_agent_report");
    assert_eq!(body["identity_status"], "conflict");
}

#[tokio::test]
async fn rejects_inventory_that_was_not_observed() {
    let state = state().await;
    let (status, body) = inventory(
        &state,
        json!({"node_id": NODE, "hardware_inventory": {"status": "stale"}}),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(
        body["error"]["message"],
        "Only observed hardware inventory can be ingested."
    );
}

#[tokio::test]
async fn rejects_inventory_larger_than_256_kib() {
    let state = state().await;
    let filler = "x".repeat(262145);
    let (status, body) = inventory(
        &state,
        json!({
            "node_id": NODE,
            "hardware_inventory": {"status": "observed", "filler": filler}
        }),
    )
    .await;
    assert_eq!(status, StatusCode::PAYLOAD_TOO_LARGE, "{body}");
}

#[tokio::test]
async fn creates_node_commands_idempotently_and_rejects_forbidden_payloads() {
    let state = state().await;
    let create = |key: &str, body: Value| {
        Request::builder()
            .method("POST")
            .uri(format!("/api/v1/agent-nodes/{NODE}/commands/"))
            .header(
                "cookie",
                "sessionid=approver-session; csrftoken=0123456789abcdef0123456789abcdef",
            )
            .header(
                "x-csrftoken",
                format!("{}{}", "a".repeat(32), "0123456789abcdef0123456789abcdef"),
            )
            .header("content-type", "application/json")
            .header("Idempotency-Key", key)
            .body(Body::from(body.to_string()))
            .unwrap()
    };

    let response = router(state.clone())
        .oneshot(create(
            "command-create-1",
            json!({"command_type": "cluster.observe", "payload": {"scope": "all"}}),
        ))
        .await
        .unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 8192)
        .await
        .unwrap();
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    assert_eq!(body["status"], "queued");
    assert_eq!(body["command_type"], "cluster.observe");
    let created_id = body["id"].as_str().unwrap().to_string();

    let response = router(state.clone())
        .oneshot(create(
            "command-create-1",
            json!({"command_type": "cluster.observe", "payload": {"scope": "all"}}),
        ))
        .await
        .unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 8192)
        .await
        .unwrap();
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["id"].as_str().unwrap(), created_id);

    let response = router(state.clone())
        .oneshot(create(
            "command-create-2",
            json!({"command_type": "cluster.observe", "payload": {"shell": "rm -rf /"}}),
        ))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);

    let response = router(state.clone())
        .oneshot(create(
            "command-create-3",
            json!({
                "command_type": "cluster.kubernetes.version.upgrade",
                "payload": {
                    "cluster_name": "lab",
                    "current_version": "v1.34.2",
                    "target_version": "v1.35.0",
                    "execution_mode": "simulation"
                }
            }),
        ))
        .await
        .unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 8192)
        .await
        .unwrap();
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(status, StatusCode::ACCEPTED, "{body}");
    assert_eq!(body["status"], "waiting_approval");

    let response = router(state.clone())
        .oneshot(create(
            "command-create-4",
            json!({
                "command_type": "cluster.kubernetes.version.upgrade",
                "payload": {
                    "cluster_name": "lab",
                    "current_version": "v1.34.2",
                    "target_version": "v1.36.0",
                    "execution_mode": "simulation"
                }
            }),
        ))
        .await
        .unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 8192)
        .await
        .unwrap();
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"]["code"], "VERSION_STEP_INVALID");
}
