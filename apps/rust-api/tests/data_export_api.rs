use ai_agent_stack_api::config::Config;
use ai_agent_stack_api::identity::session_token_hash;
use ai_agent_stack_api::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;

const SECRET: &str = "local-only-change-before-production";
const TENANT: &str = "11111111-1111-1111-1111-111111111111";
const SESSION: &str = "owner-session";

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-export-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE core_tenant (id TEXT PRIMARY KEY, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_tenantmembership (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_project (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_task (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, task_type VARCHAR(120) NOT NULL, status VARCHAR(24) NOT NULL, attempts INTEGER NOT NULL, max_attempts INTEGER NOT NULL, created_at DATETIME NOT NULL, started_at DATETIME, finished_at DATETIME)",
        "CREATE TABLE core_agentrun (id TEXT PRIMARY KEY, task_id TEXT NOT NULL, provider_name VARCHAR(120) NOT NULL, model_name VARCHAR(160) NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, latency_ms INTEGER NOT NULL, created_at DATETIME NOT NULL, finished_at DATETIME)",
        "CREATE TABLE core_auditevent (id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, event_type VARCHAR(120) NOT NULL, resource_type VARCHAR(80) NOT NULL, resource_id VARCHAR(64) NOT NULL, outcome VARCHAR(16) NOT NULL, metadata TEXT NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_tenantsetting (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, section VARCHAR(32) NOT NULL, \"values\" TEXT NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_dataexportjob (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT, requested_by_id INTEGER NOT NULL, scope TEXT NOT NULL, status VARCHAR(16) NOT NULL, idempotency_key VARCHAR(128) NOT NULL, row_count INTEGER NOT NULL, file_ref VARCHAR(500) NOT NULL, expires_at DATETIME, error_code VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, UNIQUE (tenant_id, idempotency_key))",
        "CREATE TABLE platform_users (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL UNIQUE, email VARCHAR(254) NOT NULL DEFAULT '', is_staff INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1)",
        "CREATE TABLE rust_sessions (token_hash CHAR(64) PRIMARY KEY, user_id INTEGER NOT NULL, expires_at DATETIME NOT NULL, created_at DATETIME NOT NULL, revoked_at DATETIME)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO platform_users (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1)")
        .execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenant (id, name, slug, created_at) VALUES (?, 'Acme', 'acme', datetime('now'))")
        .bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES ('m1', ?, 1, 'owner', datetime('now'))")
        .bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_project (id, tenant_id, name, slug, created_at) VALUES ('p1', ?, 'Console', 'console', datetime('now'))")
        .bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_task (id, tenant_id, project_id, task_type, status, attempts, max_attempts, created_at) VALUES ('t1', ?, 'p1', 'agent.run', 'succeeded', 1, 3, datetime('now'))")
        .bind(TENANT).execute(&pool).await.unwrap();
    let data = session_token_hash(SESSION);
    sqlx::query("INSERT INTO rust_sessions (token_hash, user_id, expires_at, created_at) VALUES (?, 1, datetime('now', '+1 day'), CURRENT_TIMESTAMP)")
        .bind(&data).execute(&pool).await.unwrap();
    pool.close().await;
    let mut config = Config::for_test("127.0.0.1:0".into(), url, Some(SECRET.to_string()));
    config.media_root = std::env::temp_dir()
        .join(format!("rust-export-media-{}", uuid::Uuid::new_v4()))
        .to_string_lossy()
        .to_string();
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
        .header("x-session-token", SESSION);
    let request = match body {
        Some(body) => request
            .header("content-type", "application/json")
            .body(Body::from(body.to_string()))
            .unwrap(),
        None => request.body(Body::empty()).unwrap(),
    };
    let response = router(state.clone()).oneshot(request).await.unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 8 * 1024 * 1024)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

#[tokio::test]
async fn creates_lists_and_expires_tenant_data_exports() {
    let state = state().await;
    let collection = format!("/api/v1/tenants/{TENANT}/data-controls/exports/");
    let (status, body) = call(
        &state,
        "POST",
        &collection,
        Some(json!({"idempotency_key": "export-1", "scope": {"kinds": ["tasks"]}})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED, "{body}");
    assert_eq!(body["status"], "succeeded");
    assert_eq!(body["row_count"], 1);
    let file_ref = body["file_ref"].as_str().unwrap();
    let content = std::fs::read_to_string(file_ref).unwrap();
    assert!(content.contains("\"tasks\""));

    let (status, body) = call(
        &state,
        "POST",
        &collection,
        Some(json!({"idempotency_key": "export-1", "scope": {"kinds": ["tasks"]}})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(
        body["id"],
        "export-1".replace("export-1", body["id"].as_str().unwrap())
    );

    let (status, body) = call(&state, "GET", &collection, None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["results"].as_array().unwrap().len(), 1);
    assert_eq!(body["results"][0]["file_ref"], Value::Null);

    let job_id = body["results"][0]["id"].as_str().unwrap().to_string();
    let (status, body) = call(
        &state,
        "GET",
        &format!("/api/v1/tenants/{TENANT}/data-controls/exports/{job_id}/"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["file_ref"], file_ref);

    let (status, body) = call(
        &state,
        "POST",
        &format!("/api/v1/tenants/{TENANT}/data-controls/exports/{job_id}/expire/"),
        Some(json!({})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["status"], "expired");
}
