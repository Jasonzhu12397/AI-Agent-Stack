use ai_agent_stack_api::config::Config;
use ai_agent_stack_api::identity::session_token_hash;
use ai_agent_stack_api::proxy::{router, AppState};
use axum::body::Body;
use axum::http::{Request, StatusCode};
use serde_json::{json, Value};
use tower::ServiceExt;

const SESSION: &str = "account-session";

async fn state() -> AppState {
    let path = std::env::temp_dir().join(format!("rust-account-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE platform_users (id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL UNIQUE, email VARCHAR(254) NOT NULL DEFAULT '', is_staff INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1)",
        "CREATE TABLE platform_credentials (user_id INTEGER NOT NULL, credential_type VARCHAR(32) NOT NULL, credential_hash TEXT NOT NULL, PRIMARY KEY (user_id, credential_type))",
        "CREATE TABLE rust_sessions (token_hash CHAR(64) PRIMARY KEY, user_id INTEGER NOT NULL, expires_at DATETIME NOT NULL, created_at DATETIME NOT NULL, revoked_at DATETIME)",
        "CREATE TABLE platform_session_activity (token_hash CHAR(64) PRIMARY KEY, user_id INTEGER NOT NULL, created_at DATETIME NOT NULL, last_activity DATETIME NOT NULL, revoked_at DATETIME)",
        "CREATE TABLE platform_user_profiles (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE, display_name VARCHAR(160) NOT NULL, timezone VARCHAR(64) NOT NULL, locale VARCHAR(16) NOT NULL, updated_by_id INTEGER, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)",
        "CREATE TABLE core_tenantmembership (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL, role VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE core_auditevent (id TEXT PRIMARY KEY, tenant_id TEXT, project_id TEXT, actor_id INTEGER, event_type VARCHAR(80) NOT NULL, resource_type VARCHAR(80) NOT NULL, resource_id VARCHAR(160) NOT NULL, outcome VARCHAR(16) NOT NULL, request_id VARCHAR(64) NOT NULL, remote_addr VARCHAR(64), user_agent VARCHAR(300) NOT NULL, metadata TEXT NOT NULL, previous_hash VARCHAR(64) NOT NULL, event_hash VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    sqlx::query("INSERT INTO platform_users (id, username, email, is_staff, is_active) VALUES (1, 'alice', 'alice@example.com', 0, 1), (2, 'bob', 'bob@example.com', 0, 1)")
        .execute(&pool).await.unwrap();
    sqlx::query("INSERT INTO platform_credentials (user_id, credential_type, credential_hash) VALUES (1, 'password', 'pbkdf2_sha256$demo'), (2, 'password', '!disabled')")
        .execute(&pool).await.unwrap();

    let session_hash = session_token_hash(SESSION);
    let second_session_hash = session_token_hash("second-session");
    let bob_session_hash = session_token_hash("bob-session");
    for (hash, user_id, days) in [
        (session_hash.as_str(), 1_i64, 1_i64),
        (second_session_hash.as_str(), 1_i64, 2_i64),
        (bob_session_hash.as_str(), 2_i64, 3_i64),
    ] {
        sqlx::query("INSERT INTO rust_sessions (token_hash, user_id, expires_at, created_at) VALUES (?, ?, datetime('now', ?), datetime('now'))")
            .bind(hash)
            .bind(user_id)
            .bind(format!("+{days} days"))
            .execute(&pool)
            .await
            .unwrap();
        sqlx::query("INSERT INTO platform_session_activity (token_hash, user_id, created_at, last_activity) VALUES (?, ?, datetime('now'), datetime('now'))")
            .bind(hash)
            .bind(user_id)
            .execute(&pool)
            .await
            .unwrap();
    }
    sqlx::query("INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) VALUES ('membership-1', 'privacy-tenant', 1, 'owner', datetime('now'))")
        .execute(&pool).await.unwrap();
    pool.close().await;
    let config = Config::for_test("127.0.0.1:0".into(), url, None);
    AppState::new(config).unwrap()
}

async fn request(state: &AppState, method: &str, body: Option<Value>) -> (StatusCode, Value) {
    let builder = Request::builder()
        .method(method)
        .uri("/api/v1/users/me/")
        .header(
            "cookie",
            format!("sessionid={SESSION}; csrftoken=0123456789abcdef0123456789abcdef"),
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
async fn reads_and_updates_the_account_profile() {
    let state = state().await;
    let (status, body) = request(&state, "GET", None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["id"], "1");
    assert_eq!(body["username"], "alice");
    assert_eq!(body["display_name"], "");
    assert_eq!(body["timezone"], "UTC");
    assert_eq!(body["locale"], "zh-hans");

    let (status, body) = request(
        &state,
        "PATCH",
        Some(json!({
            "display_name": "Alice Chen",
            "email": "  ALICE@EXAMPLE.COM ",
            "timezone": "Asia/Shanghai",
            "locale": "en",
        })),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["display_name"], "Alice Chen");
    assert_eq!(body["email"], "alice@example.com");
    assert_eq!(body["timezone"], "Asia/Shanghai");
    assert_eq!(body["locale"], "en");

    let (status, body) = request(&state, "PATCH", Some(json!({"email": "bob@example.com"}))).await;
    assert_eq!(status, StatusCode::CONFLICT, "{body}");
    assert_eq!(body["error"]["code"], "ACCOUNT_EMAIL_TAKEN");

    let (status, body) = request(&state, "PATCH", Some(json!({"timezone": "  "}))).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"]["code"], "ACCOUNT_INVALID_VALUE");

    let (status, body) = request(&state, "PATCH", Some(json!({"ignored": "x"}))).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{body}");
    assert_eq!(body["error"]["code"], "ACCOUNT_NO_FIELDS");
}

#[tokio::test]
async fn lists_and_revokes_only_owned_sessions() {
    let state = state().await;
    let request = |method: &str, uri: &str, body: Option<Value>| {
        let builder = Request::builder()
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
        let builder = match body {
            Some(body) => builder
                .header("content-type", "application/json")
                .body(Body::from(body.to_string()))
                .unwrap(),
            None => builder.body(Body::empty()).unwrap(),
        };
        let state = state.clone();
        async move {
            let response = router(state).oneshot(builder).await.unwrap();
            let status = response.status();
            let bytes = axum::body::to_bytes(response.into_body(), 4 * 1024 * 1024)
                .await
                .unwrap();
            (
                status,
                serde_json::from_slice(&bytes).unwrap_or(Value::Null),
            )
        }
    };
    let (status, body) = request("GET", "/api/v1/users/me/sessions/", None).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let results = body["results"].as_array().unwrap();
    assert_eq!(results.len(), 2, "{body}");
    let current = results
        .iter()
        .find(|row| row["session_key"] == session_token_hash(SESSION))
        .unwrap();
    assert_eq!(current["is_current"], true);
    assert_eq!(current["timestamps_source"], "rust_session_store");
    assert!(current["created_at"].is_string());

    let (status, body) = request(
        "POST",
        &format!(
            "/api/v1/users/me/sessions/{}/revoke/",
            session_token_hash("second-session")
        ),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["revoked"], true);

    let (status, body) = request(
        "POST",
        &format!(
            "/api/v1/users/me/sessions/{}/revoke/",
            session_token_hash("second-session")
        ),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["revoked"], false);
    assert_eq!(body["idempotent"], true);

    let (status, body) = request(
        "POST",
        &format!(
            "/api/v1/users/me/sessions/{}/revoke/",
            session_token_hash("bob-session")
        ),
        None,
    )
    .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(body["revoked"], false);
}

#[tokio::test]
async fn reports_password_identity_state() {
    let state = state().await;
    let request = Request::builder()
        .method("GET")
        .uri("/api/v1/users/me/identities/")
        .header(
            "cookie",
            format!("sessionid={SESSION}; csrftoken=0123456789abcdef0123456789abcdef"),
        )
        .body(Body::empty())
        .unwrap();
    let response = router(state.clone()).oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(response.into_body(), 1024 * 1024)
        .await
        .unwrap();
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(body["has_password"], json!(true));
    assert_eq!(body["methods"], json!(["password"]));
    assert_eq!(body["bound_identities"], json!(0));
    assert_eq!(body["providers"], json!([]));
}

async fn privacy_request(state: &AppState, method: &str, uri: &str) -> (StatusCode, Value) {
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
        )
        .body(Body::empty())
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
async fn lists_and_revokes_privacy_sessions() {
    let state = state().await;
    let base = "/api/v1/tenants/privacy-tenant/privacy/sessions/";
    let (status, body) = privacy_request(&state, "GET", base).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    let results = body["results"].as_array().unwrap();
    assert_eq!(results.len(), 2);
    assert_eq!(results[0]["session_key_masked"].as_str().unwrap().len(), 16);
    assert!(!body.to_string().contains("second-session"));

    let revoke = format!("{base}{}/revoke/", session_token_hash("second-session"));
    let (status, body) = privacy_request(&state, "POST", &revoke).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["revoked"], true);
    assert_eq!(body["idempotent"], false);
    let (status, body) = privacy_request(&state, "GET", base).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["results"].as_array().unwrap().len(), 1);

    let (status, body) = privacy_request(&state, "POST", &revoke).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["revoked"], false);
    assert_eq!(body["idempotent"], true);

    let foreign = format!("{base}{}/revoke/", session_token_hash("bob-session"));
    let (status, body) = privacy_request(&state, "POST", &foreign).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(body["error"]["code"], "NOT_FOUND");
}
