use ai_agent_stack_api::config::Config;
use ai_agent_stack_api::identity::session_token_hash;
use ai_agent_stack_api::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;

const SECRET: &str = "local-only-change-before-production";
const TENANT: &str = "11111111-1111-1111-1111-111111111111";
const PROJECT: &str = "22222222-2222-2222-2222-222222222222";
const NODE: &str = "33333333-3333-3333-3333-333333333333";
const SESSION: &str = "owner-session";

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-etcd-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE django_session (session_key VARCHAR(40) PRIMARY KEY, session_data TEXT NOT NULL, expire_date DATETIME NOT NULL)",
        "CREATE TABLE auth_user (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, email VARCHAR(254) NOT NULL, is_staff INTEGER NOT NULL, is_active INTEGER NOT NULL)",

        "CREATE TABLE platform_users (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL UNIQUE, email VARCHAR(254) NOT NULL DEFAULT '', is_staff INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1)",

        "CREATE TABLE platform_credentials (user_id INTEGER NOT NULL, credential_type VARCHAR(32) NOT NULL, credential_hash TEXT NOT NULL, PRIMARY KEY (user_id, credential_type))",

        "CREATE TABLE rust_sessions (token_hash CHAR(64) PRIMARY KEY, user_id INTEGER NOT NULL, expires_at DATETIME NOT NULL, created_at DATETIME NOT NULL, revoked_at DATETIME)",
        "CREATE TABLE core_tenant (id TEXT PRIMARY KEY, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_tenantmembership (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_project (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_projectmembership (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_clusterdeployment (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, eph_node_id TEXT NOT NULL, target_node_id TEXT, requested_by_id INTEGER NOT NULL, cluster_name VARCHAR(120) NOT NULL, kubernetes_version VARCHAR(40) NOT NULL, spec TEXT NOT NULL, plan_result TEXT NOT NULL, preflight_result TEXT NOT NULL, status VARCHAR(32) NOT NULL, idempotency_key VARCHAR(128) NOT NULL, error TEXT NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, started_at DATETIME, finished_at DATETIME)",
        "CREATE TABLE core_agentcommand (id TEXT PRIMARY KEY, command_type VARCHAR(80) NOT NULL, payload TEXT NOT NULL, status VARCHAR(16) NOT NULL, idempotency_key VARCHAR(128) NOT NULL, plan_hash VARCHAR(64) NOT NULL, attempts INTEGER NOT NULL, result TEXT, error TEXT, started_at DATETIME, finished_at DATETIME, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, node_id TEXT, project_id TEXT NOT NULL, requested_by_id INTEGER, tenant_id TEXT NOT NULL, deployment_id TEXT)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO platform_users (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1)")
        .execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenant (id, name, slug, created_at) VALUES (?, 'Acme', 'acme', datetime('now'))")
        .bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES (?, ?, 1, 'owner', datetime('now'))")
        .bind(uuid::Uuid::new_v4().to_string()).bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_project (id, tenant_id, name, slug, created_at) VALUES (?, ?, 'Console', 'console', datetime('now'))")
        .bind(PROJECT).bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query(r#"INSERT INTO core_clusterdeployment (id, tenant_id, project_id, eph_node_id, target_node_id, requested_by_id, cluster_name, kubernetes_version, spec, plan_result, preflight_result, status, idempotency_key, error, created_at, updated_at, started_at, finished_at) VALUES ('deploy-1', ?, ?, ?, ?, 1, 'lab-cluster', 'v1.31.0', '{"kubernetes":{"etcd_backup_keep_count":4}}', '{}', '{}', 'ready', 'key-1', '{}', datetime('now'), datetime('now'), NULL, NULL)"#)
        .bind(TENANT).bind(PROJECT).bind(NODE).bind(NODE).execute(&pool).await.unwrap();
    let data = session_token_hash(SESSION);
    sqlx::query("INSERT INTO rust_sessions (token_hash, user_id, expires_at, created_at) VALUES (?, 1, datetime('now', '+1 day'), CURRENT_TIMESTAMP)").bind(&data).execute(&pool).await.unwrap();
    pool.close().await;
    let config = Config::for_test("127.0.0.1:0".into(), url, Some(SECRET.to_string()));
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
        .header("x-session-token", SESSION)
        .header("content-type", "application/json")
        .body(
            body.map(|value| Body::from(value.to_string()))
                .unwrap_or_else(Body::empty),
        )
        .unwrap();
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
async fn reads_etcd_backup_state() {
    let state = state().await;
    let (status, body) = call(
        &state,
        "GET",
        "/api/v1/cluster-deployments/deploy-1/etcd-backup/",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["cluster_name"], "lab-cluster");
    assert_eq!(body["policy"]["keep_count"], 4);
    assert_eq!(body["policy"]["enabled"], true);
    assert_eq!(body["ok"], false);
    assert_eq!(body["findings"][0]["code"], "ETCD_BACKUP_NOT_RUN_YET");
}

#[tokio::test]
async fn updates_backup_policy() {
    let state = state().await;
    let (status, body) = call(
        &state,
        "POST",
        "/api/v1/cluster-deployments/deploy-1/etcd-backup/policy/",
        Some(json!({"keep_count": 7, "enabled": false, "max_age_hours": 12})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["keep_count"], 7);
    assert_eq!(body["enabled"], false);
    assert_eq!(body["max_age_hours"], 12);
    let (status, body) = call(
        &state,
        "GET",
        "/api/v1/cluster-deployments/deploy-1/etcd-backup/",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["policy"]["keep_count"], 7);
    assert_eq!(body["policy"]["enabled"], false);
}

#[tokio::test]
async fn rejects_invalid_policy_values() {
    let state = state().await;
    let (status, body) = call(
        &state,
        "POST",
        "/api/v1/cluster-deployments/deploy-1/etcd-backup/policy/",
        Some(json!({"keep_count": 1})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["code"], "ETCD_BACKUP_RETENTION_INVALID");
    let (status, _) = call(
        &state,
        "POST",
        "/api/v1/cluster-deployments/deploy-1/etcd-backup/policy/",
        Some(json!({})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn queues_one_backup_per_hour() {
    let state = state().await;
    let (status, body) = call(
        &state,
        "POST",
        "/api/v1/cluster-deployments/deploy-1/etcd-backup/run/",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["created"], true);
    assert_eq!(body["status"], "queued");
    let (status, body) = call(
        &state,
        "POST",
        "/api/v1/cluster-deployments/deploy-1/etcd-backup/run/",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["created"], false);
    assert_eq!(body["code"], "ETCD_BACKUP_ALREADY_QUEUED");
}

#[tokio::test]
async fn refuses_backup_while_disabled() {
    let state = state().await;
    let (status, _) = call(
        &state,
        "POST",
        "/api/v1/cluster-deployments/deploy-1/etcd-backup/policy/",
        Some(json!({"enabled": false})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let (status, body) = call(
        &state,
        "POST",
        "/api/v1/cluster-deployments/deploy-1/etcd-backup/check/",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT, "{body}");
    assert_eq!(body["code"], "ETCD_BACKUP_DISABLED");
}
