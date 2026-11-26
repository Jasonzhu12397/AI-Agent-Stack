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
const PROVIDER: &str = "aaaaaaaa-1111-1111-1111-111111111111";
const EMBEDDING: &str = "bbbbbbbb-1111-1111-1111-111111111111";
const CHAT: &str = "cccccccc-1111-1111-1111-111111111111";
const SESSION: &str = "owner-session";

async fn state() -> AppState {
    let path =
        std::env::temp_dir().join(format!("rust-knowledge-{}.sqlite3", uuid::Uuid::new_v4()));
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
        "CREATE TABLE core_task (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, task_type VARCHAR(120) NOT NULL, payload TEXT NOT NULL, result TEXT, error TEXT, status VARCHAR(24) NOT NULL, idempotency_key VARCHAR(128) NOT NULL, attempts INTEGER NOT NULL, max_attempts INTEGER NOT NULL, available_at DATETIME NOT NULL, lease_owner VARCHAR(120), lease_token TEXT, lease_expires_at DATETIME, heartbeat_at DATETIME, cancel_requested_at DATETIME, started_at DATETIME, finished_at DATETIME, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, UNIQUE (tenant_id, idempotency_key))",
        "CREATE TABLE core_modelprovider (id TEXT PRIMARY KEY, tenant_id TEXT, created_by_id INTEGER NOT NULL, name VARCHAR(120) NOT NULL, provider_type VARCHAR(32) NOT NULL, auth_mode VARCHAR(32) NOT NULL, preset_key VARCHAR(40) NOT NULL, base_url VARCHAR(500) NOT NULL, credential_ref VARCHAR(200) NOT NULL, config TEXT NOT NULL, enabled BOOLEAN NOT NULL, deployment_region VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_modelconfig (id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, model_name VARCHAR(160) NOT NULL, display_name VARCHAR(160) NOT NULL, capabilities TEXT NOT NULL, context_window INTEGER NOT NULL, max_output_tokens INTEGER NOT NULL, input_cost_per_million REAL NOT NULL, output_cost_per_million REAL NOT NULL, version INTEGER NOT NULL, enabled BOOLEAN NOT NULL, fallback_model_id TEXT, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_knowledgebase (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, embedding_model_id TEXT NOT NULL, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, description TEXT NOT NULL, chunk_size INTEGER NOT NULL, chunk_overlap INTEGER NOT NULL, is_archived BOOLEAN NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, UNIQUE (project_id, slug))",
        "CREATE TABLE core_knowledgedocument (id TEXT PRIMARY KEY, knowledge_base_id TEXT NOT NULL, uploaded_by_id INTEGER NOT NULL, title VARCHAR(240) NOT NULL, content TEXT NOT NULL, metadata TEXT NOT NULL, checksum VARCHAR(64) NOT NULL, status VARCHAR(16) NOT NULL, chunk_count INTEGER NOT NULL, vector_collection VARCHAR(200), vector_dimensions INTEGER, error TEXT, ingested_at DATETIME, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
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
    sqlx::query("INSERT INTO core_projectmembership (id, project_id, user_id, role, created_at) VALUES (?, ?, 1, 'admin', datetime('now'))")
        .bind(uuid::Uuid::new_v4().to_string())
        .bind(PROJECT)
        .execute(&pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO core_modelprovider (id, tenant_id, created_by_id, name, provider_type, auth_mode, preset_key, base_url, credential_ref, config, enabled, deployment_region, created_at, updated_at) VALUES (?, ?, 1, 'Fake', 'fake', 'tenant_secret', '', '', '', '{}', 1, '', datetime('now'), datetime('now'))")
        .bind(PROVIDER)
        .bind(TENANT)
        .execute(&pool)
        .await
        .unwrap();
    for (id, name, capabilities) in [
        (EMBEDDING, "embed", r#"["embeddings"]"#),
        (CHAT, "chat", r#"["chat"]"#),
    ] {
        sqlx::query("INSERT INTO core_modelconfig (id, provider_id, model_name, display_name, capabilities, context_window, max_output_tokens, input_cost_per_million, output_cost_per_million, version, enabled, created_at) VALUES (?, ?, ?, ?, ?, 32768, 4096, 0, 0, 1, 1, datetime('now'))")
            .bind(id)
            .bind(PROVIDER)
            .bind(name)
            .bind(name)
            .bind(capabilities)
            .execute(&pool)
            .await
            .unwrap();
    }
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
async fn creates_and_lists_knowledge_bases_with_document_counts() {
    let state = state().await;
    let collection = format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/knowledge-bases/");
    let (status, body) = call(
        &state,
        "POST",
        &collection,
        Some(json!({"name": "Docs", "slug": "docs", "description": "Runbooks", "embedding_model_id": EMBEDDING})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    assert_eq!(body["name"], "Docs");
    assert_eq!(body["document_count"], 0);
    assert_eq!(body["chunk_size"], 1200);
    assert_eq!(body["chunk_overlap"], 160);
    assert!(body["created_at"].as_str().is_some());
    let kb = body["id"].as_str().unwrap().to_string();

    let (status, body) = call(
        &state,
        "POST",
        &collection,
        Some(json!({"name": "Duplicate", "slug": "docs", "embedding_model_id": EMBEDDING})),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["error"]["code"], "KNOWLEDGE_BASE_SLUG_TAKEN");

    let (status, body) = call(
        &state,
        "POST",
        &collection,
        Some(json!({"name": "Chatty", "slug": "chatty", "embedding_model_id": CHAT})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert!(body["error"]["message"]
        .as_str()
        .unwrap()
        .contains("embeddings"));

    let (status, _body) = call(
        &state,
        "POST",
        &collection,
        Some(json!({"name": "Bad Chunk", "slug": "bad-chunk", "embedding_model_id": EMBEDDING, "chunk_size": 100, "chunk_overlap": 50})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);

    let (status, body) = call(&state, "GET", &collection, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["count"], 1);
    assert_eq!(body["results"][0]["id"], kb);
    assert_eq!(body["results"][0]["document_count"], 0);
}

#[tokio::test]
async fn ingests_documents_and_queues_the_ingestion_task() {
    let state = state().await;
    let collection = format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/knowledge-bases/");
    let (_, body) = call(
        &state,
        "POST",
        &collection,
        Some(json!({"name": "Docs", "slug": "docs", "embedding_model_id": EMBEDDING})),
    )
    .await;
    let kb = body["id"].as_str().unwrap().to_string();
    let documents = format!("/api/v1/knowledge-bases/{kb}/documents/");

    let (status, body) = call(
        &state,
        "POST",
        &documents,
        Some(json!({"title": "Runbook", "content": "Restart the edge.", "metadata": {"team": "platform"}})),
    )
    .await;
    assert_eq!(status, StatusCode::ACCEPTED);
    assert_eq!(body["status"], "pending");
    assert_eq!(body["chunk_count"], 0);
    assert_eq!(body["metadata"]["team"], "platform");
    assert_eq!(body["checksum"].as_str().unwrap().len(), 64);
    assert!(body["error"].is_null());

    let (status, body) = call(
        &state,
        "POST",
        &documents,
        Some(json!({"title": "Leaked", "content": "secret", "metadata": {"api_key": "nope"}})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert!(body["error"]["message"]
        .as_str()
        .unwrap()
        .contains("credential"));

    let (status, _body) = call(
        &state,
        "POST",
        &documents,
        Some(json!({"title": "Empty", "content": "   "})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);

    let (status, body) = call(&state, "GET", &documents, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["count"], 1);
    assert_eq!(body["results"][0]["title"], "Runbook");

    let (status, body) = call(&state, "GET", &collection, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["results"][0]["document_count"], 1);
}
