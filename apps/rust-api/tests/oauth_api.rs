use std::time::Duration;

use ai_agent_stack_api_edge::config::Config;
use ai_agent_stack_api_edge::identity::encode_session_data;
use ai_agent_stack_api_edge::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use sqlx::Row;
use tower::ServiceExt;

const SECRET: &str = "local-only-change-before-production";
const TENANT: &str = "11111111-1111-1111-1111-111111111111";
const SESSION: &str = "owner-session";

async fn state() -> AppState {
    let path =
        std::env::temp_dir().join(format!("rust-oauth-api-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE django_session (session_key VARCHAR(40) PRIMARY KEY, session_data TEXT NOT NULL, expire_date DATETIME NOT NULL)",
        "CREATE TABLE auth_user (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, email VARCHAR(254) NOT NULL, is_staff INTEGER NOT NULL, is_active INTEGER NOT NULL)",
        "CREATE TABLE core_tenant (id TEXT PRIMARY KEY, name VARCHAR(120) NOT NULL, slug VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_tenantmembership (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_provideroauthstate (id TEXT PRIMARY KEY, preset_key VARCHAR(64) NOT NULL, state VARCHAR(128) NOT NULL UNIQUE, sealed_verifier TEXT NOT NULL, redirect_uri TEXT NOT NULL, expires_at DATETIME NOT NULL, consumed_at DATETIME, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL)",
        "CREATE TABLE core_modelprovider (id TEXT PRIMARY KEY, tenant_id TEXT, created_by_id INTEGER NOT NULL, name VARCHAR(120) NOT NULL, provider_type VARCHAR(32) NOT NULL, auth_mode VARCHAR(32) NOT NULL, preset_key VARCHAR(40) NOT NULL, base_url VARCHAR(500) NOT NULL, credential_ref VARCHAR(200) NOT NULL, config TEXT NOT NULL, enabled BOOLEAN NOT NULL, deployment_region VARCHAR(80) NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_modelconfig (id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, model_name VARCHAR(160) NOT NULL, display_name VARCHAR(160) NOT NULL, capabilities TEXT NOT NULL, context_window INTEGER NOT NULL, max_output_tokens INTEGER NOT NULL, input_cost_per_million REAL NOT NULL, output_cost_per_million REAL NOT NULL, version INTEGER NOT NULL, enabled BOOLEAN NOT NULL, fallback_model_id TEXT, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_provideraccountlink (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, provider_id TEXT NOT NULL, status VARCHAR(16) NOT NULL, account_label VARCHAR(160) NOT NULL, credential_ref VARCHAR(200) NOT NULL, linked_at DATETIME NOT NULL, revoked_at DATETIME, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_membercredentialsecret (id TEXT PRIMARY KEY, sealed_value TEXT NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, user_id INTEGER NOT NULL)",
        "CREATE TABLE core_provideraccountusage (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, provider_id TEXT NOT NULL, idempotency_key VARCHAR(120) NOT NULL, status VARCHAR(16) NOT NULL, reserved_tokens INTEGER NOT NULL, consumed_tokens INTEGER NOT NULL, expires_at DATETIME, settled_at DATETIME, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_auditevent (id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, actor_id INTEGER, event_type VARCHAR(120) NOT NULL, resource_type VARCHAR(80) NOT NULL, resource_id VARCHAR(64) NOT NULL, outcome VARCHAR(16) NOT NULL, request_id VARCHAR(64) NOT NULL, remote_addr TEXT, user_agent VARCHAR(256) NOT NULL, metadata TEXT NOT NULL, previous_hash VARCHAR(64) NOT NULL, event_hash VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)",
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
    let bytes = axum::body::to_bytes(response.into_body(), 4 * 1024 * 1024)
        .await
        .unwrap();
    let value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, value)
}

#[tokio::test]
async fn starts_authorization_with_state_and_pkce_challenge() {
    let state = state().await;
    let (status, body) = call(
        &state,
        "POST",
        &format!("/api/v1/tenants/{TENANT}/model-providers/oauth/openrouter/start/"),
        Some(json!({"redirect_uri": "https://console.example/oauth/callback"})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    let url = body["authorization_url"].as_str().unwrap();
    assert!(url.starts_with("https://openrouter.ai/auth?"));
    assert!(url.contains("callback_url=https%3A%2F%2Fconsole.example%2Foauth%2Fcallback"));
    assert!(url.contains("code_challenge_method=S256"));
    let state_token = body["state"].as_str().unwrap();
    assert_eq!(state_token.len(), 32);

    let pool = state.identity_pool.as_ref().unwrap();
    let row =
        sqlx::query("SELECT preset_key, redirect_uri, sealed_verifier FROM core_provideroauthstate WHERE state = ?")
            .bind(state_token)
            .fetch_one(pool)
            .await
            .unwrap();
    assert_eq!(
        row.try_get::<String, _>("preset_key").unwrap(),
        "openrouter"
    );
    assert_eq!(
        row.try_get::<String, _>("redirect_uri").unwrap(),
        "https://console.example/oauth/callback"
    );
    assert!(!row
        .try_get::<String, _>("sealed_verifier")
        .unwrap()
        .is_empty());
}

#[tokio::test]
async fn rejects_presets_without_authorization_and_relative_callbacks() {
    let state = state().await;
    let (status, body) = call(
        &state,
        "POST",
        &format!("/api/v1/tenants/{TENANT}/model-providers/oauth/openai/start/"),
        Some(json!({"redirect_uri": "https://console.example/oauth/callback"})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "OAUTH_PRESET_UNSUPPORTED");

    let (status, body) = call(
        &state,
        "POST",
        &format!("/api/v1/tenants/{TENANT}/model-providers/oauth/openrouter/start/"),
        Some(json!({"redirect_uri": "/oauth/callback"})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "OAUTH_REDIRECT_INVALID");
}

#[tokio::test]
async fn refuses_unknown_expired_and_consumed_attempts_before_calling_the_platform() {
    let state = state().await;
    let uri = format!("/api/v1/tenants/{TENANT}/model-providers/oauth/openrouter/exchange/");
    let (status, body) = call(
        &state,
        "POST",
        &uri,
        Some(json!({"code": "one", "state": "missing"})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "OAUTH_STATE_UNKNOWN");

    let pool = state.identity_pool.as_ref().unwrap();
    for (token, expires, consumed) in [
        ("expired", "2020-01-01 00:00:00", None),
        ("used", "2030-01-01 00:00:00", Some("2026-01-01 00:00:00")),
    ] {
        sqlx::query(
            "INSERT INTO core_provideroauthstate (id, preset_key, state, sealed_verifier, redirect_uri, \
             expires_at, consumed_at, created_at, updated_at, tenant_id, user_id) \
             VALUES (?, 'openrouter', ?, 'sealed', 'https://console.example/cb', ?, ?, datetime('now'), \
             datetime('now'), ?, 1)",
        )
        .bind(uuid::Uuid::new_v4().to_string())
        .bind(token)
        .bind(expires)
        .bind(consumed)
        .bind(TENANT)
        .execute(pool)
        .await
        .unwrap();
    }
    let (status, body) = call(
        &state,
        "POST",
        &uri,
        Some(json!({"code": "one", "state": "expired"})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "OAUTH_STATE_EXPIRED");

    let (status, body) = call(
        &state,
        "POST",
        &uri,
        Some(json!({"code": "one", "state": "used"})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "OAUTH_STATE_UNKNOWN");
}
