use std::time::Duration;

use ai_agent_stack_api_edge::config::Config;
use ai_agent_stack_api_edge::identity::encode_session_data;
use ai_agent_stack_api_edge::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;

const SECRET: &str = "secret";

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-teams-{}.sqlite3", uuid::Uuid::new_v4()));
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
        "CREATE TABLE core_agentteam (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, description TEXT NOT NULL, draft_config TEXT NOT NULL, current_version INTEGER NOT NULL, is_archived INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, UNIQUE(project_id, slug))",
        "CREATE TABLE core_agentteamversion (id TEXT PRIMARY KEY, team_id TEXT NOT NULL, version INTEGER NOT NULL, definition_snapshot TEXT NOT NULL, workflow_version_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, published_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentteamrun (id TEXT PRIMARY KEY, team_version_id TEXT NOT NULL, workflow_run_id TEXT NOT NULL, input_text TEXT NOT NULL, output_text TEXT, created_at DATETIME NOT NULL, finished_at DATETIME)",
        "CREATE TABLE core_workflowdefinition (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, description TEXT NOT NULL, draft_definition TEXT NOT NULL, current_version INTEGER NOT NULL, is_archived INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, UNIQUE(project_id, slug))",
        "CREATE TABLE core_workflowversion (id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, version INTEGER NOT NULL, definition_snapshot TEXT NOT NULL, created_by_id INTEGER NOT NULL, published_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentdefinition (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, description TEXT NOT NULL, draft_config TEXT NOT NULL, current_version INTEGER NOT NULL, is_archived INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, UNIQUE(project_id, slug))",
        "CREATE TABLE core_agentversion (id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, version INTEGER NOT NULL, config_snapshot TEXT NOT NULL, created_by_id INTEGER NOT NULL, published_at DATETIME NOT NULL)",
        "CREATE TABLE core_workflowrun (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, workflow_version_id TEXT NOT NULL, input_data TEXT NOT NULL)",
        "CREATE TABLE core_task (id TEXT PRIMARY KEY, status VARCHAR(16) NOT NULL)",
        "CREATE TABLE core_auditevent (id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, actor_id INTEGER, event_type VARCHAR(64) NOT NULL, resource_type VARCHAR(64) NOT NULL, resource_id TEXT, outcome VARCHAR(16) NOT NULL, request_id VARCHAR(64) NOT NULL, remote_addr VARCHAR(39), user_agent TEXT NOT NULL, metadata TEXT NOT NULL, previous_hash VARCHAR(64), event_hash VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO auth_user (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1)").execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES ('m1', 'tenant', 1, 'owner', datetime('now'))").execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_project (id, tenant_id) VALUES ('project', 'tenant')")
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_agentteam (id, tenant_id, project_id, created_by_id, name, slug, description, draft_config, current_version, is_archived, created_at, updated_at) VALUES ('team-1', 'tenant', 'project', 1, 'Fixture Team', 'fixture-team', '', '{\"strategy\":\"sequential\",\"members\":[{\"agent_id\":\"11111111-1111-1111-1111-111111111111\",\"role\":\"analyst\",\"instructions\":\"Investigate the incident.\"}]}', 1, 0, datetime('now'), datetime('now'))").execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_agentteamversion (id, team_id, version, definition_snapshot, workflow_version_id, created_by_id, published_at) VALUES ('version-1', 'team-1', 1, '{}', 'workflow-version-1', 1, datetime('now'))").execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_agentdefinition (id, tenant_id, project_id, created_by_id, name, slug, description, draft_config, current_version, is_archived, created_at, updated_at) VALUES ('11111111-1111-1111-1111-111111111111', 'tenant', 'project', 1, 'Analyst', 'analyst', '', '{}', 1, 0, datetime('now'), datetime('now'))").execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_agentversion (id, agent_id, version, config_snapshot, created_by_id, published_at) VALUES ('11111111-1111-1111-1111-222222222222', '11111111-1111-1111-1111-111111111111', 1, '{\"tool_allowlist\":[]}', 1, datetime('now'))").execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_task (id, status) VALUES ('task-1', 'running')")
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_workflowrun (id, task_id, workflow_version_id, input_data) VALUES ('workflow-run-1', 'task-1', 'workflow-version-1', '{}')").execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_agentteamrun (id, team_version_id, workflow_run_id, input_text, output_text, created_at, finished_at) VALUES ('run-1', 'version-1', 'workflow-run-1', 'investigate', NULL, datetime('now'), NULL)").execute(&pool).await.unwrap();
    let session = encode_session_data(1, "password-hash", SECRET).unwrap();
    sqlx::query("INSERT INTO django_session (session_key, session_data, expire_date) VALUES ('team-session', ?, datetime('now', '+1 day'))")
        .bind(session).execute(&pool).await.unwrap();
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
    let builder = Request::builder()
        .method(method)
        .uri(uri)
        .header(
            "cookie",
            "sessionid=team-session; csrftoken=0123456789abcdef0123456789abcdef",
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
async fn creates_lists_and_updates_team() {
    let state = state().await;
    let (status, body) = call(
        &state,
        "POST",
        "/api/v1/tenants/tenant/projects/project/agent-teams/",
        Some(json!({"name": "Ops Team", "slug": "ops-team", "draft_config": {"members": []}})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED, "{body}");
    let team_id = body["id"].as_str().unwrap().to_string();
    assert_eq!(body["slug"], "ops-team");

    let (status, body) = call(
        &state,
        "GET",
        "/api/v1/tenants/tenant/projects/project/agent-teams/",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body.as_array().unwrap().len(), 2);

    let (status, body) = call(
        &state,
        "PATCH",
        &format!("/api/v1/agent-teams/{team_id}/"),
        Some(json!({"description": "night shift"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["description"], "night shift");
}

#[tokio::test]
async fn rejects_secret_material_in_draft_config() {
    let state = state().await;
    let (status, body) = call(
        &state,
        "POST",
        "/api/v1/tenants/tenant/projects/project/agent-teams/",
        Some(json!({"name": "Bad", "slug": "bad", "draft_config": {"api_key": "secret"}})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
}

#[tokio::test]
async fn lists_team_runs_and_reads_run_detail() {
    let state = state().await;
    let (status, body) = call(&state, "GET", "/api/v1/agent-teams/team-1/runs/", None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body.as_array().unwrap().len(), 1);
    assert_eq!(body[0]["task_status"], "running");

    let (status, body) = call(&state, "GET", "/api/v1/agent-team-runs/run-1/", None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["id"], "run-1");
    assert_eq!(body["task_id"], "task-1");
}

#[tokio::test]
async fn publishes_team_and_creates_workflow_version() {
    let state = state().await;
    let (status, body) = call(&state, "POST", "/api/v1/agent-teams/team-1/publish/", None).await;
    assert_eq!(status, StatusCode::CREATED, "{body}");
    assert_eq!(body["version"], 2);
    assert!(body["workflow_version_id"].as_str().is_some());
    let snapshot = &body["definition_snapshot"];
    assert_eq!(snapshot["strategy"], "sequential");
    assert_eq!(snapshot["members"][0]["role"], "analyst");

    let database = state.identity_pool.clone().expect("identity pool");
    let count: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM core_workflowdefinition WHERE slug = 'agent-team-team1'",
    )
    .fetch_one(&database)
    .await
    .unwrap();
    assert_eq!(count, 1);
    let versions: i64 = sqlx::query_scalar(
        "SELECT COUNT(*) FROM core_workflowversion WHERE CAST(workflow_id AS TEXT) = CAST((SELECT id FROM core_workflowdefinition WHERE slug = 'agent-team-team1') AS TEXT)",
    )
    .fetch_one(&database)
    .await
    .unwrap();
    assert_eq!(versions, 1);
}
