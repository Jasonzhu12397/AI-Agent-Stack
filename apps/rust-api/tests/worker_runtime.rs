use ai_agent_stack_api::config::Config;
use ai_agent_stack_api::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use sqlx::Row;
use tower::ServiceExt;

const SECRET: &str = "local-only-change-before-production";
const TENANT: &str = "11111111-1111-1111-1111-111111111111";
const PROJECT: &str = "22222222-2222-2222-2222-222222222222";

async fn state() -> (AppState, String) {
    let path = std::env::temp_dir().join(format!("rust-worker-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE core_task (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, task_type VARCHAR(120) NOT NULL, payload TEXT NOT NULL, result TEXT, error TEXT, status VARCHAR(24) NOT NULL, idempotency_key VARCHAR(128) NOT NULL, attempts INTEGER NOT NULL, max_attempts INTEGER NOT NULL, available_at DATETIME NOT NULL, lease_owner VARCHAR(120), lease_token TEXT, lease_expires_at DATETIME, heartbeat_at DATETIME, cancel_requested_at DATETIME, started_at DATETIME, finished_at DATETIME, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, UNIQUE (tenant_id, idempotency_key))",
        "CREATE TABLE core_modelprovider (id TEXT PRIMARY KEY, tenant_id TEXT, name VARCHAR(120) NOT NULL, provider_type VARCHAR(40) NOT NULL, auth_mode VARCHAR(24) NOT NULL, preset_key VARCHAR(80), base_url VARCHAR(500), credential_ref VARCHAR(200), config TEXT NOT NULL, enabled BOOLEAN NOT NULL, deployment_region VARCHAR(80), created_by_id INTEGER, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_modelconfig (id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, model_name VARCHAR(160) NOT NULL, display_name VARCHAR(160) NOT NULL, capabilities TEXT NOT NULL, max_output_tokens INTEGER NOT NULL, enabled BOOLEAN NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentdefinition (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, description TEXT NOT NULL, draft_config TEXT NOT NULL, current_version INTEGER NOT NULL, is_archived BOOLEAN NOT NULL, created_by_id INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentversion (id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, version INTEGER NOT NULL, config_snapshot TEXT NOT NULL, created_by_id INTEGER NOT NULL, published_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentrun (id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE, agent_version_id TEXT NOT NULL, input_text TEXT NOT NULL, output_text TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, provider_name VARCHAR(120) NOT NULL, model_name VARCHAR(160) NOT NULL, latency_ms INTEGER NOT NULL, execution_state TEXT NOT NULL, created_at DATETIME NOT NULL, finished_at DATETIME)",
        "CREATE TABLE core_workflowdefinition (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, description TEXT NOT NULL, draft_definition TEXT NOT NULL, current_version INTEGER NOT NULL, is_archived BOOLEAN NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_workflowversion (id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, version INTEGER NOT NULL, definition_snapshot TEXT NOT NULL, created_by_id INTEGER NOT NULL, published_at DATETIME NOT NULL)",
        "CREATE TABLE core_workflowrun (id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE, workflow_version_id TEXT NOT NULL, input_data TEXT NOT NULL, state TEXT NOT NULL, output_data TEXT, deadline_at DATETIME, created_at DATETIME NOT NULL, finished_at DATETIME)",
        "CREATE TABLE core_workflowsteprun (id TEXT PRIMARY KEY, workflow_run_id TEXT NOT NULL, node_id VARCHAR(120) NOT NULL, node_type VARCHAR(32) NOT NULL, status VARCHAR(24) NOT NULL, input_data TEXT NOT NULL, output_data TEXT, error TEXT, attempt INTEGER NOT NULL, started_at DATETIME, finished_at DATETIME, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_tenanttokenbudget (id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL UNIQUE, daily_limit BIGINT NOT NULL, monthly_limit BIGINT NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_tenanttokenusage (id TEXT PRIMARY KEY, idempotency_key VARCHAR(160) NOT NULL, status VARCHAR(16) NOT NULL, reserved_tokens BIGINT NOT NULL, consumed_tokens BIGINT NOT NULL, expires_at DATETIME NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, agent_run_id TEXT, task_id TEXT NOT NULL, tenant_id TEXT NOT NULL, workflow_run_id TEXT, settled_at DATETIME, UNIQUE(tenant_id, idempotency_key))",
        "CREATE TABLE core_auditevent (id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, actor_id INTEGER, event_type VARCHAR(120) NOT NULL, resource_type VARCHAR(80) NOT NULL, resource_id VARCHAR(64) NOT NULL, outcome VARCHAR(16) NOT NULL, request_id VARCHAR(64) NOT NULL, remote_addr TEXT, user_agent VARCHAR(256) NOT NULL, metadata TEXT NOT NULL, previous_hash VARCHAR(64) NOT NULL, event_hash VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO core_modelprovider (id, tenant_id, name, provider_type, auth_mode, base_url, credential_ref, config, enabled, created_at, updated_at) VALUES ('provider-1', NULL, 'Fake', 'fake', 'tenant_secret', '', '', '{\"response\":\"worker answer\",\"tool_call\":false}', 1, datetime('now'), datetime('now'))").execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_modelconfig (id, provider_id, model_name, display_name, capabilities, max_output_tokens, enabled, created_at, updated_at) VALUES ('33333333-3333-3333-3333-333333333333', 'provider-1', 'fake-model', 'Fake', '[\"chat\"]', 512, 1, datetime('now'), datetime('now'))").execute(&pool).await.unwrap();
    pool.close().await;
    let config = Config::for_test("127.0.0.1:0".into(), url, Some(SECRET.to_string()));
    (AppState::new(config).unwrap(), path.display().to_string())
}

async fn task_id(pool: &sqlx::AnyPool) -> String {
    sqlx::query_scalar("SELECT CAST(id AS TEXT) FROM core_task WHERE status = 'queued' LIMIT 1")
        .fetch_one(pool)
        .await
        .unwrap()
}

#[tokio::test]
async fn worker_executes_agent_runs() {
    let (state, path) = state().await;
    let pool = state.identity_pool.as_ref().unwrap();
    sqlx::query("INSERT INTO core_agentdefinition (id, tenant_id, project_id, name, slug, description, draft_config, current_version, is_archived, created_by_id, created_at, updated_at) VALUES ('agent-1', ?, ?, 'Agent', 'agent', '', '{}', 1, 0, 1, datetime('now'), datetime('now'))")
        .bind(TENANT).bind(PROJECT).execute(pool).await.unwrap();
    sqlx::query("INSERT INTO core_agentversion (id, agent_id, version, config_snapshot, created_by_id, published_at) VALUES ('version-1', 'agent-1', 1, ?, 1, datetime('now'))")
        .bind(json!({"model_config_id":"33333333-3333-3333-3333-333333333333","system_prompt":"Answer briefly.","max_tokens":128,"max_iterations":2,"timeout_seconds":30,"knowledge_base_ids":[],"capability_allowlist":[]}).to_string())
        .execute(pool).await.unwrap();
    sqlx::query("INSERT INTO core_task (id, tenant_id, project_id, created_by_id, task_type, payload, status, idempotency_key, attempts, max_attempts, available_at, created_at, updated_at) VALUES ('task-1', ?, ?, 1, 'agent.run', '{}', 'queued', 'key-1', 0, 3, datetime('now'), datetime('now'), datetime('now'))")
        .bind(TENANT).bind(PROJECT).execute(pool).await.unwrap();
    sqlx::query("INSERT INTO core_agentrun (id, task_id, agent_version_id, input_text, output_text, input_tokens, output_tokens, provider_name, model_name, latency_ms, execution_state, created_at) VALUES ('run-1', 'task-1', 'version-1', 'hello', '', 0, 0, '', '', 0, '{}', datetime('now'))").execute(pool).await.unwrap();
    let id = task_id(pool).await;
    assert!(ai_agent_stack_api::worker::run_once(&state, "test-worker")
        .await
        .unwrap());
    let row = sqlx::query("SELECT status, result FROM core_task WHERE CAST(id AS TEXT) = ?")
        .bind(&id)
        .fetch_one(pool)
        .await
        .unwrap();
    assert_eq!(row.try_get::<String, _>("status").unwrap(), "succeeded");
    let result: Value = serde_json::from_str(&row.try_get::<String, _>("result").unwrap()).unwrap();
    assert_eq!(result["content"], "worker answer");
    let finished = sqlx::query_scalar::<_, Option<String>>(
        "SELECT CAST(finished_at AS TEXT) FROM core_agentrun WHERE CAST(id AS TEXT) = 'run-1'",
    )
    .fetch_one(pool)
    .await
    .unwrap();
    assert!(finished.is_some());
    let _ = path;
}

#[tokio::test]
async fn worker_executes_workflow_runs_and_records_steps() {
    let (state, path) = state().await;
    let pool = state.identity_pool.as_ref().unwrap();
    let definition = json!({
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "answer", "type": "llm", "config": {"model_config_id": "33333333-3333-3333-3333-333333333333", "prompt_template": "Say {{input}}", "system_prompt": "Be brief."}},
            {"id": "end", "type": "end", "config": {"source_node": "answer"}}
        ],
        "edges": [{"source": "start", "target": "answer"}, {"source": "answer", "target": "end"}],
        "ui": {"node_positions": {"start": {"x": 0, "y": 0}, "answer": {"x": 200, "y": 0}, "end": {"x": 400, "y": 0}}}
    });
    sqlx::query("INSERT INTO core_workflowdefinition (id, tenant_id, project_id, created_by_id, name, slug, description, draft_definition, current_version, is_archived, created_at, updated_at) VALUES ('workflow-1', ?, ?, 1, 'Flow', 'flow', '', '{}', 1, 0, datetime('now'), datetime('now'))")
        .bind(TENANT).bind(PROJECT).execute(pool).await.unwrap();
    sqlx::query("INSERT INTO core_workflowversion (id, workflow_id, version, definition_snapshot, created_by_id, published_at) VALUES ('workflow-version-1', 'workflow-1', 1, ?, 1, datetime('now'))")
        .bind(definition.to_string()).execute(pool).await.unwrap();
    sqlx::query("INSERT INTO core_task (id, tenant_id, project_id, created_by_id, task_type, payload, status, idempotency_key, attempts, max_attempts, available_at, created_at, updated_at) VALUES ('task-2', ?, ?, 1, 'workflow.run', '{}', 'queued', 'key-2', 0, 3, datetime('now'), datetime('now'), datetime('now'))")
        .bind(TENANT).bind(PROJECT).execute(pool).await.unwrap();
    sqlx::query("INSERT INTO core_workflowrun (id, task_id, workflow_version_id, input_data, state, created_at) VALUES ('run-2', 'task-2', 'workflow-version-1', '{\"message\":\"hi\"}', '{\"input\":{\"message\":\"hi\"}}', datetime('now'))").execute(pool).await.unwrap();
    let id = task_id(pool).await;
    assert!(ai_agent_stack_api::worker::run_once(&state, "test-worker")
        .await
        .unwrap());
    let row = sqlx::query("SELECT status, result, error FROM core_task WHERE CAST(id AS TEXT) = ?")
        .bind(&id)
        .fetch_one(pool)
        .await
        .unwrap();
    assert_eq!(row.try_get::<String, _>("status").unwrap(), "succeeded");
    let result: Value = serde_json::from_str(&row.try_get::<String, _>("result").unwrap()).unwrap();
    assert_eq!(result["output"]["content"], "worker answer");
    let steps = sqlx::query("SELECT node_id, status FROM core_workflowsteprun WHERE CAST(workflow_run_id AS TEXT) = 'run-2' ORDER BY node_id").fetch_all(pool).await.unwrap();
    assert_eq!(steps.len(), 3);
    assert!(steps
        .iter()
        .all(|step| step.try_get::<String, _>("status").unwrap() == "succeeded"));
    let run = sqlx::query("SELECT CAST(finished_at AS TEXT) AS finished_at, output_data FROM core_workflowrun WHERE CAST(id AS TEXT) = 'run-2'").fetch_one(pool).await.unwrap();
    assert!(run
        .try_get::<Option<String>, _>("finished_at")
        .unwrap()
        .is_some());
    assert!(run
        .try_get::<Option<String>, _>("output_data")
        .unwrap()
        .is_some());
    let _ = path;
}

#[tokio::test]
async fn task_worker_route_remains_authenticated() {
    let (state, _path) = state().await;
    let response = router(state)
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!(
                    "/api/v1/tenants/{TENANT}/projects/{PROJECT}/tasks/"
                ))
                .header("content-type", "application/json")
                .body(Body::from("{}"))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}
