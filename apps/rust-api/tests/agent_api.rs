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
const SESSION: &str = "owner-session";

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-agent-{}.sqlite3", uuid::Uuid::new_v4()));
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
        "CREATE TABLE core_agentdefinition (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, description TEXT NOT NULL, draft_config TEXT NOT NULL, current_version INTEGER NOT NULL, is_archived BOOLEAN NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, UNIQUE (project_id, slug))",
        "CREATE TABLE core_agentversion (id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, version INTEGER NOT NULL, config_snapshot TEXT NOT NULL, created_by_id INTEGER NOT NULL, published_at DATETIME NOT NULL, UNIQUE (agent_id, version))",
        "CREATE TABLE core_task (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, task_type VARCHAR(120) NOT NULL, payload TEXT NOT NULL, result TEXT, error TEXT, status VARCHAR(24) NOT NULL, idempotency_key VARCHAR(128) NOT NULL, attempts INTEGER NOT NULL, max_attempts INTEGER NOT NULL, available_at DATETIME NOT NULL, lease_owner TEXT, lease_token TEXT, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, UNIQUE (tenant_id, idempotency_key))",
        "CREATE TABLE core_modelprovider (id TEXT PRIMARY KEY, tenant_id TEXT, created_by_id INTEGER, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, auth_mode VARCHAR(32) NOT NULL, enabled BOOLEAN NOT NULL)",
        "CREATE TABLE core_modelconfig (id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, name VARCHAR(160) NOT NULL, capabilities TEXT NOT NULL, max_output_tokens INTEGER NOT NULL, enabled BOOLEAN NOT NULL, version INTEGER NOT NULL)",
        "CREATE TABLE core_agentplugininstallation (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, plugin_id TEXT NOT NULL, status VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentplugindefinition (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, capabilities TEXT NOT NULL, enabled BOOLEAN NOT NULL)",
        "CREATE TABLE core_agentrun (id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE, agent_version_id TEXT NOT NULL, input_text TEXT NOT NULL, output_text TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, provider_name VARCHAR(120) NOT NULL, model_name VARCHAR(160) NOT NULL, latency_ms INTEGER NOT NULL, execution_state TEXT NOT NULL, created_at DATETIME NOT NULL, finished_at DATETIME)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO core_modelprovider (id, tenant_id, created_by_id, name, slug, auth_mode, enabled) VALUES ('provider-1', NULL, NULL, 'Platform', 'platform', 'platform_pool', 1)")
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_modelconfig (id, provider_id, name, capabilities, max_output_tokens, enabled, version) VALUES ('bbbbbbbb-1111-1111-1111-111111111111', 'provider-1', 'Echo', '[\"chat\",\"tools\"]', 4096, 1, 1)")
        .execute(&pool)
        .await
        .unwrap();
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
    AppState::new(config).unwrap()
}

async fn call(
    state: &AppState,
    method: &str,
    uri: &str,
    body: Option<Value>,
) -> (StatusCode, Value) {
    let request = Request::builder()
        .method(method)
        .uri(uri)
        .header("cookie", format!("sessionid={SESSION}"));
    let request = match body {
        Some(body) => request
            .header("content-type", "application/json")
            .body(Body::from(body.to_string()))
            .unwrap(),
        None => request.body(Body::empty()).unwrap(),
    };
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
async fn creates_lists_and_patches_agent_drafts() {
    let state = state().await;
    let collection = format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/agents/");
    let (status, body) = call(
        &state,
        "POST",
        &collection,
        Some(json!({"name": "Helper", "slug": "helper", "description": "Answers questions", "draft_config": {"model_config_id": "bbbbbbbb-1111-1111-1111-111111111111", "system_prompt": "Be brief."}})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    assert_eq!(body["name"], "Helper");
    assert_eq!(body["current_version"], 0);
    assert_eq!(body["is_archived"], false);
    assert_eq!(body["draft_config"]["system_prompt"], "Be brief.");
    assert!(body["created_at"].as_str().is_some());
    let agent = body["id"].as_str().unwrap().to_string();

    let (status, body) = call(
        &state,
        "POST",
        &collection,
        Some(json!({"name": "Duplicate", "slug": "helper"})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert!(body["error"]["details"]["slug"].is_array());

    let (status, body) = call(
        &state,
        "POST",
        &collection,
        Some(json!({"name": "Leaky", "slug": "leaky", "draft_config": {"api_key": "nope"}})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert!(body["error"]["details"]["draft_config"].is_array());

    let (status, body) = call(&state, "GET", &collection, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body.as_array().unwrap().len(), 1);
    assert_eq!(body[0]["id"], agent);

    let detail = format!("/api/v1/agents/{agent}/");
    let (status, body) = call(&state, "GET", &detail, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["slug"], "helper");

    let (status, body) = call(
        &state,
        "PATCH",
        &detail,
        Some(json!({"name": "Helper v2", "draft_config": {"system_prompt": "Be precise."}})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["name"], "Helper v2");
    assert_eq!(body["draft_config"]["system_prompt"], "Be precise.");

    let (status, body) = call(
        &state,
        "PATCH",
        &detail,
        Some(json!({"draft_config": {"model_config_id": "bbbbbbbb-1111-1111-1111-111111111111", "system_prompt": "Be precise."}})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["current_version"], 0);

    let (status, body) = call(
        &state,
        "POST",
        &format!("/api/v1/agents/{agent}/publish/"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    assert_eq!(body["version"], 1);
    assert_eq!(body["config_snapshot"]["max_iterations"], 4);
    assert!(body["published_at"].as_str().is_some());

    let runs = format!("/api/v1/agents/{agent}/runs/");
    let (status, body) = call(
        &state,
        "POST",
        &runs,
        Some(json!({"input": "Summarise the fleet"})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], "INVALID_REQUEST");

    let request = Request::builder()
        .method("POST")
        .uri(&runs)
        .header("cookie", format!("sessionid={SESSION}"))
        .header("content-type", "application/json")
        .header("idempotency-key", "agent-run-1")
        .body(Body::from(
            json!({"input": "Summarise the fleet"}).to_string(),
        ))
        .unwrap();
    let response = router(state.clone()).oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::ACCEPTED);
    let bytes = axum::body::to_bytes(response.into_body(), 4 * 1024 * 1024)
        .await
        .unwrap();
    let run: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(run["task_status"], "queued");
    assert_eq!(run["output_text"], "");
    let run_id = run["id"].as_str().unwrap().to_string();

    let (status, body) = call(&state, "GET", &format!("/api/v1/runs/{run_id}/"), None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["input_text"], "Summarise the fleet");

    let (status, body) = call(&state, "GET", &runs, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body.as_array().unwrap().len(), 1);
}
