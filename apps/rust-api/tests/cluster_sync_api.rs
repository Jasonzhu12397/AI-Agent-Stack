
use ai_agent_stack_api::config::Config;
use ai_agent_stack_api::identity::encode_session_data;
use ai_agent_stack_api::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::Value;
use tower::ServiceExt;

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-eph-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE django_session (session_key VARCHAR(40) PRIMARY KEY, session_data TEXT NOT NULL, expire_date DATETIME NOT NULL)",
        "CREATE TABLE auth_user (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, email VARCHAR(254) NOT NULL, is_staff INTEGER NOT NULL, is_active INTEGER NOT NULL)",
        "CREATE TABLE core_tenantmembership (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_projectmembership (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_clusterdeployment (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, preflight_result TEXT, updated_at DATETIME)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO auth_user (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1)").execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES ('m1', 'tenant', 1, 'owner', datetime('now'))").execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_projectmembership (id, project_id, user_id, role, created_at) VALUES ('pm1', 'project', 1, 'editor', datetime('now'))").execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_clusterdeployment (id, tenant_id, project_id, preflight_result) VALUES ('deploy-1', 'tenant', 'project', '{}')").execute(&pool).await.unwrap();
    let session = encode_session_data(1, "password-hash", "secret").unwrap();
    sqlx::query("INSERT INTO django_session (session_key, session_data, expire_date) VALUES ('session-1', ?, datetime('now', '+1 day'))").bind(session).execute(&pool).await.unwrap();
    pool.close().await;
    let config = Config::for_test("127.0.0.1:0".into(), url, Some("secret".to_string()));
    AppState::new(config).unwrap()
}

async fn post(state: &AppState, id: &str) -> (StatusCode, Value) {
    let response = router(state.clone())
        .oneshot(
            Request::builder()
                .method("POST")
                .uri(format!(
                    "/api/v1/cluster-deployments/{id}/preflight/eph/sync/"
                ))
                .header(
                    "cookie",
                    "sessionid=session-1; csrftoken=0123456789abcdef0123456789abcdef",
                )
                .header(
                    "x-csrftoken",
                    format!("{}{}", "a".repeat(32), "0123456789abcdef0123456789abcdef"),
                )
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 1024 * 1024)
        .await
        .unwrap();
    (status, serde_json::from_slice(&bytes).unwrap())
}

#[tokio::test]
async fn unknown_deployment_is_not_found() {
    let state = state().await;
    let (status, body) = post(&state, "missing").await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{body}");
}

#[tokio::test]
async fn unconfigured_cloud_control_is_service_unavailable() {
    let state = state().await;
    let (status, body) = post(&state, "deploy-1").await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE, "{body}");
    assert_eq!(body["error"]["code"], "EPH_PREFLIGHT_UNAVAILABLE");
}

#[tokio::test]
async fn bmh_sync_is_unavailable_without_cloud_control() {
    let state = state().await;
    let response = router(state.clone())
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/v1/cluster-deployments/deploy-1/bmh-inventory/sync/")
                .header(
                    "cookie",
                    "sessionid=session-1; csrftoken=0123456789abcdef0123456789abcdef",
                )
                .header(
                    "x-csrftoken",
                    format!("{}{}", "a".repeat(32), "0123456789abcdef0123456789abcdef"),
                )
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::SERVICE_UNAVAILABLE);
}
