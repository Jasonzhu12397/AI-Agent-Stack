use std::time::Duration;

use ai_agent_stack_api_edge::config::Config;
use ai_agent_stack_api_edge::identity::encode_session_data;
use ai_agent_stack_api_edge::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;

const SECRET: &str = "local-only-change-before-production";
const SESSION: &str = "preferences-session";

async fn state() -> AppState {
    let path =
        std::env::temp_dir().join(format!("rust-preferences-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE django_session (session_key VARCHAR(40) PRIMARY KEY, session_data TEXT NOT NULL, expire_date DATETIME NOT NULL)",
        "CREATE TABLE auth_user (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, email VARCHAR(254) NOT NULL, password VARCHAR(128) NOT NULL, is_staff INTEGER NOT NULL, is_active INTEGER NOT NULL)",
        "CREATE TABLE core_userpreference (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, namespace VARCHAR(32) NOT NULL, \"values\" TEXT NOT NULL, updated_by_id INTEGER, updated_at DATETIME NOT NULL, UNIQUE(user_id, namespace))",
        "CREATE TABLE core_tenantmembership (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL)",
        "CREATE TABLE core_tenantsetting (id TEXT PRIMARY KEY, section VARCHAR(32) NOT NULL, \"values\" TEXT NOT NULL, updated_at DATETIME NOT NULL, tenant_id TEXT NOT NULL, updated_by_id INTEGER, UNIQUE(tenant_id, section))",
        "CREATE TABLE core_projectsetting (id TEXT PRIMARY KEY, section VARCHAR(32) NOT NULL, \"values\" TEXT NOT NULL, updated_at DATETIME NOT NULL, tenant_id TEXT NOT NULL, project_id TEXT NOT NULL, updated_by_id INTEGER, UNIQUE(project_id, section))",
        "CREATE TABLE core_projectmembership (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL)",
        "CREATE TABLE core_notificationendpoint (id TEXT PRIMARY KEY, channel VARCHAR(16) NOT NULL, name VARCHAR(120) NOT NULL, sealed_url TEXT NOT NULL, sealed_secret TEXT NOT NULL, enabled INTEGER NOT NULL, last_sent_at DATETIME, last_error VARCHAR(200) NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, tenant_id TEXT NOT NULL)",
        "CREATE TABLE core_feedbackentry (id TEXT PRIMARY KEY, category VARCHAR(32) NOT NULL, severity VARCHAR(16) NOT NULL, body TEXT NOT NULL, created_at DATETIME NOT NULL, tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL)",
        "CREATE TABLE core_auditevent (id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, actor_id INTEGER, event_type VARCHAR(80) NOT NULL, resource_type VARCHAR(80) NOT NULL, resource_id VARCHAR(160) NOT NULL, outcome VARCHAR(16) NOT NULL, request_id VARCHAR(64) NOT NULL, remote_addr VARCHAR(64), user_agent VARCHAR(300) NOT NULL, metadata TEXT NOT NULL, previous_hash VARCHAR(64) NOT NULL, event_hash VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO auth_user (id, username, email, password, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 'pbkdf2_sha256$demo', 0, 1)")
        .execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role) VALUES ('m1', '4c900ce633d9463d88256c3371c48bc8', 1, 'admin')")
        .execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO core_projectmembership (id, project_id, user_id, role) VALUES ('pm1', 'aad6c762ebb6461da08507f141d8b3eb', 1, 'admin')")
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
    let builder = Request::builder()
        .method(method)
        .uri(uri)
        .header(
            "cookie",
            format!(
                "sessionid={SESSION}; csrftoken={}",
                "0123456789abcdef0123456789abcdef"
            ),
        )
        .header(
            "x-csrftoken",
            format!("{}{}", "a".repeat(32), "0123456789abcdef0123456789abcdef"),
        );
    let request = match body {
        Some(body) => builder
            .header("content-type", "application/json")
            .body(Body::from(body.to_string()))
            .unwrap(),
        None => builder.body(Body::empty()).unwrap(),
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
async fn reads_defaults_and_updates_preferences() {
    let state = state().await;
    let (status, body) = call(
        &state,
        "GET",
        "/api/v1/users/me/preferences/appearance/",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["values"], json!({}));
    assert_eq!(body["effective"]["theme"], json!("system"));
    assert_eq!(body["sources"]["theme"], json!("default"));

    let (status, body) = call(
        &state,
        "PATCH",
        "/api/v1/users/me/preferences/appearance/",
        Some(json!({"theme": "dark"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["values"], json!({"theme": "dark"}));
    assert_eq!(body["effective"]["theme"], json!("dark"));
    assert_eq!(body["sources"]["theme"], json!("user"));

    let (status, body) = call(
        &state,
        "PATCH",
        "/api/v1/users/me/preferences/appearance/",
        Some(json!({"theme": "neon"})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], json!("SETTINGS_INVALID_VALUE"));
}

#[tokio::test]
async fn rejects_unknown_namespaces_and_shortcut_conflicts() {
    let state = state().await;
    let (status, _) = call(&state, "GET", "/api/v1/users/me/preferences/unknown/", None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);

    let (status, body) = call(
        &state,
        "PATCH",
        "/api/v1/users/me/preferences/shortcuts/",
        Some(json!({"unknown.action": "Mod+B"})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], json!("SETTINGS_UNKNOWN_KEY"));

    let (status, body) = call(
        &state,
        "PUT",
        "/api/v1/users/me/preferences/shortcuts/",
        Some(json!({"sidebar.toggle": "Mod+B", "command.palette": "mod+b"})),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["error"]["code"], json!("SHORTCUT_CONFLICT"));

    let (status, body) = call(
        &state,
        "PUT",
        "/api/v1/users/me/preferences/shortcuts/",
        Some(json!({"sidebar.toggle": "Mod+B"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["values"], json!({"sidebar.toggle": "Mod+B"}));
}

#[tokio::test]
async fn exposes_meta_about_and_shortcut_registry() {
    let state = state().await;
    let (status, body) = call(&state, "GET", "/api/v1/meta/about/", None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["version"], json!("1.1.0"));
    assert_eq!(body["licenses"]["file"], json!("LICENSE"));

    let (status, body) = call(&state, "GET", "/api/v1/meta/shortcuts/", None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["results"].as_array().unwrap().len(), 5);
    assert_eq!(body["results"][1]["id"], json!("command.palette"));
}

#[tokio::test]
async fn lists_and_creates_tenant_feedback() {
    let state = state().await;
    let uri = "/api/v1/tenants/4c900ce6-33d9-463d-8825-6c3371c48bc8/feedback/";
    let (status, body) = call(
        &state,
        "POST",
        uri,
        Some(json!({"category": "bug", "severity": "high", "body": "  Export is slow.  "})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED);
    assert_eq!(body["body"], json!("Export is slow."));

    let (status, body) = call(
        &state,
        "POST",
        uri,
        Some(json!({"category": "nope", "severity": "high", "body": ""})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], json!("FEEDBACK_INVALID"));

    let (status, body) = call(&state, "GET", uri, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["results"].as_array().unwrap().len(), 1);

    let (status, _) = call(
        &state,
        "GET",
        "/api/v1/tenants/ffffffffffffffffffffffffffffffff/feedback/",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn manages_notification_endpoints_without_exposing_secrets() {
    let state = state().await;
    let base =
        "/api/v1/tenants/4c900ce6-33d9-463d-8825-6c3371c48bc8/settings/notifications/endpoints/";
    let (status, body) = call(
        &state,
        "POST",
        base,
        Some(json!({"channel": "dingtalk", "name": "Ops", "webhook_url": "https://example.invalid/robot", "secret": "sign-key"})),
    )
    .await;
    assert_eq!(status, StatusCode::CREATED, "{}", body);
    assert!(body.get("sealed_url").is_none());
    assert_eq!(body["enabled"], json!(true));
    let id = body["id"].as_str().unwrap().to_string();

    let (status, body) = call(&state, "GET", base, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["results"].as_array().unwrap().len(), 1);
    assert_eq!(body["results"][0]["name"], json!("Ops"));

    let (status, _) = call(
        &state,
        "POST",
        base,
        Some(json!({"channel": "slack", "name": "Bad", "webhook_url": "https://example.invalid"})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);

    let detail = format!("{base}{id}/");
    let (status, body) = call(
        &state,
        "PATCH",
        &detail,
        Some(json!({"name": "Renamed", "enabled": false})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["name"], json!("Renamed"));
    assert_eq!(body["enabled"], json!(false));

    let (status, _) = call(
        &state,
        "PATCH",
        &detail,
        Some(json!({"webhook_url": "https://127.0.0.1:9/hook"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    let test_uri = format!("{base}{id}/test/");
    let (status, body) = call(&state, "POST", &test_uri, Some(json!({}))).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["ok"], json!(false));
    assert!(body["message"]
        .as_str()
        .unwrap()
        .contains("transport error"));

    let (status, _) = call(&state, "DELETE", &detail, None).await;
    assert_eq!(status, StatusCode::NO_CONTENT);
    let (status, body) = call(&state, "GET", base, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["results"].as_array().unwrap().len(), 0);
}

#[tokio::test]
async fn manages_tenant_and_project_settings_with_inheritance() {
    let state = state().await;
    let tenant = "/api/v1/tenants/4c900ce6-33d9-463d-8825-6c3371c48bc8/settings/general/";
    let (status, body) = call(&state, "GET", tenant, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["effective"]["items_per_page"], json!(20));
    assert_eq!(body["sources"]["items_per_page"], json!("default"));

    let (status, body) = call(
        &state,
        "PATCH",
        tenant,
        Some(json!({"items_per_page": 30, "compact_mode": true})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["values"]["items_per_page"], json!(30));
    assert_eq!(body["sources"]["items_per_page"], json!("tenant"));

    let (status, body) = call(
        &state,
        "PATCH",
        tenant,
        Some(json!({"items_per_page": 300})),
    )
    .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"]["code"], json!("SETTINGS_INVALID_VALUE"));

    let project = "/api/v1/tenants/4c900ce6-33d9-463d-8825-6c3371c48bc8/projects/aad6c762-ebb6-461d-a085-07f141d8b3eb/settings/general/";
    let (status, body) = call(&state, "GET", project, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["effective"]["items_per_page"], json!(30));
    assert_eq!(body["sources"]["items_per_page"], json!("tenant"));
    assert_eq!(body["sources"]["compact_mode"], json!("tenant"));

    let (status, body) = call(
        &state,
        "PATCH",
        project,
        Some(json!({"compact_mode": false, "default_landing_page": "workflows"})),
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["values"]["compact_mode"], json!(false));
    assert_eq!(body["effective"]["items_per_page"], json!(30));
    assert_eq!(body["sources"]["items_per_page"], json!("tenant"));
    assert_eq!(body["sources"]["default_landing_page"], json!("project"));
}
