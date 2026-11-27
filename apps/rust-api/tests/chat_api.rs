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
const SESSION: &str = "owner-session";

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-chat-{}.sqlite3", uuid::Uuid::new_v4()));
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
        "CREATE TABLE core_modelprovider (id TEXT PRIMARY KEY, tenant_id TEXT, created_by_id INTEGER, name VARCHAR(120) NOT NULL, provider_type VARCHAR(32) NOT NULL, auth_mode VARCHAR(32) NOT NULL, preset_key VARCHAR(40) NOT NULL, base_url VARCHAR(500) NOT NULL, credential_ref VARCHAR(200) NOT NULL, config TEXT NOT NULL, enabled BOOLEAN NOT NULL, deployment_region VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_modelconfig (id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, display_name VARCHAR(160) NOT NULL, model_name VARCHAR(160) NOT NULL, capabilities TEXT NOT NULL, context_window INTEGER NOT NULL, max_output_tokens INTEGER NOT NULL, enabled BOOLEAN NOT NULL, version INTEGER NOT NULL)",
        "CREATE TABLE core_agentchatthread (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, title VARCHAR(200) NOT NULL, model_config_id TEXT, archived BOOLEAN NOT NULL, execution_target VARCHAR(16) NOT NULL, effort VARCHAR(16) NOT NULL, goal VARCHAR(500) NOT NULL, plan_first BOOLEAN NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentchatmessage (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, role VARCHAR(16) NOT NULL, content TEXT NOT NULL, author_id INTEGER, context TEXT NOT NULL, usage TEXT NOT NULL, tool_calls TEXT NOT NULL, error_code VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentchatattachment (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, uploaded_by_id INTEGER NOT NULL, filename VARCHAR(255) NOT NULL, content_type VARCHAR(128) NOT NULL, size_bytes INTEGER NOT NULL, file VARCHAR(100) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentplugindefinition (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, plugin_key VARCHAR(100) NOT NULL, name VARCHAR(120) NOT NULL, description TEXT NOT NULL, version VARCHAR(40) NOT NULL, protocol VARCHAR(32) NOT NULL, endpoint_url VARCHAR(500) NOT NULL, auth_required BOOLEAN NOT NULL, credential_ref VARCHAR(2048) NOT NULL, capabilities TEXT NOT NULL, manifest TEXT NOT NULL, input_schema TEXT NOT NULL, output_schema TEXT NOT NULL, timeout_seconds INTEGER NOT NULL, max_concurrency INTEGER NOT NULL, enabled BOOLEAN NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_scheduledchatjob (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, created_by_id INTEGER NOT NULL, name VARCHAR(160) NOT NULL, prompt TEXT NOT NULL, cadence VARCHAR(16) NOT NULL, hour INTEGER NOT NULL, weekday INTEGER NOT NULL, execution_target VARCHAR(16) NOT NULL, effort VARCHAR(16) NOT NULL, status VARCHAR(16) NOT NULL, next_run_at DATETIME NOT NULL, last_run_at DATETIME, last_error_code VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, model_config_id TEXT, thread_id TEXT)",
        "CREATE TABLE core_auditevent (id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, actor_id INTEGER, event_type VARCHAR(120) NOT NULL, resource_type VARCHAR(80) NOT NULL, resource_id VARCHAR(64) NOT NULL, outcome VARCHAR(16) NOT NULL, request_id VARCHAR(64) NOT NULL, remote_addr TEXT, user_agent VARCHAR(256) NOT NULL, metadata TEXT NOT NULL, previous_hash VARCHAR(64) NOT NULL, event_hash VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_agentplugininstallation (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, plugin_id TEXT NOT NULL, plugin_version VARCHAR(40) NOT NULL, status VARCHAR(16) NOT NULL, granted_capabilities TEXT NOT NULL, credential_ref VARCHAR(200) NOT NULL, installed_by_id INTEGER NOT NULL, revision INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
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
    sqlx::query("INSERT INTO core_modelprovider (id, tenant_id, created_by_id, name, provider_type, auth_mode, preset_key, base_url, credential_ref, config, enabled, deployment_region, created_at, updated_at) VALUES ('provider-1', ?, 1, 'Local echo', 'fake', 'platform_pool', '', '', '', '{\"response\": \"Echo reply\"}', 1, '', datetime('now'), datetime('now'))")
        .bind(TENANT).execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_modelconfig (id, provider_id, display_name, model_name, capabilities, context_window, max_output_tokens, enabled, version) VALUES ('bbbbbbbb-1111-1111-1111-111111111111', 'provider-1', 'Echo', 'demo-echo', '[\"chat\"]', 8192, 2048, 1, 1)")
        .execute(&pool).await.unwrap();
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

async fn call(
    state: &AppState,
    method: &str,
    uri: &str,
    body: Option<Value>,
) -> (StatusCode, Value) {
    let request = Request::builder()
        .method(method)
        .uri(uri)
        .header(
            "cookie",
            format!("sessionid={SESSION}; csrftoken=0123456789abcdef0123456789abcdef"),
        )
        .header(
            "x-csrftoken",
            format!("{}{}", "a".repeat(32), "0123456789abcdef0123456789abcdef"),
        );
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
async fn manages_chat_threads_in_real_storage() {
    let state = state().await;
    let collection = format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/agent-chat/threads/");
    let (status, body) = call(
        &state,
        "POST",
        &collection,
        Some(json!({"title": "  Fleet review  ", "execution_target": "local", "goal": "Check nodes"})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED, "{body}");
    assert_eq!(body["title"], "Fleet review");
    assert_eq!(body["execution_target"], "local");
    assert_eq!(
        body["model_config_id"],
        "bbbbbbbb-1111-1111-1111-111111111111"
    );
    let thread = body["id"].as_str().unwrap().to_string();

    let (status, body) = call(&state, "GET", &collection, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["items"].as_array().unwrap().len(), 1);
    assert_eq!(body["items"][0]["id"], thread);

    let detail = format!("/api/v1/agent-chat/threads/{thread}/");
    let (status, body) = call(&state, "GET", &detail, None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["messages"].as_array().unwrap().len(), 0);
    assert_eq!(body["attachments"].as_array().unwrap().len(), 0);

    let (status, body) = call(
        &state,
        "PATCH",
        &detail,
        Some(json!({"title": "Renamed", "archived": true})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["title"], "Renamed");
    assert_eq!(body["archived"], true);

    let (status, _) = call(&state, "DELETE", &detail, None).await;
    assert_eq!(status, StatusCode::NO_CONTENT);

    let context = format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/agent-chat/context/");
    let (status, body) = call(&state, "GET", &context, None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["project"]["id"], PROJECT);
    assert_eq!(body["models"][0]["model_name"], "demo-echo");
}

#[tokio::test]
async fn lists_thread_messages_with_usage_and_context() {
    let state = state().await;
    let (status, thread) = call(
        &state,
        "POST",
        &format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/agent-chat/threads/"),
        Some(json!({"title": "History"})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    let thread_id = thread["id"].as_str().unwrap().to_string();
    let pool = state.identity_pool.as_ref().unwrap();
    for (id, role, content, usage) in [
        ("m1", "user", "hello", "{}"),
        (
            "m2",
            "assistant",
            "hi",
            "{\"input_tokens\": 2, \"output_tokens\": 3}",
        ),
    ] {
        sqlx::query(
            "INSERT INTO core_agentchatmessage (id, thread_id, role, content, author_id, context, \
             usage, tool_calls, error_code, created_at) VALUES (?, ?, ?, ?, 1, '{}', ?, '[]', '', \
             datetime('now'))",
        )
        .bind(id)
        .bind(&thread_id)
        .bind(role)
        .bind(content)
        .bind(usage)
        .execute(pool)
        .await
        .unwrap();
    }
    let (status, body) = call(
        &state,
        "GET",
        &format!("/api/v1/agent-chat/threads/{thread_id}/messages/"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let items = body["items"].as_array().unwrap();
    assert_eq!(items.len(), 2);
    assert_eq!(items[1]["role"], "assistant");
    assert_eq!(items[1]["usage"]["output_tokens"], 3);
    assert_eq!(items[1]["author"], "alice");
}

#[tokio::test]
async fn creates_user_and_assistant_messages_in_real_storage() {
    let state = state().await;
    let (status, thread) = call(
        &state,
        "POST",
        &format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/agent-chat/threads/"),
        Some(json!({"title": "New conversation"})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    let thread_id = thread["id"].as_str().unwrap().to_string();
    let uri = format!("/api/v1/agent-chat/threads/{thread_id}/messages/");

    let (status, body) = call(
        &state,
        "POST",
        &uri,
        Some(json!({"content": "  first line\nsecond line  ", "context": {"skills": ["s1"]}})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED, "{body}");
    assert_eq!(body["user"]["role"], "user");
    assert_eq!(body["user"]["content"], "first line\nsecond line");
    assert_eq!(body["user"]["author"], "alice");
    assert_eq!(body["assistant"]["content"], "Echo reply");
    assert_eq!(body["assistant"]["usage"]["provider"], "Local echo");
    assert_eq!(body["assistant"]["usage"]["model"], "demo-echo");

    let (status, body) = call(&state, "GET", &uri, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["items"].as_array().unwrap().len(), 2);
    assert_eq!(body["items"][0]["context"]["skills"][0], "s1");

    let pool = state.identity_pool.as_ref().unwrap();
    let title = sqlx::query_scalar::<_, String>(
        "SELECT title FROM core_agentchatthread WHERE CAST(id AS TEXT) = ?",
    )
    .bind(&thread_id)
    .fetch_one(pool)
    .await
    .unwrap();
    assert_eq!(title, "first line");

    let (status, body) = call(&state, "POST", &uri, Some(json!({"content": "   "}))).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], "INVALID_MESSAGE");

    let (status, _) = call(
        &state,
        "DELETE",
        &format!("/api/v1/agent-chat/threads/{thread_id}/"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::NO_CONTENT);
    let remaining = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_agentchatmessage WHERE CAST(thread_id AS TEXT) = ?",
    )
    .bind(&thread_id)
    .fetch_one(pool)
    .await
    .unwrap();
    assert_eq!(remaining, 0);
}

#[tokio::test]
async fn tests_model_config_against_the_provider() {
    let state = state().await;
    let uri = format!(
        "/api/v1/tenants/{TENANT}/model-configs/bbbbbbbb-1111-1111-1111-111111111111/test/"
    );
    let (status, body) = call(
        &state,
        "POST",
        &uri,
        Some(json!({"messages": [{"role": "user", "content": "ping"}]})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["content"], "Echo reply");
    assert_eq!(body["provider"], "Local echo");
    assert_eq!(body["model"], "demo-echo");

    let (status, body) = call(&state, "POST", &uri, Some(json!({"messages": []}))).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], "INVALID_MESSAGES");

    let missing = format!("/api/v1/tenants/{TENANT}/model-configs/missing/test/");
    let (status, _) = call(&state, "POST", &missing, Some(json!({}))).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn stores_chat_attachments_in_real_storage() {
    let mut state = state().await;
    let media = std::env::temp_dir().join(format!("rust-media-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&media).unwrap();
    state.config.media_root = media.to_string_lossy().to_string();
    let (status, body) = call(
        &state,
        "POST",
        &format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/agent-chat/threads/"),
        Some(json!({"title": "Files"})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    let thread_id = body["id"].as_str().unwrap().to_string();
    let collection = format!("/api/v1/agent-chat/threads/{thread_id}/attachments/");

    let boundary = "X-BOUNDARY";
    let multipart = format!(
        "--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"notes.txt\"\r\nContent-Type: text/plain\r\n\r\nhello attach\r\n--{boundary}--\r\n"
    );
    let request = Request::builder()
        .method("POST")
        .uri(&collection)
        .header(
            "cookie",
            format!("sessionid={SESSION}; csrftoken=0123456789abcdef0123456789abcdef"),
        )
        .header(
            "x-csrftoken",
            format!("{}{}", "a".repeat(32), "0123456789abcdef0123456789abcdef"),
        )
        .header(
            "content-type",
            format!("multipart/form-data; boundary={boundary}"),
        )
        .body(Body::from(multipart))
        .unwrap();
    let response = router(state.clone()).oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::CREATED);
    let bytes = axum::body::to_bytes(response.into_body(), 4 * 1024 * 1024)
        .await
        .unwrap();
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(body["filename"], "notes.txt");
    assert_eq!(body["content_type"], "text/plain");
    assert_eq!(body["size_bytes"], 12);
    let attachment_id = body["id"].as_str().unwrap().to_string();

    let (status, body) = call(&state, "GET", &collection, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["items"].as_array().unwrap().len(), 1);

    let request = Request::builder()
        .method("GET")
        .uri(format!("/api/v1/agent-chat/attachments/{attachment_id}/"))
        .header("cookie", format!("sessionid={SESSION}"))
        .body(Body::empty())
        .unwrap();
    let response = router(state.clone()).oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(response.into_body(), 4 * 1024 * 1024)
        .await
        .unwrap();
    assert_eq!(&bytes[..], b"hello attach");

    let (status, _) = call(
        &state,
        "DELETE",
        &format!("/api/v1/agent-chat/attachments/{attachment_id}/"),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::NO_CONTENT);
    let (status, _) = call(&state, "GET", &collection, None).await;
    assert_eq!(status, StatusCode::OK);
}

#[tokio::test]
async fn manages_scheduled_chat_jobs() {
    let state = state().await;
    let base = format!("/api/v1/tenants/{TENANT}/projects/{PROJECT}/scheduled-jobs/");
    let (status, body) = call(
        &state,
        "POST",
        &base,
        Some(json!({
            "name": "Morning fleet check",
            "prompt": "Check nodes",
            "cadence": "weekly",
            "hour": 8,
            "weekday": 2,
            "model_config_id": "bbbbbbbb-1111-1111-1111-111111111111"
        })),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED, "{body}");
    assert_eq!(body["name"], "Morning fleet check");
    assert_eq!(body["status"], "active");
    assert_eq!(body["enabled"], true);
    assert_eq!(body["weekday"], 2);
    assert_eq!(
        body["model_config_id"],
        "bbbbbbbb-1111-1111-1111-111111111111"
    );
    let id = body["id"].as_str().unwrap().to_string();

    let (status, body) = call(&state, "GET", &base, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["items"].as_array().unwrap().len(), 1);

    let item = format!("{base}{id}/");
    let (status, body) = call(
        &state,
        "PATCH",
        &item,
        Some(json!({"enabled": false, "hour": 9})),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["status"], "paused");
    assert_eq!(body["enabled"], false);
    assert_eq!(body["hour"], 9);

    let (status, body) = call(
        &state,
        "POST",
        &base,
        Some(json!({"name": "", "prompt": "x"})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "INVALID_SCHEDULED_JOB");

    let (status, _) = call(&state, "DELETE", &item, None).await;
    assert_eq!(status, StatusCode::NO_CONTENT);
    let (status, body) = call(&state, "GET", &base, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["items"].as_array().unwrap().len(), 0);
}
