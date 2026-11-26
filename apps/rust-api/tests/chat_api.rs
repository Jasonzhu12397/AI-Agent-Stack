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
    let path = std::env::temp_dir().join(format!("rust-chat-{}.sqlite3", uuid::Uuid::new_v4()));
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
        "CREATE TABLE core_modelprovider (id TEXT PRIMARY KEY, tenant_id TEXT, created_by_id INTEGER, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, provider_type VARCHAR(32) NOT NULL, auth_mode VARCHAR(32) NOT NULL, enabled BOOLEAN NOT NULL)",
        "CREATE TABLE core_modelconfig (id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, display_name VARCHAR(160) NOT NULL, model_name VARCHAR(160) NOT NULL, capabilities TEXT NOT NULL, context_window INTEGER NOT NULL, max_output_tokens INTEGER NOT NULL, enabled BOOLEAN NOT NULL, version INTEGER NOT NULL)",
        "CREATE TABLE core_agentchatthread (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, title VARCHAR(200) NOT NULL, model_config_id TEXT, archived BOOLEAN NOT NULL, execution_target VARCHAR(16) NOT NULL, effort VARCHAR(16) NOT NULL, goal VARCHAR(500) NOT NULL, plan_first BOOLEAN NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentchatmessage (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, role VARCHAR(16) NOT NULL, content TEXT NOT NULL, author_id INTEGER, context TEXT NOT NULL, usage TEXT NOT NULL, tool_calls TEXT NOT NULL, error_code VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentchatattachment (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, uploaded_by_id INTEGER NOT NULL, filename VARCHAR(255) NOT NULL, content_type VARCHAR(128) NOT NULL, size_bytes INTEGER NOT NULL, file VARCHAR(100) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentplugindefinition (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, plugin_key VARCHAR(100) NOT NULL, name VARCHAR(120) NOT NULL, description TEXT NOT NULL, version VARCHAR(40) NOT NULL, protocol VARCHAR(32) NOT NULL, endpoint_url VARCHAR(500) NOT NULL, auth_required BOOLEAN NOT NULL, credential_ref VARCHAR(2048) NOT NULL, capabilities TEXT NOT NULL, manifest TEXT NOT NULL, input_schema TEXT NOT NULL, output_schema TEXT NOT NULL, timeout_seconds INTEGER NOT NULL, max_concurrency INTEGER NOT NULL, enabled BOOLEAN NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentplugininstallation (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, plugin_id TEXT NOT NULL, plugin_version VARCHAR(40) NOT NULL, status VARCHAR(16) NOT NULL, granted_capabilities TEXT NOT NULL, credential_ref VARCHAR(200) NOT NULL, installed_by_id INTEGER NOT NULL, revision INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO auth_user (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1)")
        .execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenant (id, name, slug, created_at) VALUES (?, 'Acme', 'acme', datetime('now'))")
        .bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES (?, ?, 1, 'owner', datetime('now'))")
        .bind(uuid::Uuid::new_v4().to_string()).bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_project (id, tenant_id, name, slug, created_at) VALUES (?, ?, 'Console', 'console', datetime('now'))")
        .bind(PROJECT).bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_projectmembership (id, project_id, user_id, role, created_at) VALUES (?, ?, 1, 'admin', datetime('now'))")
        .bind(uuid::Uuid::new_v4().to_string()).bind(PROJECT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_modelprovider (id, tenant_id, created_by_id, name, slug, provider_type, auth_mode, enabled) VALUES ('provider-1', ?, 1, 'Local echo', 'local-echo', 'fake', 'platform_pool', 1)")
        .bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_modelconfig (id, provider_id, display_name, model_name, capabilities, context_window, max_output_tokens, enabled, version) VALUES ('bbbbbbbb-1111-1111-1111-111111111111', 'provider-1', 'Echo', 'demo-echo', '[\"chat\"]', 8192, 2048, 1, 1)")
        .execute(&pool).await.unwrap();
    let data = encode_session_data(1, "password-hash", SECRET).unwrap();
    sqlx::query("INSERT INTO django_session (session_key, session_data, expire_date) VALUES (?, ?, datetime('now', '+1 day'))")
        .bind(SESSION).bind(data).execute(&pool).await.unwrap();
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
async fn manages_chat_threads_in_real_storage() {
    let state = state().await;
    let collection = format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/agent-chat/threads/");
    let (status, body) = call(
        &state,
        "POST",
        &collection,
        Some(json!({"title": "  Fleet review  ", "execution_target": "local", "goal": "Check nodes"})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED, "{body}");
    assert_eq!(body["title"], "Fleet review");
    assert_eq!(body["execution_target"], "local");
    assert_eq!(
        body["model_config_id"],
        "bbbbbbbb-1111-1111-1111-111111111111"
    );
    let thread = body["id"].as_str().unwrap().to_string();

    let (status, body) = call(&state, "GET", &collection, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["items"].as_array().unwrap().len(), 1);
    assert_eq!(body["items"][0]["id"], thread);

    let detail = format!("/api/v1/agent-chat/threads/{thread}/");
    let (status, body) = call(&state, "GET", &detail, None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["messages"].as_array().unwrap().len(), 0);
    assert_eq!(body["attachments"].as_array().unwrap().len(), 0);

    let (status, body) = call(
        &state,
        "PATCH",
        &detail,
        Some(json!({"title": "Renamed", "archived": true})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["title"], "Renamed");
    assert_eq!(body["archived"], true);

    let (status, _) = call(&state, "DELETE", &detail, None).await;
    assert_eq!(status, StatusCode::NO_CONTENT);

    let context = format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/agent-chat/context/");
    let (status, body) = call(&state, "GET", &context, None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["project"]["id"], PROJECT);
    assert_eq!(body["models"][0]["model_name"], "demo-echo");
}
