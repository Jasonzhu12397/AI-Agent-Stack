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
const PROVIDER: &str = "aaaaaaaa-1111-1111-1111-111111111111";
const MODEL_A: &str = "bbbbbbbb-1111-1111-1111-111111111111";
const MODEL_B: &str = "cccccccc-1111-1111-1111-111111111111";
const SESSION: &str = "owner-session";

async fn state() -> AppState {
    let path =
        std::env::temp_dir().join(format!("rust-model-api-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE django_session (session_key VARCHAR(40) PRIMARY KEY, session_data TEXT NOT NULL, expire_date DATETIME NOT NULL)",
        "CREATE TABLE auth_user (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, email VARCHAR(254) NOT NULL, is_staff INTEGER NOT NULL, is_active INTEGER NOT NULL)",
        "CREATE TABLE core_tenant (id TEXT PRIMARY KEY, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_tenantmembership (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_auditevent (id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, actor_id INTEGER, event_type VARCHAR(120) NOT NULL, resource_type VARCHAR(80) NOT NULL, resource_id VARCHAR(64) NOT NULL, outcome VARCHAR(16) NOT NULL, request_id VARCHAR(64) NOT NULL, remote_addr TEXT, user_agent VARCHAR(256) NOT NULL, metadata TEXT NOT NULL, previous_hash VARCHAR(64) NOT NULL, event_hash VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_modelprovider (id TEXT PRIMARY KEY, tenant_id TEXT, created_by_id INTEGER NOT NULL, name VARCHAR(120) NOT NULL, provider_type VARCHAR(32) NOT NULL, auth_mode VARCHAR(32) NOT NULL, preset_key VARCHAR(40) NOT NULL, base_url VARCHAR(500) NOT NULL, credential_ref VARCHAR(200) NOT NULL, config TEXT NOT NULL, enabled BOOLEAN NOT NULL, deployment_region VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_modelconfig (id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, model_name VARCHAR(160) NOT NULL, display_name VARCHAR(160) NOT NULL, capabilities TEXT NOT NULL, context_window INTEGER NOT NULL, max_output_tokens INTEGER NOT NULL, input_cost_per_million REAL NOT NULL, output_cost_per_million REAL NOT NULL, version INTEGER NOT NULL, enabled BOOLEAN NOT NULL, fallback_model_id TEXT, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_provideraccountlink (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, provider_id TEXT NOT NULL, status VARCHAR(16) NOT NULL, account_label VARCHAR(160) NOT NULL, credential_ref VARCHAR(200) NOT NULL, linked_at DATETIME NOT NULL, revoked_at DATETIME, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_provideraccountusage (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, provider_id TEXT NOT NULL, idempotency_key VARCHAR(120) NOT NULL, status VARCHAR(16) NOT NULL, reserved_tokens INTEGER NOT NULL, consumed_tokens INTEGER NOT NULL, expires_at DATETIME, settled_at DATETIME, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
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
    sqlx::query("INSERT INTO core_modelprovider (id, tenant_id, created_by_id, name, provider_type, auth_mode, preset_key, base_url, credential_ref, config, enabled, deployment_region, created_at, updated_at) VALUES (?, ?, 1, 'Fake', 'fake', 'tenant_secret', '', '', '', '{}', 1, '', datetime('now'), datetime('now'))")
        .bind(PROVIDER)
        .bind(TENANT)
        .execute(&pool)
        .await
        .unwrap();
    for (id, name) in [(MODEL_A, "fake-chat"), (MODEL_B, "fake-standby")] {
        sqlx::query("INSERT INTO core_modelconfig (id, provider_id, model_name, display_name, capabilities, context_window, max_output_tokens, input_cost_per_million, output_cost_per_million, version, enabled, created_at) VALUES (?, ?, ?, ?, '[\"chat\"]', 32768, 4096, 0, 0, 1, 1, datetime('now'))")
            .bind(id)
            .bind(PROVIDER)
            .bind(name)
            .bind(name)
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
    key: Option<&str>,
    body: Option<Value>,
) -> (StatusCode, Value) {
    let mut request = Request::builder().method(method).uri(uri);
    if let Some(key) = key {
        request = request.header("x-session-token", key);
    }
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
    let value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, value)
}

#[tokio::test]
async fn lists_visible_providers_with_model_metadata() {
    let state = state().await;
    let (status, body) = call(
        &state,
        "GET",
        &format!("/api/v1/tenants/{TENANT}/model-providers/"),
        Some(SESSION),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let providers = body.as_array().unwrap();
    assert_eq!(providers.len(), 1);
    assert_eq!(providers[0]["name"], "Fake");
    assert_eq!(providers[0]["credential_configured"], false);
    assert_eq!(providers[0]["account"], Value::Null);

    let (status, body) = call(
        &state,
        "GET",
        &format!("/api/v1/tenants/{TENANT}/model-providers/{PROVIDER}/models/"),
        Some(SESSION),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let models = body.as_array().unwrap();
    assert_eq!(models.len(), 2);
    assert_eq!(models[0]["fallback_model"], Value::Null);
    assert_eq!(models[0]["version"], 1);
}

#[tokio::test]
async fn catalog_exposes_platform_presets() {
    let state = state().await;
    let (status, body) = call(
        &state,
        "GET",
        &format!("/api/v1/tenants/{TENANT}/model-providers/catalog/"),
        Some(SESSION),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let platforms = body.as_array().unwrap();
    assert!(!platforms.is_empty());
    let openai = platforms
        .iter()
        .find(|item| item["key"] == "openai")
        .unwrap();
    assert_eq!(openai["label"], "OpenAI");
    assert_eq!(openai["base_url"], "https://api.openai.com/v1");
    assert!(openai["models"].as_array().unwrap().len() > 1);
}

#[tokio::test]
async fn creates_account_provider_from_preset_and_reports_allowance() {
    let state = state().await;
    let (status, body) = call(
        &state,
        "POST",
        &format!("/api/v1/tenants/{TENANT}/model-providers/"),
        Some(SESSION),
        Some(json!({"name": "Doubao", "provider_type": "openai_compatible", "auth_mode": "user_account", "preset_key": "doubao"})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    assert_eq!(body["provider_type"], "openai_compatible");
    assert_eq!(body["account"]["linked"], false);
    assert_eq!(body["account"]["free_daily_tokens"], 50000);
    assert_eq!(body["account"]["used_today"], 0);
    assert_eq!(body["account"]["remaining_today"], 50000);
    assert_eq!(body["account"]["exhausted"], false);

    let (status, body) = call(
        &state,
        "GET",
        &format!(
            "/api/v1/tenants/{TENANT}/model-providers/{}/models/",
            body["id"].as_str().unwrap()
        ),
        Some(SESSION),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert!(!body.as_array().unwrap().is_empty());
}

#[tokio::test]
async fn rejects_duplicate_names_and_remote_urls_outside_allowlist() {
    let state = state().await;
    let (status, body) = call(
        &state,
        "POST",
        &format!("/api/v1/tenants/{TENANT}/model-providers/"),
        Some(SESSION),
        Some(json!({"name": "Fake", "provider_type": "fake"})),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["error"]["code"], "MODEL_PROVIDER_NAME_TAKEN");

    let (status, body) = call(
        &state,
        "POST",
        &format!("/api/v1/tenants/{TENANT}/model-providers/"),
        Some(SESSION),
        Some(json!({"name": "Remote", "provider_type": "openai_compatible", "base_url": "https://unlisted.example.com/v1", "credential_ref": "env:REMOTE_KEY"})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert!(body["error"]["details"]["base_url"].is_array());
}

#[tokio::test]
async fn creates_and_rewires_fallback_chain_without_cycles() {
    let state = state().await;
    let (status, body) = call(
        &state,
        "POST",
        &format!("/api/v1/tenants/{TENANT}/model-providers/{PROVIDER}/models/"),
        Some(SESSION),
        Some(json!({"model_name": "fake-chat", "display_name": "Fake Chat v2", "capabilities": ["chat", "tools"], "context_window": 65536, "max_output_tokens": 8192, "fallback_model": MODEL_B})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    assert_eq!(body["version"], 2);
    assert_eq!(body["fallback_model"], MODEL_B);
    assert_eq!(body["fallback_model_name"], "fake-standby");

    let (status, body) = call(
        &state,
        "POST",
        &format!("/api/v1/tenants/{TENANT}/model-providers/{PROVIDER}/models/"),
        Some(SESSION),
        Some(json!({"model_name": "fake-chat", "display_name": "Too Big", "context_window": 8192, "max_output_tokens": 16384})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert!(body["error"]["details"]["max_output_tokens"].is_array());

    let (status, body) = call(
        &state,
        "PATCH",
        &format!("/api/v1/tenants/{TENANT}/model-configs/{MODEL_A}/"),
        Some(SESSION),
        Some(json!({"fallback_model": MODEL_B})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["fallback_model"], MODEL_B);

    let (status, body) = call(
        &state,
        "PATCH",
        &format!("/api/v1/tenants/{TENANT}/model-configs/{MODEL_B}/"),
        Some(SESSION),
        Some(json!({"fallback_model": MODEL_A})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert!(body["detail"].as_str().unwrap().contains("cycle"));

    let (status, body) = call(
        &state,
        "PATCH",
        &format!("/api/v1/tenants/{TENANT}/model-configs/{MODEL_A}/"),
        Some(SESSION),
        Some(json!({"fallback_model": MODEL_A})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert!(body["detail"].as_str().unwrap().contains("cycle"));
}

#[tokio::test]
async fn exposes_native_edge_liveness() {
    let state = state().await;
    let (status, body) = call(&state, "GET", "/health/live", None, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["service"], "rust-api-edge");
    assert_eq!(body["native_identity"], true);
}
