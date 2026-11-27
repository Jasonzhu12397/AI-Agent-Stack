use std::time::Duration;

use ai_agent_stack_api_edge::config::Config;
use ai_agent_stack_api_edge::identity::encode_session_data;
use ai_agent_stack_api_edge::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::Value;
use tower::ServiceExt;

const SECRET: &str = "secret";

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-privacy-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE django_session (session_key VARCHAR(40) PRIMARY KEY, session_data TEXT NOT NULL, expire_date DATETIME NOT NULL)",
        "CREATE TABLE auth_user (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, email VARCHAR(254) NOT NULL, is_staff INTEGER NOT NULL, is_active INTEGER NOT NULL)",
        "CREATE TABLE core_tenantmembership (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentplugin (id TEXT PRIMARY KEY, name VARCHAR(120) NOT NULL, plugin_key VARCHAR(64) NOT NULL)",
        "CREATE TABLE core_agentplugininstallation (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, plugin_id TEXT NOT NULL, plugin_version VARCHAR(40) NOT NULL, status VARCHAR(16) NOT NULL, granted_capabilities TEXT NOT NULL, credential_ref VARCHAR(200) NOT NULL, installed_by_id INTEGER NOT NULL, revision INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_auditevent (id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, actor_id INTEGER, event_type VARCHAR(64) NOT NULL, resource_type VARCHAR(64) NOT NULL, resource_id TEXT, outcome VARCHAR(16) NOT NULL, request_id VARCHAR(64) NOT NULL, remote_addr VARCHAR(39), user_agent TEXT NOT NULL, metadata TEXT NOT NULL, previous_hash VARCHAR(64), event_hash VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO auth_user (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1)")
        .execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES ('m1', 'tenant', 1, 'owner', datetime('now'))")
        .execute(&pool).await.unwrap();
    sqlx::query(
        "INSERT INTO core_agentplugin (id, name, plugin_key) VALUES ('p1', 'GitHub', 'github')",
    )
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query("INSERT INTO core_agentplugininstallation (id, tenant_id, project_id, plugin_id, plugin_version, status, granted_capabilities, credential_ref, installed_by_id, revision, created_at, updated_at) VALUES ('i1', 'tenant', 'project', 'p1', '1.0.0', 'installed', '[\"repo:read\"]', '', 1, 1, datetime('now'), datetime('now'))")
        .execute(&pool).await.unwrap();
    let session = encode_session_data(1, "password-hash", SECRET).unwrap();
    sqlx::query("INSERT INTO django_session (session_key, session_data, expire_date) VALUES ('privacy-session', ?, datetime('now', '+1 day'))")
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

#[tokio::test]
async fn lists_privacy_grants() {
    let state = state().await;
    let request = Request::builder()
        .method("GET")
        .uri("/api/v1/tenants/tenant/privacy/grants/")
        .header("cookie", "sessionid=privacy-session")
        .body(Body::empty())
        .unwrap();
    let response = router(state).oneshot(request).await.unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 8192)
        .await
        .unwrap();
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(status, StatusCode::OK, "{body}");
    let results = body["results"].as_array().unwrap();
    assert_eq!(results.len(), 1);
    assert_eq!(results[0]["connector_slug"], "github");
    assert_eq!(results[0]["scopes"][0], "repo:read");
}
