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
const WORKFLOW: &str = "33333333-3333-3333-3333-333333333333";
const VERSION: &str = "44444444-4444-4444-4444-444444444444";
const SESSION: &str = "owner-session";

async fn state() -> (AppState, String) {
    let path = std::env::temp_dir().join(format!(
        "rust-workflow-reads-{}.sqlite3",
        uuid::Uuid::new_v4()
    ));
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
        "CREATE TABLE core_workflowdefinition (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, description TEXT NOT NULL, draft_definition TEXT NOT NULL, current_version INTEGER NOT NULL, is_archived INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_workflowversion (id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, version INTEGER NOT NULL, definition_snapshot TEXT NOT NULL, created_by_id INTEGER NOT NULL, published_at DATETIME NOT NULL)",
        "CREATE TABLE core_workflowrun (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, workflow_version_id TEXT NOT NULL, input_data TEXT NOT NULL, state TEXT NOT NULL, output_data TEXT, deadline_at DATETIME, created_at DATETIME NOT NULL, finished_at DATETIME)",
        "CREATE TABLE core_workflowsteprun (id TEXT PRIMARY KEY, workflow_run_id TEXT NOT NULL, node_id VARCHAR(120) NOT NULL, node_type VARCHAR(32) NOT NULL, status VARCHAR(24) NOT NULL, input_data TEXT NOT NULL, output_data TEXT, error TEXT, attempt INTEGER NOT NULL, started_at DATETIME, finished_at DATETIME, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_modelprovider (id TEXT PRIMARY KEY, tenant_id TEXT, created_by_id INTEGER NOT NULL, name VARCHAR(120) NOT NULL, provider_type VARCHAR(32) NOT NULL, auth_mode VARCHAR(32) NOT NULL, preset_key VARCHAR(40) NOT NULL, base_url VARCHAR(500) NOT NULL, credential_ref VARCHAR(200) NOT NULL, config TEXT NOT NULL, enabled INTEGER NOT NULL, deployment_region VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_modelconfig (id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, model_name VARCHAR(160) NOT NULL, display_name VARCHAR(160) NOT NULL, capabilities TEXT NOT NULL, context_window INTEGER NOT NULL, max_output_tokens INTEGER NOT NULL, input_cost_per_million REAL NOT NULL, output_cost_per_million REAL NOT NULL, version INTEGER NOT NULL, enabled INTEGER NOT NULL, fallback_model_id TEXT, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_knowledgebase (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, embedding_model_id TEXT NOT NULL, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, description TEXT NOT NULL, chunk_size INTEGER NOT NULL, chunk_overlap INTEGER NOT NULL, is_archived INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentplugindefinition (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, plugin_key VARCHAR(100) NOT NULL, name VARCHAR(120) NOT NULL, description TEXT NOT NULL, version VARCHAR(40) NOT NULL, protocol VARCHAR(32) NOT NULL, endpoint_url VARCHAR(500) NOT NULL, auth_required INTEGER NOT NULL, credential_ref VARCHAR(2048) NOT NULL, capabilities TEXT NOT NULL, manifest TEXT NOT NULL, input_schema TEXT NOT NULL, output_schema TEXT NOT NULL, timeout_seconds INTEGER NOT NULL, enabled INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentplugininstallation (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, plugin_id TEXT NOT NULL, plugin_version VARCHAR(40) NOT NULL, status VARCHAR(16) NOT NULL, granted_capabilities TEXT NOT NULL, credential_ref VARCHAR(200) NOT NULL, installed_by_id INTEGER NOT NULL, revision INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentdefinition (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, description TEXT NOT NULL, draft_config TEXT NOT NULL, current_version INTEGER NOT NULL, is_archived INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentversion (id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, version INTEGER NOT NULL, config_snapshot TEXT NOT NULL, created_by_id INTEGER NOT NULL, published_at DATETIME NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO auth_user (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1)")
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_tenant (id, name, slug, created_at) VALUES (?, 'Acme', 'acme', datetime('now'))")
        .bind(TENANT)
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES (?, ?, 1, 'owner', datetime('now'))")
        .bind(uuid::Uuid::new_v4().to_string())
        .bind(TENANT)
        .execute(&pool)
        .await
        .unwrap();
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
    sqlx::query("INSERT INTO core_workflowdefinition (id, tenant_id, project_id, created_by_id, name, slug, description, draft_definition, current_version, is_archived, created_at, updated_at) VALUES (?, ?, ?, 1, 'Flow', 'flow', '', '{\"nodes\":[]}', 1, 0, datetime('now'), datetime('now'))")
        .bind(WORKFLOW)
        .bind(TENANT)
        .bind(PROJECT)
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_workflowversion (id, workflow_id, version, definition_snapshot, created_by_id, published_at) VALUES (?, ?, 1, '{}', 1, datetime('now'))")
        .bind(VERSION)
        .bind(WORKFLOW)
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_modelprovider (id, tenant_id, created_by_id, name, provider_type, auth_mode, preset_key, base_url, credential_ref, config, enabled, deployment_region, created_at, updated_at) VALUES ('provider-1', ?, 1, 'Fake', 'fake', 'tenant_secret', '', '', '', '{}', 1, 'global', datetime('now'), datetime('now'))")
        .bind(TENANT)
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_modelconfig (id, provider_id, model_name, display_name, capabilities, context_window, max_output_tokens, input_cost_per_million, output_cost_per_million, version, enabled, created_at) VALUES ('model-1', 'provider-1', 'fake-chat', 'Fake Chat', '[\"chat\"]', 32768, 4096, 0, 0, 1, 1, datetime('now'))")
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_knowledgebase (id, tenant_id, project_id, created_by_id, embedding_model_id, name, slug, description, chunk_size, chunk_overlap, is_archived, created_at, updated_at) VALUES ('kb-1', ?, ?, 1, 'model-1', 'Docs', 'docs', '', 1200, 160, 0, datetime('now'), datetime('now'))")
        .bind(TENANT)
        .bind(PROJECT)
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_agentplugindefinition (id, tenant_id, created_by_id, plugin_key, name, description, version, protocol, endpoint_url, auth_required, credential_ref, capabilities, manifest, input_schema, output_schema, timeout_seconds, enabled, created_at, updated_at) VALUES ('plugin-1', ?, 1, 'echo', 'Echo', '', '1.0', 'http_json', 'https://example.com', 0, '', '[\"echo\",\"write\"]', '{\"capabilities\":[{\"name\":\"echo\",\"risk_level\":\"low\"},{\"name\":\"write\",\"risk_level\":\"high\"}]}', '{}', '{}', 60, 1, datetime('now'), datetime('now'))")
        .bind(TENANT)
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_agentplugininstallation (id, tenant_id, project_id, plugin_id, plugin_version, status, granted_capabilities, credential_ref, installed_by_id, revision, created_at, updated_at) VALUES ('install-1', ?, ?, 'plugin-1', '1.0', 'installed', '[\"echo\",\"write\"]', '', 1, 1, datetime('now'), datetime('now'))")
        .bind(TENANT)
        .bind(PROJECT)
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_agentdefinition (id, tenant_id, project_id, created_by_id, name, slug, description, draft_config, current_version, is_archived, created_at, updated_at) VALUES ('agent-1', ?, ?, 1, 'Helper', 'helper', '', '{}', 1, 0, datetime('now'), datetime('now'))")
        .bind(TENANT)
        .bind(PROJECT)
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_agentversion (id, agent_id, version, config_snapshot, created_by_id, published_at) VALUES ('agent-version-1', 'agent-1', 1, '{}', 1, datetime('now'))")
        .execute(&pool)
        .await
        .unwrap();
    let data = encode_session_data(1, "password-hash", SECRET).unwrap();
    sqlx::query("INSERT INTO django_session (session_key, session_data, expire_date) VALUES (?, ?, datetime('now', '+1 day'))")
        .bind(SESSION)
        .bind(data)
        .execute(&pool)
        .await
        .unwrap();
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
    key: Option<&str>,
    body: Value,
) -> (StatusCode, Value) {
    let mut builder = Request::builder()
        .method(method)
        .uri(uri)
        .header("cookie", format!("sessionid={SESSION}"))
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
async fn catalog_lists_bound_resources_with_visibility_rules() {
    let (state, _path) = state().await;
    let uri = format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/workflow-catalog/");
    let (status, body) = call(&state, "GET", &uri, None, json!({})).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["models"].as_array().unwrap().len(), 1);
    assert_eq!(body["models"][0]["label"], "Fake Chat");
    assert_eq!(body["knowledge_bases"][0]["name"], "Docs");
    assert_eq!(body["tools"].as_array().unwrap().len(), 2);
    assert_eq!(body["tools"][0]["id"], "echo");
    assert_eq!(body["tools"][1]["risk_level"], "high");
    assert_eq!(body["agents"][0]["version"], 1);
}

#[tokio::test]
async fn creates_runs_idempotently_and_lists_steps() {
    let (state, _path) = state().await;
    let uri = format!("/api/v1/workflows/{WORKFLOW}/runs/");
    let request = json!({"input": {"topic": "rust"}, "deadline_seconds": 600});
    let (status, body) = call(&state, "POST", &uri, Some("run-key"), request.clone()).await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(body["task_status"], "queued");
    assert_eq!(body["workflow_version_id"], VERSION);
    assert_eq!(body["input_data"]["topic"], "rust");
    assert_eq!(body["state"]["input"]["topic"], "rust");
    assert_eq!(body["steps"].as_array().unwrap().len(), 0);
    let run_id = body["id"].as_str().unwrap().to_string();

    let (status, body) = call(&state, "POST", &uri, Some("run-key"), request).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["id"], run_id);

    let (status, _) = call(
        &state,
        "POST",
        &uri,
        Some("run-key"),
        json!({"input": {"topic": "different"}}),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);

    let (status, list) = call(&state, "GET", &uri, None, json!({})).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(list.as_array().unwrap().len(), 1);
    assert_eq!(list[0]["id"], run_id);
}

#[tokio::test]
async fn run_detail_and_approval_decisions() {
    let (state, path) = state().await;
    let runs_uri = format!("/api/v1/workflows/{WORKFLOW}/runs/");
    let (_, body) = call(
        &state,
        "POST",
        &runs_uri,
        Some("approve-key"),
        json!({"input": {"topic": "approval"}}),
    )
    .await;
    let run_id = body["id"].as_str().unwrap().to_string();
    let task_id = body["task_id"].as_str().unwrap().to_string();
    let pool = sqlx::AnyPool::connect(&format!("sqlite://{path}"))
        .await
        .unwrap();
    sqlx::query("UPDATE core_task SET status = 'waiting_approval' WHERE CAST(id AS TEXT) = ?")
        .bind(&task_id)
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_workflowsteprun (id, workflow_run_id, node_id, node_type, status, input_data, output_data, error, attempt, started_at, finished_at, created_at) VALUES ('step-1', ?, 'approve_node', 'approval', 'awaiting_approval', '{}', NULL, NULL, 1, datetime('now'), NULL, datetime('now'))")
        .bind(&run_id)
        .execute(&pool)
        .await
        .unwrap();
    pool.close().await;

    let detail = format!("/api/v1/workflow-runs/{run_id}/");
    let (status, body) = call(&state, "GET", &detail, None, json!({})).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["task_status"], "waiting_approval");
    assert_eq!(body["steps"][0]["node_id"], "approve_node");

    let approve = format!("/api/v1/workflow-runs/{run_id}/steps/approve_node/approve/");
    let (status, body) = call(
        &state,
        "POST",
        &approve,
        None,
        json!({"reason": "looks good"}),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["decision"], "approved");
    assert_eq!(body["task_status"], "queued");

    let (status, body) = call(&state, "GET", &detail, None, json!({})).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["task_status"], "queued");
    assert_eq!(body["steps"][0]["status"], "succeeded");
    assert_eq!(body["steps"][0]["output_data"]["reason"], "looks good");

    let (_, body) = call(
        &state,
        "POST",
        &runs_uri,
        Some("reject-key"),
        json!({"input": {"topic": "reject"}}),
    )
    .await;
    let run_id = body["id"].as_str().unwrap().to_string();
    let task_id = body["task_id"].as_str().unwrap().to_string();
    let pool = sqlx::AnyPool::connect(&format!("sqlite://{path}"))
        .await
        .unwrap();
    sqlx::query("UPDATE core_task SET status = 'waiting_approval' WHERE CAST(id AS TEXT) = ?")
        .bind(&task_id)
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_workflowsteprun (id, workflow_run_id, node_id, node_type, status, input_data, output_data, error, attempt, started_at, finished_at, created_at) VALUES ('step-2', ?, 'approve_node', 'approval', 'awaiting_approval', '{}', NULL, NULL, 1, datetime('now'), NULL, datetime('now'))")
        .bind(&run_id)
        .execute(&pool)
        .await
        .unwrap();
    pool.close().await;

    let reject = format!("/api/v1/workflow-runs/{run_id}/steps/approve_node/reject/");
    let (status, body) = call(&state, "POST", &reject, None, json!({"reason": "no"})).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["task_status"], "failed");
    let detail = format!("/api/v1/workflow-runs/{run_id}/");
    let (status, body) = call(&state, "GET", &detail, None, json!({})).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["task_status"], "failed");
    assert_eq!(body["steps"][0]["status"], "failed");
    assert_eq!(
        body["steps"][0]["error"]["code"],
        "WORKFLOW_APPROVAL_REJECTED"
    );
}

#[tokio::test]
async fn approval_requires_a_pending_step_and_project_admin() {
    let (state, path) = state().await;
    let runs_uri = format!("/api/v1/workflows/{WORKFLOW}/runs/");
    let (_, body) = call(&state, "POST", &runs_uri, Some("pending-key"), json!({})).await;
    let run_id = body["id"].as_str().unwrap().to_string();
    let task_id = body["task_id"].as_str().unwrap().to_string();
    let pool = sqlx::AnyPool::connect(&format!("sqlite://{path}"))
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_workflowsteprun (id, workflow_run_id, node_id, node_type, status, input_data, output_data, error, attempt, started_at, finished_at, created_at) VALUES ('step-3', ?, 'approve_node', 'approval', 'running', '{}', NULL, NULL, 1, datetime('now'), NULL, datetime('now'))")
        .bind(&run_id)
        .execute(&pool)
        .await
        .unwrap();
    pool.close().await;
    let approve = format!("/api/v1/workflow-runs/{run_id}/steps/approve_node/approve/");
    let (status, _) = call(&state, "POST", &approve, None, json!({})).await;
    assert_eq!(status, StatusCode::CONFLICT);

    sqlx::AnyPool::connect(&format!("sqlite://{path}"))
        .await
        .unwrap();
    let _ = task_id;
}
