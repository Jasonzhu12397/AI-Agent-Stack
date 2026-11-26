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
const VIEWER_SESSION: &str = "viewer-session";

async fn state() -> (AppState, String) {
    let path =
        std::env::temp_dir().join(format!("rust-workflows-{}.sqlite3", uuid::Uuid::new_v4()));
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
        "CREATE TABLE core_workflowdefinition (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, description TEXT NOT NULL, draft_definition TEXT NOT NULL, current_version INTEGER NOT NULL, is_archived BOOLEAN NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, UNIQUE (project_id, slug))",
        "CREATE TABLE core_workflowversion (id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, version INTEGER NOT NULL, definition_snapshot TEXT NOT NULL, created_by_id INTEGER NOT NULL, published_at DATETIME NOT NULL, UNIQUE (workflow_id, version))",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    for (id, username) in [(1, "alice"), (2, "carol")] {
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
    sqlx::query("INSERT INTO core_tenant (id, name, slug, created_at) VALUES (?, 'Acme', 'acme', datetime('now'))")
        .bind(TENANT)
        .execute(&pool)
        .await
        .unwrap();
    for (user, role) in [(1, "owner"), (2, "viewer")] {
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
    for (user, role) in [(1, "admin"), (2, "viewer")] {
        sqlx::query("INSERT INTO core_projectmembership (id, project_id, user_id, role, created_at) VALUES (?, ?, ?, ?, datetime('now'))")
            .bind(uuid::Uuid::new_v4().to_string())
            .bind(PROJECT)
            .bind(user)
            .bind(role)
            .execute(&pool)
            .await
            .unwrap();
    }
    for (session, user) in [(OWNER_SESSION, 1), (VIEWER_SESSION, 2)] {
        let data = encode_session_data(user, "password-hash", SECRET).unwrap();
        sqlx::query("INSERT INTO django_session (session_key, session_data, expire_date) VALUES (?, ?, datetime('now', '+1 day'))")
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
        .header("cookie", format!("sessionid={session}"))
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
async fn creates_and_reads_workflow_drafts() {
    let (state, _path) = state().await;
    let collection = format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/workflows/");
    let draft = json!({"nodes": [{"id": "start", "type": "input"}], "edges": []});
    let (status, body) = call(
        &state,
        "POST",
        &collection,
        OWNER_SESSION,
        json!({"name": "Onboarding", "slug": "onboarding", "description": "Draft", "draft_definition": draft}),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    assert_eq!(body["name"], "Onboarding");
    assert_eq!(body["slug"], "onboarding");
    assert_eq!(body["draft_definition"]["nodes"][0]["id"], "start");
    assert_eq!(body["current_version"], 0);
    assert_eq!(body["is_archived"], false);
    assert!(body["created_at"].as_str().is_some());
    let workflow_id = body["id"].as_str().unwrap().to_string();

    let (status, list) = call(&state, "GET", &collection, OWNER_SESSION, json!({})).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(list.as_array().unwrap().len(), 1);
    assert_eq!(list[0]["id"], workflow_id);
    assert!(list[0]["created_at"].as_str().is_some());

    let detail = format!("/api/v1/workflows/{workflow_id}/");
    let (status, body) = call(&state, "GET", &detail, VIEWER_SESSION, json!({})).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["id"], workflow_id);
}

#[tokio::test]
async fn enforces_slug_uniqueness_editor_rights_and_archive_lock() {
    let (state, _path) = state().await;
    let collection = format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/workflows/");
    let (status, _) = call(
        &state,
        "POST",
        &collection,
        OWNER_SESSION,
        json!({"name": "Flow", "slug": "flow", "draft_definition": {"nodes": [], "edges": []}}),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);

    let (status, _) = call(
        &state,
        "POST",
        &collection,
        OWNER_SESSION,
        json!({"name": "Other", "slug": "flow"}),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);

    let (status, _) = call(
        &state,
        "POST",
        &collection,
        VIEWER_SESSION,
        json!({"name": "Viewer Flow", "slug": "viewer-flow"}),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn updates_draft_and_rejects_archived_mutation() {
    let (state, path) = state().await;
    let collection = format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/workflows/");
    let (_, body) = call(
        &state,
        "POST",
        &collection,
        OWNER_SESSION,
        json!({"name": "Flow", "slug": "flow", "draft_definition": {"nodes": [], "edges": []}}),
    )
    .await;
    let workflow_id = body["id"].as_str().unwrap().to_string();
    let detail = format!("/api/v1/workflows/{workflow_id}/");

    let (status, body) = call(
        &state,
        "PATCH",
        &detail,
        OWNER_SESSION,
        json!({"name": "Renamed", "draft_definition": {"nodes": [{"id": "a"}], "edges": [{"source": "a", "target": "b"}]}}),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["name"], "Renamed");
    assert_eq!(body["draft_definition"]["edges"][0]["target"], "b");

    let pool = sqlx::AnyPool::connect(&format!("sqlite://{path}"))
        .await
        .unwrap();
    sqlx::query("UPDATE core_workflowdefinition SET is_archived = 1 WHERE CAST(id AS TEXT) = ?")
        .bind(&workflow_id)
        .execute(&pool)
        .await
        .unwrap();
    pool.close().await;

    let (status, _) = call(
        &state,
        "PATCH",
        &detail,
        OWNER_SESSION,
        json!({"name": "Should Fail"}),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);

    let (status, list) = call(&state, "GET", &collection, OWNER_SESSION, json!({})).await;
    assert_eq!(status, StatusCode::OK);
    assert!(list.as_array().unwrap().is_empty());
}

#[tokio::test]
async fn validates_and_publishes_a_workflow() {
    let (state, _path) = state().await;
    let collection = format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/workflows/");
    let definition = json!({
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "end", "type": "end", "config": {"source_node": "start"}}
        ],
        "edges": [{"source": "start", "target": "end"}],
        "ui": {"node_positions": {"start": {"x": 10, "y": 20}, "end": {"x": 300, "y": 20}}}
    });
    let (status, body) = call(
        &state,
        "POST",
        &collection,
        OWNER_SESSION,
        json!({"name": "Publishable", "slug": "publishable", "draft_definition": definition}),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    let workflow_id = body["id"].as_str().unwrap().to_string();

    let validate_uri = format!("/api/v1/workflows/{workflow_id}/validate/");
    let (status, body) = call(&state, "POST", &validate_uri, OWNER_SESSION, json!({})).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["is_valid"], true);
    assert_eq!(body["node_count"], 2);
    assert_eq!(body["edge_count"], 1);
    assert_eq!(body["execution_layers"][0][0], "start");
    assert_eq!(body["execution_layers"][1][0], "end");

    let publish_uri = format!("/api/v1/workflows/{workflow_id}/publish/");
    let (status, body) = call(&state, "POST", &publish_uri, OWNER_SESSION, json!({})).await;
    assert_eq!(status, StatusCode::CREATED);
    assert_eq!(body["version"], 1);
    assert_eq!(body["workflow_id"], workflow_id);
    assert_eq!(body["definition_snapshot"]["edges"][0]["target"], "end");

    let (status, body) = call(&state, "POST", &publish_uri, OWNER_SESSION, json!({})).await;
    assert_eq!(status, StatusCode::CREATED);
    assert_eq!(body["version"], 2);
}

#[tokio::test]
async fn rejects_invalid_graphs_and_non_editors() {
    let (state, _path) = state().await;
    let collection = format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/workflows/");
    let (status, body) = call(
        &state,
        "POST",
        &collection,
        OWNER_SESSION,
        json!({"name": "Broken", "slug": "broken", "draft_definition": {
            "nodes": [{"id": "start", "type": "start", "config": {}}],
            "edges": []
        }}),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    let workflow_id = body["id"].as_str().unwrap().to_string();

    let validate_uri = format!("/api/v1/workflows/{workflow_id}/validate/");
    let (status, body) = call(&state, "POST", &validate_uri, OWNER_SESSION, json!({})).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["is_valid"], false);
    assert!(body["errors"][0].as_str().unwrap().contains("2 and 50"));

    let publish_uri = format!("/api/v1/workflows/{workflow_id}/publish/");
    let (status, _) = call(&state, "POST", &publish_uri, OWNER_SESSION, json!({})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);

    let (status, _) = call(&state, "POST", &publish_uri, VIEWER_SESSION, json!({})).await;
    assert_eq!(status, StatusCode::FORBIDDEN);
}
