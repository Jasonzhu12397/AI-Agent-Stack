use std::time::Duration;

use ai_agent_stack_api_edge::audit::{record_event, AuditEvent};
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
const MEMBER_SESSION: &str = "member-session";
const STAFF_SESSION: &str = "staff-session";

async fn state() -> (AppState, String) {
    let path = std::env::temp_dir().join(format!("rust-audit-{}.sqlite3", uuid::Uuid::new_v4()));
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
        "CREATE TABLE core_auditevent (id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, actor_id INTEGER, event_type VARCHAR(120) NOT NULL, resource_type VARCHAR(80) NOT NULL, resource_id VARCHAR(64) NOT NULL, outcome VARCHAR(16) NOT NULL, request_id VARCHAR(64) NOT NULL, remote_addr TEXT, user_agent VARCHAR(256) NOT NULL, metadata TEXT NOT NULL, previous_hash VARCHAR(64) NOT NULL, event_hash VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    for (id, username, staff) in [(1, "alice", 0), (2, "carol", 0), (3, "root", 1)] {
        sqlx::query("INSERT INTO auth_user (id, username, email, is_staff, is_active) VALUES (?, ?, ?, ?, 1)")
            .bind(id)
            .bind(username)
            .bind(format!("{username}@example.com"))
            .bind(staff)
            .execute(&pool)
            .await
            .unwrap();
    }
    sqlx::query("INSERT INTO core_tenant (id, name, slug, created_at) VALUES (?, 'Acme', 'acme', datetime('now'))")
        .bind(TENANT)
        .execute(&pool)
        .await
        .unwrap();
    for (user, role) in [(1, "owner"), (2, "member")] {
        sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES (?, ?, ?, ?, datetime('now'))")
            .bind(uuid::Uuid::new_v4().to_string())
            .bind(TENANT)
            .bind(user)
            .bind(role)
            .execute(&pool)
            .await
            .unwrap();
    }
    sqlx::query("INSERT INTO core_project (id, tenant_id, name, slug, created_at) VALUES (?, ?, 'Console', 'console', datetime('now'))")
        .bind(PROJECT)
        .bind(TENANT)
        .execute(&pool)
        .await
        .unwrap();
    for (session, user) in [(OWNER_SESSION, 1), (MEMBER_SESSION, 2), (STAFF_SESSION, 3)] {
        let data = encode_session_data(user, "password-hash", SECRET).unwrap();
        sqlx::query("INSERT INTO django_session (session_key, session_data, expire_date) VALUES (?, ?, datetime('now', '+1 day'))")
            .bind(session)
            .bind(data)
            .execute(&pool)
            .await
            .unwrap();
    }
    for event_type in ["tenant.created", "project.created"] {
        record_event(
            &pool,
            &url,
            AuditEvent {
                tenant_id: Some(TENANT),
                project_id: Some(PROJECT),
                event_type,
                resource_type: "tenant",
                resource_id: TENANT,
                outcome: "success",
                actor_id: Some(1),
                request_id: "req-1",
                remote_addr: None,
                user_agent: "test",
                metadata: json!({"name": "Acme", "nested": {"b": 2, "a": 1}}),
            },
        )
        .await
        .unwrap();
    }
    record_event(
        &pool,
        &url,
        AuditEvent {
            tenant_id: None,
            project_id: None,
            event_type: "auth.login.succeeded",
            resource_type: "session",
            resource_id: "",
            outcome: "success",
            actor_id: Some(3),
            request_id: "",
            remote_addr: None,
            user_agent: "test",
            metadata: json!({"reason": "authenticated"}),
        },
    )
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
    (AppState::new(config).unwrap(), path.display().to_string())
}

async fn get(state: &AppState, uri: &str, session: &str) -> (StatusCode, Value) {
    let response = router(state.clone())
        .oneshot(
            Request::builder()
                .uri(uri)
                .header("cookie", format!("sessionid={session}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), 1024 * 1024)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

#[tokio::test]
async fn lists_and_verifies_the_tenant_hash_chain() {
    let (state, _path) = state().await;
    let uri = format!("/api/v1/tenants/{TENANT}/audit-events/");
    let (status, body) = get(&state, &uri, OWNER_SESSION).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["count"], 2);
    assert_eq!(body["results"][0]["event_type"], "project.created");
    assert_eq!(body["results"][0]["metadata"]["nested"]["a"], 1);

    let (status, body) = get(&state, &format!("{uri}verify/"), OWNER_SESSION).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["valid"], true);
    assert_eq!(body["verified_count"], 2);

    let (status, body) = get(&state, &uri, MEMBER_SESSION).await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    assert!(body["detail"].is_string());
}

#[tokio::test]
async fn detects_tampering_and_protects_the_global_chain() {
    let (state, path) = state().await;
    let pool = sqlx::AnyPool::connect(&format!("sqlite://{path}"))
        .await
        .unwrap();
    sqlx::query("UPDATE core_auditevent SET metadata = ? WHERE event_type = 'project.created'")
        .bind(r#"{"name":"Tampered"}"#)
        .execute(&pool)
        .await
        .unwrap();
    let (status, body) = get(
        &state,
        &format!("/api/v1/tenants/{TENANT}/audit-events/verify/"),
        OWNER_SESSION,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["valid"], false);
    assert!(body["invalid_event_id"].is_string());

    let (status, body) = get(&state, "/api/v1/audit-events/global/", STAFF_SESSION).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["count"], 1);
    let (status, _) = get(&state, "/api/v1/audit-events/global/", OWNER_SESSION).await;
    assert_eq!(status, StatusCode::FORBIDDEN);
    let (status, body) = get(&state, "/api/v1/audit-events/global/verify/", STAFF_SESSION).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["valid"], true);
}
