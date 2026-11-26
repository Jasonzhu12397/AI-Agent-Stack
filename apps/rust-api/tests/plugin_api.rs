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
const PLUGIN: &str = "33333333-3333-3333-3333-333333333333";
const INSTALLATION: &str = "44444444-4444-4444-4444-444444444444";
const SESSION: &str = "owner-session";

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-plugin-{}.sqlite3", uuid::Uuid::new_v4()));
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
        "CREATE TABLE core_agentplugindefinition (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, plugin_key VARCHAR(100) NOT NULL, name VARCHAR(120) NOT NULL, description TEXT NOT NULL, version VARCHAR(40) NOT NULL, protocol VARCHAR(32) NOT NULL, endpoint_url VARCHAR(500) NOT NULL, auth_required BOOLEAN NOT NULL, credential_ref VARCHAR(2048) NOT NULL, capabilities TEXT NOT NULL, manifest TEXT NOT NULL, input_schema TEXT NOT NULL, output_schema TEXT NOT NULL, timeout_seconds INTEGER NOT NULL, max_concurrency INTEGER NOT NULL, enabled BOOLEAN NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentplugininstallation (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, plugin_id TEXT NOT NULL, plugin_version VARCHAR(40) NOT NULL, status VARCHAR(16) NOT NULL, granted_capabilities TEXT NOT NULL, credential_ref VARCHAR(200) NOT NULL, installed_by_id INTEGER NOT NULL, revision INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
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
    sqlx::query("INSERT INTO core_projectmembership (id, project_id, user_id, role, created_at) VALUES (?, ?, 1, 'editor', datetime('now'))")
        .bind(uuid::Uuid::new_v4().to_string())
        .bind(PROJECT)
        .execute(&pool)
        .await
        .unwrap();
    let manifest = json!({"capabilities": [{"name": "weather.lookup", "risk_level": "low", "description": "Lookup weather"}]});
    sqlx::query("INSERT INTO core_agentplugindefinition (id, tenant_id, created_by_id, plugin_key, name, description, version, protocol, endpoint_url, auth_required, credential_ref, capabilities, manifest, input_schema, output_schema, timeout_seconds, max_concurrency, enabled, created_at, updated_at) VALUES (?, ?, 1, 'weather', 'Weather', '', '1.0.0', 'http_json', 'https://example.com', 0, '', '[]', ?, '{}', '{}', 60, 4, 1, datetime('now'), datetime('now'))")
        .bind(PLUGIN)
        .bind(TENANT)
        .bind(manifest.to_string())
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_agentplugininstallation (id, tenant_id, project_id, plugin_id, plugin_version, status, granted_capabilities, credential_ref, installed_by_id, revision, created_at, updated_at) VALUES (?, ?, ?, ?, '1.0.0', 'installed', '[]', '', 1, 1, datetime('now'), datetime('now'))")
        .bind(INSTALLATION)
        .bind(TENANT)
        .bind(PROJECT)
        .bind(PLUGIN)
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
async fn lists_plugins_and_changes_installation_status() {
    let state = state().await;
    let (status, body) = call(
        &state,
        "GET",
        &format!("/api/v1/tenants/{TENANT}/agent-plugins/"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body.as_array().unwrap().len(), 1);
    assert_eq!(body[0]["plugin_key"], "weather");
    assert_eq!(body[0]["capabilities"][0], "weather.lookup");
    assert_eq!(body[0]["capability_definitions"][0]["risk_level"], "low");
    assert_eq!(body[0]["credential_configured"], false);

    let installations =
        format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/agent-plugin-installations/");
    let (status, body) = call(&state, "GET", &installations, None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body.as_array().unwrap().len(), 1);
    assert_eq!(body[0]["plugin"]["name"], "Weather");
    assert_eq!(body[0]["status"], "installed");

    let (status, body) = call(
        &state,
        "POST",
        &format!("/api/v1/agent-plugin-installations/{INSTALLATION}/status/"),
        Some(json!({"status": "disabled"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["status"], "disabled");

    let (status, _) = call(
        &state,
        "POST",
        &format!("/api/v1/agent-plugin-installations/{INSTALLATION}/status/"),
        Some(json!({"status": "bogus"})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
}
