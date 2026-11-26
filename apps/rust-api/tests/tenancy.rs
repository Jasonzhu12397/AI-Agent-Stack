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
const OWNER_SESSION: &str = "owner-session";
const ADMIN_SESSION: &str = "admin-session";
const MEMBER_SESSION: &str = "member-session";

async fn state() -> (AppState, String) {
    let path = std::env::temp_dir().join(format!("rust-tenancy-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE django_session (session_key VARCHAR(40) PRIMARY KEY, session_data TEXT NOT NULL, expire_date DATETIME NOT NULL)",
        "CREATE TABLE auth_user (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, email VARCHAR(254) NOT NULL, is_staff INTEGER NOT NULL, is_active INTEGER NOT NULL)",
        "CREATE TABLE core_tenant (id TEXT PRIMARY KEY, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL UNIQUE, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_tenantmembership (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL, UNIQUE (tenant_id, user_id))",
        "CREATE TABLE core_project (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL, UNIQUE (tenant_id, slug))",
        "CREATE TABLE core_projectmembership (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL, UNIQUE (project_id, user_id))",
        "CREATE TABLE core_auditevent (id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, actor_id INTEGER, event_type VARCHAR(120) NOT NULL, resource_type VARCHAR(80) NOT NULL, resource_id VARCHAR(64) NOT NULL, outcome VARCHAR(16) NOT NULL, request_id VARCHAR(64) NOT NULL, remote_addr TEXT, user_agent VARCHAR(256) NOT NULL, metadata TEXT NOT NULL, previous_hash VARCHAR(64) NOT NULL, event_hash VARCHAR(64) NOT NULL UNIQUE, created_at DATETIME NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    for (id, username) in [(1, "alice"), (2, "bob"), (3, "carol")] {
        sqlx::query(
            "INSERT INTO auth_user (id, username, email, is_staff, is_active) VALUES (?, ?, ?, 0, 1)",
        )
        .bind(id)
        .bind(username)
        .bind(format!("{username}@example.com"))
        .execute(&pool)
        .await
        .unwrap();
    }
    sqlx::query(
        "INSERT INTO core_tenant (id, name, slug, created_at) VALUES (?, 'Acme', 'acme', datetime('now'))",
    )
    .bind(TENANT)
    .execute(&pool)
    .await
    .unwrap();
    for (username, role) in [("alice", "owner"), ("bob", "admin"), ("carol", "member")] {
        sqlx::query(
            "INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) \
             VALUES (?, ?, ?, ?, datetime('now'))",
        )
        .bind(uuid::Uuid::new_v4().to_string())
        .bind(TENANT)
        .bind(match username {
            "alice" => 1,
            "bob" => 2,
            _ => 3,
        })
        .bind(role)
        .execute(&pool)
        .await
        .unwrap();
    }
    sqlx::query(
        "INSERT INTO core_project (id, tenant_id, name, slug, created_at) \
         VALUES (?, ?, 'Console', 'console', datetime('now'))",
    )
    .bind(PROJECT)
    .bind(TENANT)
    .execute(&pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO core_projectmembership (id, project_id, user_id, role, created_at) \
         VALUES (?, ?, 2, 'admin', datetime('now'))",
    )
    .bind(uuid::Uuid::new_v4().to_string())
    .bind(PROJECT)
    .execute(&pool)
    .await
    .unwrap();
    for (session, user) in [(OWNER_SESSION, 1), (ADMIN_SESSION, 2), (MEMBER_SESSION, 3)] {
        let data = encode_session_data(user, "password-hash", SECRET).unwrap();
        sqlx::query(
            "INSERT INTO django_session (session_key, session_data, expire_date) \
             VALUES (?, ?, datetime('now', '+1 day'))",
        )
        .bind(session)
        .bind(data)
        .execute(&pool)
        .await
        .unwrap();
    }
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
    (AppState::new(config).unwrap(), path.display().to_string())
}

async fn call(
    state: &AppState,
    method: &str,
    uri: &str,
    session: &str,
    body: Value,
) -> (StatusCode, Value) {
    let request = Request::builder()
        .method(method)
        .uri(uri)
        .header("x-session-token", session)
        .header("content-type", "application/json")
        .body(Body::from(body.to_string()))
        .unwrap();
    let response = router(state.clone()).oneshot(request).await.unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 1024 * 1024)
        .await
        .unwrap();
    let value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, value)
}

#[tokio::test]
async fn lists_tenants_and_creates_one_with_its_owner_and_audit_event() {
    let (state, _path) = state().await;
    let (status, body) = call(&state, "GET", "/api/v1/tenants/", OWNER_SESSION, json!({})).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["count"], 1);
    assert_eq!(body["results"][0]["slug"], "acme");

    let (status, body) = call(
        &state,
        "POST",
        "/api/v1/tenants/",
        OWNER_SESSION,
        json!({"name": "Beta", "slug": "beta"}),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    assert_eq!(body["slug"], "beta");
    let tenant_id = body["id"].as_str().unwrap().to_string();

    let (status, body) = call(&state, "GET", "/api/v1/tenants/", OWNER_SESSION, json!({})).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["count"], 2);

    let pool = sqlx::AnyPool::connect(&format!("sqlite://{}", _path))
        .await
        .unwrap();
    let role: String = sqlx::query_scalar(
        "SELECT role FROM core_tenantmembership WHERE CAST(tenant_id AS TEXT) = ? AND user_id = 1",
    )
    .bind(&tenant_id)
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(role, "owner");
    let audit_tenant: String = sqlx::query_scalar(
        "SELECT tenant_id FROM core_auditevent WHERE event_type = 'tenant.created'",
    )
    .fetch_one(&pool)
    .await
    .unwrap();
    assert_eq!(audit_tenant, tenant_id);
}

#[tokio::test]
async fn creates_projects_and_enforces_membership_permissions() {
    let (state, _path) = state().await;
    let (status, body) = call(
        &state,
        "POST",
        &format!("/api/v1/tenants/{TENANT}/projects/"),
        ADMIN_SESSION,
        json!({"name": "Ops", "slug": "ops"}),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    assert_eq!(body["tenant_id"], TENANT);

    let (status, _) = call(
        &state,
        "POST",
        &format!("/api/v1/tenants/{TENANT}/projects/"),
        MEMBER_SESSION,
        json!({"name": "Nope", "slug": "nope"}),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);

    let (status, body) = call(
        &state,
        "GET",
        &format!("/api/v1/tenants/{TENANT}/members/"),
        MEMBER_SESSION,
        json!({}),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    assert_eq!(body["error"]["code"], "TENANT_ADMIN_REQUIRED");
}

#[tokio::test]
async fn protects_the_last_owner_and_adds_project_members() {
    let (state, _path) = state().await;
    let owner_membership: String = {
        let pool = sqlx::AnyPool::connect(&format!("sqlite://{}", _path))
            .await
            .unwrap();
        sqlx::query_scalar("SELECT CAST(id AS TEXT) FROM core_tenantmembership WHERE user_id = 1")
            .fetch_one(&pool)
            .await
            .unwrap()
    };
    let (status, body) = call(
        &state,
        "DELETE",
        &format!("/api/v1/tenants/{TENANT}/members/{owner_membership}/"),
        OWNER_SESSION,
        json!({}),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["error"]["code"], "LAST_OWNER_PROTECTED");

    let (status, body) = call(
        &state,
        "POST",
        &format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/members/"),
        ADMIN_SESSION,
        json!({"username": "carol", "role": "viewer"}),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    assert_eq!(body["username"], "carol");

    let (status, body) = call(
        &state,
        "GET",
        &format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/members/"),
        ADMIN_SESSION,
        json!({}),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body.as_array().unwrap().len(), 2);

    let (status, body) = call(
        &state,
        "POST",
        &format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/members/"),
        MEMBER_SESSION,
        json!({"username": "alice", "role": "viewer"}),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    assert_eq!(body["error"]["code"], "PROJECT_ADMIN_REQUIRED");
}
