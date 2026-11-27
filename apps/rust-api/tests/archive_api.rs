use std::time::Duration;

use ai_agent_stack_api_edge::config::Config;
use ai_agent_stack_api_edge::identity::encode_session_data;
use ai_agent_stack_api_edge::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::Value;
use tower::ServiceExt;

const SECRET: &str = "local-only-change-before-production";
const TENANT: &str = "11111111-1111-1111-1111-111111111111";
const PROJECT: &str = "22222222-2222-2222-2222-222222222222";
const SESSION: &str = "owner-session";

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-archive-{}.sqlite3", uuid::Uuid::new_v4()));
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
        "CREATE TABLE core_agentdefinition (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, description TEXT NOT NULL, draft_config TEXT NOT NULL, current_version INTEGER NOT NULL, is_archived BOOLEAN NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentteam (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, description TEXT NOT NULL, draft_config TEXT NOT NULL, current_version INTEGER NOT NULL, is_archived BOOLEAN NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_knowledgebase (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, embedding_model_id TEXT NOT NULL, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, description TEXT NOT NULL, chunk_size INTEGER NOT NULL, chunk_overlap INTEGER NOT NULL, is_archived BOOLEAN NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_archiveaction (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, item_type VARCHAR(40) NOT NULL, item_id VARCHAR(64) NOT NULL, action VARCHAR(10) NOT NULL, actor_id INTEGER NOT NULL, idempotency_key VARCHAR(128) NOT NULL, created_at DATETIME NOT NULL)",
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
    sqlx::query("INSERT INTO core_agentdefinition (id, tenant_id, project_id, created_by_id, name, slug, description, draft_config, current_version, is_archived, created_at, updated_at) VALUES ('agent-1', ?, ?, 1, 'Old Agent', 'old-agent', 'retired', '{}', 0, 1, datetime('now'), datetime('now'))")
        .bind(TENANT).bind(PROJECT).execute(&pool).await.unwrap();
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

async fn call(state: &AppState, method: &str, uri: &str) -> (StatusCode, Value) {
    let request = Request::builder()
        .method(method)
        .uri(uri)
        .header("x-session-token", SESSION)
        .body(Body::empty())
        .unwrap();
    let response = router(state.clone()).oneshot(request).await.unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 4 * 1024 * 1024)
        .await
        .unwrap();
    let body = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, body)
}

#[tokio::test]
async fn lists_and_restores_archived_agent() {
    let state = state().await;
    let base = format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/archive-items/");
    let (status, body) = call(&state, "GET", &base).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["count"], 1);
    assert_eq!(body["results"][0]["item_type"], "agent");

    let (status, body) = call(&state, "POST", &format!("{base}agent/agent-1/restore/")).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["already_restored"], false);
}

#[tokio::test]
async fn rejects_unknown_archive_type() {
    let state = state().await;
    let (status, body) = call(
        &state,
        "GET",
        &format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/archive-items/?type=bogus"),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
}
