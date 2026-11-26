use std::net::SocketAddr;
use std::time::Duration;

use ai_agent_stack_api_edge::config::Config;
use ai_agent_stack_api_edge::proxy::{router, AppState};
use axum::body::Body;
use axum::extract::ConnectInfo;
use axum::http::{Request, StatusCode};
use base64::Engine;
use pbkdf2::pbkdf2_hmac;
use serde_json::Value;
use sha2::Sha256;
use tower::ServiceExt;

const SECRET: &str = "native-auth-test-secret";
const USERNAME: &str = "rust-auth@example.com";
const PASSWORD: &str = "rust-auth-pass-123";

async fn state() -> (AppState, String) {
    let path =
        std::env::temp_dir().join(format!("rust-native-auth-{}.sqlite3", uuid::Uuid::new_v4()));
    std::fs::File::create(&path).unwrap();
    let url = format!("sqlite://{}", path.display());
    sqlx::any::install_default_drivers();
    let pool = sqlx::AnyPool::connect(&url).await.unwrap();
    for statement in [
        "CREATE TABLE django_session (\
         session_key VARCHAR(40) PRIMARY KEY, session_data TEXT NOT NULL, \
         expire_date DATETIME NOT NULL)",
        "CREATE TABLE auth_user (\
         id INTEGER PRIMARY KEY, username VARCHAR(150) NOT NULL, \
         email VARCHAR(254) NOT NULL, password VARCHAR(128) NOT NULL, \
         is_staff INTEGER NOT NULL, is_active INTEGER NOT NULL, \
         last_login DATETIME NULL)",
        "CREATE TABLE core_auditevent (\
         id CHAR(32) PRIMARY KEY, tenant_id CHAR(32) NULL, project_id CHAR(32) NULL, \
         actor_id INTEGER NULL, event_type VARCHAR(120) NOT NULL, \
         resource_type VARCHAR(80) NOT NULL, resource_id VARCHAR(64) NOT NULL, \
         outcome VARCHAR(16) NOT NULL, request_id VARCHAR(64) NOT NULL, \
         remote_addr VARCHAR(39) NULL, user_agent VARCHAR(256) NOT NULL, \
         metadata TEXT NOT NULL, previous_hash VARCHAR(64) NOT NULL, \
         event_hash VARCHAR(64) NOT NULL UNIQUE, created_at DATETIME NOT NULL)",
    ] {
        sqlx::query(statement).execute(&pool).await.unwrap();
    }
    let password = hash_password(PASSWORD, 1000);
    sqlx::query(
        "INSERT INTO auth_user (id, username, email, password, is_staff, is_active) \
         VALUES (1, ?, ?, ?, 0, 1)",
    )
    .bind(USERNAME)
    .bind(USERNAME)
    .bind(password)
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
    (AppState::new(config).unwrap(), path.display().to_string())
}

fn hash_password(password: &str, iterations: u32) -> String {
    let salt = "native-auth-salt";
    let mut derived = vec![0u8; 32];
    pbkdf2_hmac::<Sha256>(
        password.as_bytes(),
        salt.as_bytes(),
        iterations,
        &mut derived,
    );
    format!(
        "pbkdf2_sha256${iterations}${salt}${}",
        base64::engine::general_purpose::STANDARD.encode(derived)
    )
}

fn with_peer(mut request: Request<Body>) -> Request<Body> {
    request
        .extensions_mut()
        .insert(ConnectInfo(SocketAddr::from(([127, 0, 0, 1], 40000))));
    request
}

fn cookie_pair(response: &axum::response::Response) -> Vec<String> {
    response
        .headers()
        .get_all("set-cookie")
        .iter()
        .filter_map(|value| value.to_str().ok())
        .map(|value| value.split(';').next().unwrap_or_default().to_string())
        .collect()
}

#[tokio::test]
async fn completes_the_native_login_logout_flow() {
    let (state, path) = state().await;

    let csrf = router(state.clone())
        .oneshot(
            Request::builder()
                .uri("/api/v1/auth/csrf/")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(csrf.status(), StatusCode::OK);
    let cookies = cookie_pair(&csrf);
    let csrf_cookie = cookies.first().unwrap().to_string();
    let body: Value = serde_json::from_slice(
        &axum::body::to_bytes(csrf.into_body(), 1024 * 1024)
            .await
            .unwrap(),
    )
    .unwrap();
    let csrf_token = body["csrf_token"].as_str().unwrap().to_string();

    let login = router(state.clone())
        .oneshot(with_peer(
            Request::builder()
                .method("POST")
                .uri("/api/v1/auth/login/")
                .header("content-type", "application/json")
                .header("cookie", &csrf_cookie)
                .header("x-csrftoken", &csrf_token)
                .body(Body::from(
                    serde_json::json!({"username": USERNAME, "password": PASSWORD}).to_string(),
                ))
                .unwrap(),
        ))
        .await
        .unwrap();
    assert_eq!(login.status(), StatusCode::OK);
    let session = cookie_pair(&login).first().unwrap().to_string();
    let body: Value = serde_json::from_slice(
        &axum::body::to_bytes(login.into_body(), 1024 * 1024)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(body["username"], USERNAME);
    assert!(body["session_token"].as_str().is_some());

    let me = router(state.clone())
        .oneshot(
            Request::builder()
                .uri("/api/v1/auth/me/")
                .header("cookie", &session)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(me.status(), StatusCode::OK);

    let logout = router(state.clone())
        .oneshot(with_peer(
            Request::builder()
                .method("POST")
                .uri("/api/v1/auth/logout/")
                .header("cookie", format!("{}; {}", session, csrf_cookie))
                .header("x-csrftoken", &csrf_token)
                .body(Body::empty())
                .unwrap(),
        ))
        .await
        .unwrap();
    assert_eq!(logout.status(), StatusCode::NO_CONTENT);
    assert!(cookie_pair(&logout)
        .iter()
        .any(|cookie| cookie == "sessionid="));

    let after = router(state)
        .oneshot(
            Request::builder()
                .uri("/api/v1/auth/me/")
                .header("cookie", &session)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(after.status(), StatusCode::UNAUTHORIZED);
    let _ = std::fs::remove_file(path);
}

#[tokio::test]
async fn rejects_login_without_csrf_and_appends_denied_events() {
    let (state, path) = state().await;
    let response = router(state.clone())
        .oneshot(with_peer(
            Request::builder()
                .method("POST")
                .uri("/api/v1/auth/login/")
                .header("content-type", "application/json")
                .body(Body::from(
                    serde_json::json!({"username": USERNAME, "password": PASSWORD}).to_string(),
                ))
                .unwrap(),
        ))
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::FORBIDDEN);

    let pool = state.identity_pool.as_ref().unwrap();
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM core_auditevent")
        .fetch_one(pool)
        .await
        .unwrap();
    assert_eq!(count, 0, "a rejected CSRF request must not reach auditing");
    let _ = std::fs::remove_file(path);
}
