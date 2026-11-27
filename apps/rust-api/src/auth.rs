use std::net::SocketAddr;

use axum::body::Bytes;
use axum::extract::{ConnectInfo, State};
use axum::http::{header, HeaderMap, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use base64::Engine;
use pbkdf2::pbkdf2_hmac;
use serde::Deserialize;
use serde_json::json;
use sha2::Sha256;

use crate::audit::{record_auth_event, AuthAudit};
use crate::identity::{new_session_key, session_age_seconds, session_token_hash};
use crate::proxy::AppState;

const CSRF_COOKIE: &str = "csrftoken";
const CSRF_HEADER: &str = "x-csrftoken";
const CSRF_ALLOWED: &[u8] = b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
const CSRF_SECRET_LENGTH: usize = 32;
const CSRF_TOKEN_LENGTH: usize = 64;

#[derive(Deserialize)]
struct LoginPayload {
    username: String,
    password: String,
}

/// Native `GET /api/v1/auth/csrf/`.
///
/// The cookie is the 32-character secret while the JSON token is the 64-char
/// masked CSRF value returned to the web console; the mask changes on every call,
/// so a cached response cannot pair a stale token with a live cookie.
pub async fn csrf(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let secret = cookie_value(&headers, CSRF_COOKIE)
        .filter(|value| value.len() == CSRF_SECRET_LENGTH && valid_csrf_chars(value))
        .unwrap_or_else(csrf_secret);
    let mask = csrf_secret();
    let token = mask_secret(&secret, &mask);
    let mut response = Json(json!({ "csrf_token": token })).into_response();
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        HeaderValue::from_static("no-store, private, max-age=0"),
    );
    response
        .headers_mut()
        .insert(header::PRAGMA, HeaderValue::from_static("no-cache"));
    response
        .headers_mut()
        .insert(header::VARY, HeaderValue::from_static("Cookie"));
    append_cookie(
        &mut response,
        &csrf_cookie(&secret, state.config.secure_cookies),
    );
    response
}

/// Native `POST /api/v1/auth/login/`.
pub async fn login(
    State(state): State<AppState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable("The Rust identity pool is not configured.");
    };
    if let Err(response) = enforce_csrf(&headers) {
        return *response;
    }

    let payload: LoginPayload = match serde_json::from_slice(&body) {
        Ok(payload) => payload,
        Err(_) => {
            audit_denied(&state, &headers, peer, "invalid_payload", None).await;
            return bad_request("Username and password are required.");
        }
    };

    let user = sqlx::query_as::<_, (i64, String, String, String, i64)>(
        "SELECT u.id, u.username, u.email, c.credential_hash, \
         CASE WHEN u.is_active THEN 1 ELSE 0 END \
         FROM platform_users u JOIN platform_credentials c ON c.user_id = u.id \
         AND c.credential_type = 'password' WHERE u.username = ?",
    )
    .bind(&payload.username)
    .fetch_optional(pool)
    .await;
    let (user_id, username, email, encoded_password, is_active) = match user {
        Ok(Some(user)) => user,
        Ok(None) => {
            audit_denied(&state, &headers, peer, "invalid_credentials", None).await;
            return bad_request("Invalid username or password.");
        }
        Err(error) => {
            tracing::error!(%error, "rust auth could not read the user");
            return unavailable("The user store could not be read.");
        }
    };
    if is_active == 0 || !verify_password(&encoded_password, &payload.password) {
        audit_denied(&state, &headers, peer, "invalid_credentials", Some(user_id)).await;
        return bad_request("Invalid username or password.");
    }

    let session_key = new_session_key();
    let request_id = header_string(&headers, "x-request-id");
    let remote = peer.ip().to_string();
    let user_agent = header_string(&headers, "user-agent");
    let audit = AuthAudit {
        event_type: "auth.login.succeeded",
        outcome: "success",
        reason: "authenticated",
        actor_id: Some(user_id),
        request_id: &request_id,
        remote_addr: Some(&remote),
        user_agent: &user_agent,
    };
    if let Err(error) = persist_session(&state, pool, user_id, &session_key).await {
        tracing::error!(%error, "rust auth could not persist the session");
        return unavailable("The session could not be created.");
    }
    record_session_activity(
        pool,
        user_id,
        &session_key,
        database_url(&state).starts_with("postgres"),
    )
    .await;

    if let Err(error) = record_auth_event(pool, database_url(&state), audit).await {
        tracing::error!(%error, "rust auth could not append the login audit event");
        if let Err(cleanup_error) = sqlx::query("DELETE FROM rust_sessions WHERE token_hash = ?")
            .bind(session_token_hash(&session_key))
            .execute(pool)
            .await
        {
            tracing::error!(%cleanup_error, "rust auth could not clean up an unaudited login session");
        }
        return unavailable("The login audit event could not be recorded.");
    }
    let _ = sqlx::query("UPDATE platform_users SET last_login = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?")
        .bind(user_id)
        .execute(pool)
        .await;

    let mut response = Json(json!({
        "id": user_id,
        "username": username,
        "email": email,
        "session_token": session_key,
    }))
    .into_response();
    append_cookie(
        &mut response,
        &session_cookie(&session_key, state.config.secure_cookies),
    );
    response
}

async fn record_session_activity(
    pool: &sqlx::AnyPool,
    user_id: i64,
    session_key: &str,
    postgres: bool,
) {
    let now = if postgres { "NOW()" } else { "datetime('now')" };
    let sql = format!(
        "INSERT INTO platform_session_activity (token_hash, user_id, created_at, last_activity, revoked_at) \
         VALUES (?, ?, {now}, {now}, NULL) \
         ON CONFLICT(token_hash) DO UPDATE SET user_id = excluded.user_id, \
         last_activity = excluded.last_activity, revoked_at = NULL"
    );
    let result = sqlx::query(&sql)
        .bind(session_token_hash(session_key))
        .bind(user_id)
        .execute(pool)
        .await;
    if let Err(error) = result {
        tracing::debug!(%error, "rust auth skipped session activity metadata");
    }
}

async fn mark_session_revoked(pool: &sqlx::AnyPool, session_key: &str, postgres: bool) {
    let now = if postgres { "NOW()" } else { "datetime('now')" };
    let sql = format!(
        "UPDATE platform_session_activity SET revoked_at = {now} \
         WHERE token_hash = ? AND revoked_at IS NULL"
    );
    let result = sqlx::query(&sql)
        .bind(session_token_hash(session_key))
        .execute(pool)
        .await;
    if let Err(error) = result {
        tracing::debug!(%error, "rust auth skipped session revocation metadata");
    }
}

/// Native `POST /api/v1/auth/logout/`.
pub async fn logout(
    State(state): State<AppState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
) -> Response {
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable("The Rust identity pool is not configured.");
    };
    if let Err(response) = enforce_csrf(&headers) {
        return *response;
    }
    let Some(session_key) = crate::identity::session_key_from_headers(&headers) else {
        return unauthenticated("Authentication credentials were not provided.");
    };
    let token_hash = session_token_hash(&session_key);
    let actor_id = match sqlx::query_scalar::<_, i64>(
        "SELECT user_id FROM rust_sessions WHERE token_hash = ? \
         AND expires_at > CURRENT_TIMESTAMP AND revoked_at IS NULL",
    )
    .bind(&token_hash)
    .fetch_optional(pool)
    .await
    {
        Ok(Some(user_id)) => Some(user_id),
        Ok(None) => return unauthenticated("This session has expired. Sign in again."),
        Err(error) => {
            tracing::error!(%error, "rust auth could not read the logout session");
            return unavailable("The session store could not be read.");
        }
    };

    let request_id = header_string(&headers, "x-request-id");
    let remote = peer.ip().to_string();
    let user_agent = header_string(&headers, "user-agent");
    if let Err(error) = sqlx::query("UPDATE rust_sessions SET revoked_at = CURRENT_TIMESTAMP WHERE token_hash = ? AND revoked_at IS NULL")
        .bind(&token_hash)
        .execute(pool)
        .await
    {
        tracing::error!(%error, "rust auth could not delete the logout session");
        return unavailable("The session could not be invalidated.");
    }
    mark_session_revoked(
        pool,
        &session_key,
        database_url(&state).starts_with("postgres"),
    )
    .await;

    let audit = AuthAudit {
        event_type: "auth.logout.succeeded",
        outcome: "success",
        reason: "signed_out",
        actor_id,
        request_id: &request_id,
        remote_addr: Some(&remote),
        user_agent: &user_agent,
    };
    if let Err(error) = record_auth_event(pool, database_url(&state), audit).await {
        tracing::error!(%error, "rust auth could not append the logout audit event");
        return unavailable("The logout audit event could not be recorded.");
    }

    let mut response = StatusCode::NO_CONTENT.into_response();
    append_cookie(
        &mut response,
        &format!(
            "sessionid=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{}",
            secure_flag(state.config.secure_cookies)
        ),
    );
    response
}

async fn audit_denied(
    state: &AppState,
    headers: &HeaderMap,
    peer: SocketAddr,
    reason: &str,
    actor_id: Option<i64>,
) {
    let request_id = header_string(headers, "x-request-id");
    let remote = peer.ip().to_string();
    let user_agent = header_string(headers, "user-agent");
    let audit = AuthAudit {
        event_type: "auth.login.denied",
        outcome: "denied",
        reason,
        actor_id,
        request_id: &request_id,
        remote_addr: Some(&remote),
        user_agent: &user_agent,
    };
    if let Some(pool) = state.identity_pool.as_ref() {
        if let Err(error) = record_auth_event(pool, database_url(state), audit).await {
            tracing::error!(%error, "rust auth could not append the denied audit event");
        }
    }
}

async fn persist_session(
    state: &AppState,
    pool: &sqlx::AnyPool,
    user_id: i64,
    session_key: &str,
) -> Result<(), String> {
    let postgres = database_url(state).starts_with("postgres://")
        || database_url(state).starts_with("postgresql://");
    let sql = if postgres {
        "INSERT INTO rust_sessions (token_hash, user_id, expires_at, created_at) \
         VALUES (?, ?, NOW() + INTERVAL '1209600 seconds', NOW())"
    } else {
        "INSERT INTO rust_sessions (token_hash, user_id, expires_at, created_at) \
         VALUES (?, ?, datetime('now', '+1209600 seconds'), datetime('now'))"
    };
    sqlx::query(sql)
        .bind(session_token_hash(session_key))
        .bind(user_id)
        .execute(pool)
        .await
        .map(|_| ())
        .map_err(|error| error.to_string())
}

fn database_url(state: &AppState) -> &str {
    state.config.database_url.as_deref().unwrap_or_default()
}

pub(crate) fn enforce_csrf(headers: &HeaderMap) -> Result<(), Box<Response>> {
    let Some(cookie_secret) = cookie_value(headers, CSRF_COOKIE) else {
        return Err(Box::new(csrf_failure("CSRF cookie not set.")));
    };
    let Some(token) = headers
        .get(CSRF_HEADER)
        .and_then(|value| value.to_str().ok())
    else {
        return Err(Box::new(csrf_failure("CSRF token missing.")));
    };
    if cookie_secret.len() != CSRF_SECRET_LENGTH || !valid_csrf_chars(&cookie_secret) {
        return Err(Box::new(csrf_failure("CSRF cookie has an invalid format.")));
    }
    if token.len() != CSRF_TOKEN_LENGTH || !valid_csrf_chars(token) {
        return Err(Box::new(csrf_failure("CSRF token has an invalid format.")));
    }
    let unmasked = unmask_secret(token);
    if !constant_time_eq(unmasked.as_bytes(), cookie_secret.as_bytes()) {
        return Err(Box::new(csrf_failure("CSRF token incorrect.")));
    }
    Ok(())
}

fn csrf_secret() -> String {
    let bytes = [uuid::Uuid::new_v4(), uuid::Uuid::new_v4()];
    let mut secret = String::with_capacity(CSRF_SECRET_LENGTH);
    for byte in bytes
        .iter()
        .flat_map(|uuid| uuid.as_bytes().iter().copied())
        .take(CSRF_SECRET_LENGTH)
    {
        secret.push(CSRF_ALLOWED[byte as usize % CSRF_ALLOWED.len()] as char);
    }
    secret
}

fn mask_secret(secret: &str, mask: &str) -> String {
    let mut masked = String::with_capacity(CSRF_TOKEN_LENGTH);
    masked.push_str(mask);
    for (secret_char, mask_char) in secret.chars().zip(mask.chars()) {
        let secret_index = CSRF_ALLOWED
            .iter()
            .position(|candidate| *candidate == secret_char as u8)
            .unwrap_or(0);
        let mask_index = CSRF_ALLOWED
            .iter()
            .position(|candidate| *candidate == mask_char as u8)
            .unwrap_or(0);
        masked.push(CSRF_ALLOWED[(secret_index + mask_index) % CSRF_ALLOWED.len()] as char);
    }
    masked
}

fn unmask_secret(token: &str) -> String {
    let mask = &token[..CSRF_SECRET_LENGTH];
    let cipher = &token[CSRF_SECRET_LENGTH..];
    let mut secret = String::with_capacity(CSRF_SECRET_LENGTH);
    for (cipher_char, mask_char) in cipher.chars().zip(mask.chars()) {
        let cipher_index = CSRF_ALLOWED
            .iter()
            .position(|candidate| *candidate == cipher_char as u8)
            .unwrap_or(0) as isize;
        let mask_index = CSRF_ALLOWED
            .iter()
            .position(|candidate| *candidate == mask_char as u8)
            .unwrap_or(0) as isize;
        let index = (cipher_index - mask_index).rem_euclid(CSRF_ALLOWED.len() as isize) as usize;
        secret.push(CSRF_ALLOWED[index] as char);
    }
    secret
}

fn valid_csrf_chars(value: &str) -> bool {
    value.bytes().all(|byte| CSRF_ALLOWED.contains(&byte))
}

fn verify_password(encoded: &str, password: &str) -> bool {
    let mut parts = encoded.split('$');
    let algorithm = parts.next().unwrap_or_default();
    let iterations = parts.next().and_then(|value| value.parse::<u32>().ok());
    let salt = parts.next();
    let hash = parts.next();
    let (Some(iterations), Some(salt), Some(hash)) = (iterations, salt, hash) else {
        return false;
    };
    if algorithm != "pbkdf2_sha256"
        || iterations == 0
        || iterations > 10_000_000
        || salt.is_empty()
        || salt.len() > 128
    {
        return false;
    }
    let Ok(expected) = base64::engine::general_purpose::STANDARD.decode(hash) else {
        return false;
    };
    if expected.len() != 32 {
        return false;
    }
    let mut derived = vec![0u8; expected.len()];
    pbkdf2_hmac::<Sha256>(
        password.as_bytes(),
        salt.as_bytes(),
        iterations,
        &mut derived,
    );
    constant_time_eq(&derived, &expected)
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    left.iter()
        .zip(right)
        .fold(0u8, |acc, (a, b)| acc | (a ^ b))
        == 0
}

fn cookie_value(headers: &HeaderMap, name: &str) -> Option<String> {
    headers
        .get(header::COOKIE)
        .and_then(|value| value.to_str().ok())
        .and_then(|cookie| {
            cookie.split(';').find_map(|pair| {
                let (key, value) = pair.trim().split_once('=')?;
                (key == name).then(|| value.trim().to_string())
            })
        })
}

fn header_string(headers: &HeaderMap, name: &str) -> String {
    headers
        .get(name)
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default()
        .chars()
        .take(256)
        .collect()
}

fn session_cookie(session_key: &str, secure: bool) -> String {
    format!(
        "sessionid={session_key}; Path=/; HttpOnly; SameSite=Lax; Max-Age={}{}",
        session_age_seconds(),
        secure_flag(secure)
    )
}

fn csrf_cookie(secret: &str, secure: bool) -> String {
    format!(
        "csrftoken={secret}; Path=/; SameSite=Lax{}",
        secure_flag(secure)
    )
}

fn secure_flag(secure: bool) -> &'static str {
    if secure {
        "; Secure"
    } else {
        ""
    }
}

fn append_cookie(response: &mut Response, cookie: &str) {
    if let Ok(value) = HeaderValue::from_str(cookie) {
        response.headers_mut().append(header::SET_COOKIE, value);
    }
}

fn bad_request(message: &str) -> Response {
    (StatusCode::BAD_REQUEST, Json(json!({ "detail": message }))).into_response()
}

fn csrf_failure(message: &str) -> Response {
    (
        StatusCode::FORBIDDEN,
        Json(json!({ "detail": format!("CSRF Failed: {message}") })),
    )
        .into_response()
}

fn unauthenticated(message: &str) -> Response {
    let mut response =
        (StatusCode::UNAUTHORIZED, Json(json!({ "detail": message }))).into_response();
    response.headers_mut().insert(
        header::WWW_AUTHENTICATE,
        HeaderValue::from_static("Session"),
    );
    response
}

fn unavailable(message: &str) -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({ "detail": message })),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_unsafe_or_malformed_pbkdf2_parameters() {
        let valid_length_hash = base64::engine::general_purpose::STANDARD.encode([0u8; 32]);
        assert!(!verify_password(
            &format!("pbkdf2_sha256$10000001$salt${valid_length_hash}"),
            "password"
        ));
        assert!(!verify_password(
            &format!("pbkdf2_sha256$0$salt${valid_length_hash}"),
            "password"
        ));
        assert!(!verify_password("pbkdf2_sha256$1000$$AA==", "password"));
        assert!(!verify_password("pbkdf2_sha256$1000$salt$AA==", "password"));
    }
}

#[derive(Deserialize)]
struct RegisterPayload {
    identifier: String,
    password: String,
    #[serde(default)]
    display_name: String,
}

/// Native `POST /api/v1/auth/register/`.
pub async fn register(
    State(state): State<AppState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable("The Rust identity pool is not configured.");
    };
    if let Err(response) = enforce_csrf(&headers) {
        return *response;
    }
    if std::env::var("PLATFORM_ALLOW_SELF_REGISTRATION")
        .ok()
        .is_some_and(|value| {
            matches!(
                value.trim().to_ascii_lowercase().as_str(),
                "0" | "false" | "no"
            )
        })
    {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"detail": "Self registration is disabled."})),
        )
            .into_response();
    }
    let payload: RegisterPayload = match serde_json::from_slice(&body) {
        Ok(payload) => payload,
        Err(_) => {
            return bad_request("An email address or phone number and a password are required.")
        }
    };
    if payload.password.len() < 8 {
        return bad_request("Password must be at least 8 characters.");
    }
    if payload.password.len() > 128 {
        return bad_request("Password is too long.");
    }
    let (username, email) = match normalized_identity(&payload.identifier) {
        Ok(value) => value,
        Err(message) => return bad_request(message),
    };
    let display_name = {
        let trimmed = payload.display_name.trim();
        if trimmed.is_empty() {
            username
                .split('@')
                .next()
                .unwrap_or(&username)
                .chars()
                .take(120)
                .collect::<String>()
        } else {
            trimmed.chars().take(120).collect::<String>()
        }
    };
    let exists = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM platform_users WHERE LOWER(username) = LOWER(?)",
    )
    .bind(&username)
    .fetch_one(pool)
    .await
    .unwrap_or(0);
    if exists > 0 {
        return (
            StatusCode::CONFLICT,
            Json(json!({"detail": "An account with this email or phone number already exists."})),
        )
            .into_response();
    }
    let encoded_password = encode_password(&payload.password, 870_000);
    let user = sqlx::query_scalar::<_, i64>(
        "INSERT INTO platform_users (username, email, is_active, is_staff, is_superuser, date_joined) \
         VALUES (?, ?, TRUE, FALSE, FALSE, CURRENT_TIMESTAMP) RETURNING id",
    )
    .bind(&username)
    .bind(&email)
    .fetch_one(pool)
    .await;
    let user_id: i64 = match user {
        Ok(id) => id,
        Err(error) => {
            tracing::error!(%error, "rust auth could not create the user");
            return unavailable("The account could not be created.");
        }
    };
    let credential = sqlx::query(
        "INSERT INTO platform_credentials (user_id, credential_type, credential_hash) \
         VALUES (?, 'password', ?)",
    )
    .bind(user_id)
    .bind(&encoded_password)
    .execute(pool)
    .await;
    if let Err(error) = credential {
        tracing::error!(%error, "rust auth could not store the password credential");
        return unavailable("The account credentials could not be created.");
    }
    let tenant_id = uuid::Uuid::new_v4().simple().to_string();
    let project_id = uuid::Uuid::new_v4().simple().to_string();
    let tenant_slug = unique_tenant_slug(pool, &display_name).await;
    let tenant = sqlx::query(
        "INSERT INTO core_tenant (id, name, slug, created_at, updated_at) \
         VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
    )
    .bind(&tenant_id)
    .bind(
        format!("{}'s workspace", display_name)
            .chars()
            .take(120)
            .collect::<String>(),
    )
    .bind(&tenant_slug)
    .execute(pool)
    .await;
    if let Err(error) = tenant {
        tracing::error!(%error, "rust auth could not create the tenant");
        return unavailable("The workspace could not be created.");
    }
    let membership = sqlx::query(
        "INSERT INTO core_tenantmembership (id, tenant_id, user_id, role, created_at) \
         VALUES (?, ?, ?, 'owner', CURRENT_TIMESTAMP)",
    )
    .bind(uuid::Uuid::new_v4().simple().to_string())
    .bind(&tenant_id)
    .bind(user_id)
    .execute(pool)
    .await;
    if let Err(error) = membership {
        tracing::error!(%error, "rust auth could not create the tenant membership");
        return unavailable("The workspace membership could not be created.");
    }
    let project = sqlx::query(
        "INSERT INTO core_project (id, tenant_id, name, slug, created_at) \
         VALUES (?, ?, 'Default project', 'default', CURRENT_TIMESTAMP)",
    )
    .bind(&project_id)
    .bind(&tenant_id)
    .execute(pool)
    .await;
    if let Err(error) = project {
        tracing::error!(%error, "rust auth could not create the default project");
        return unavailable("The default project could not be created.");
    }
    let project_membership = sqlx::query(
        "INSERT INTO core_projectmembership (id, project_id, user_id, role, created_at) \
         VALUES (?, ?, ?, 'admin', CURRENT_TIMESTAMP)",
    )
    .bind(uuid::Uuid::new_v4().simple().to_string())
    .bind(&project_id)
    .bind(user_id)
    .execute(pool)
    .await;
    if let Err(error) = project_membership {
        tracing::error!(%error, "rust auth could not create the project membership");
        return unavailable("The project membership could not be created.");
    }
    let session_key = new_session_key();
    if let Err(error) = persist_session(&state, pool, user_id, &session_key).await {
        tracing::error!(%error, "rust auth could not persist the session");
        return unavailable("The session could not be created.");
    }
    record_session_activity(
        pool,
        user_id,
        &session_key,
        database_url(&state).starts_with("postgres"),
    )
    .await;
    let request_id = header_string(&headers, "x-request-id");
    let remote = peer.ip().to_string();
    let user_agent = header_string(&headers, "user-agent");
    let kind = if username.contains('@') {
        "email"
    } else {
        "phone"
    };
    let _ = record_auth_event(
        pool,
        database_url(&state),
        AuthAudit {
            event_type: "auth.register.succeeded",
            outcome: "success",
            reason: if kind == "email" {
                "email_self_registration"
            } else {
                "phone_self_registration"
            },
            actor_id: Some(user_id),
            request_id: &request_id,
            remote_addr: Some(&remote),
            user_agent: &user_agent,
        },
    )
    .await;
    let mut response = (
        StatusCode::CREATED,
        Json(json!({
            "id": user_id,
            "username": username,
            "email": email,
            "session_token": session_key,
            "tenant_id": tenant_id,
            "project_id": project_id,
        })),
    )
        .into_response();
    append_cookie(
        &mut response,
        &session_cookie(&session_key, state.config.secure_cookies),
    );
    response
}

fn encode_password(password: &str, iterations: u32) -> String {
    let salt = uuid::Uuid::new_v4().simple().to_string();
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

fn normalized_identity(value: &str) -> Result<(String, String), &'static str> {
    let identity = value.trim();
    if identity.is_empty() {
        return Err("An email address or phone number and a password are required.");
    }
    if identity.contains('@') {
        let email = identity.to_ascii_lowercase();
        let mut parts = email.split('@');
        let local = parts.next().unwrap_or_default();
        let domain = parts.next().unwrap_or_default();
        if local.is_empty()
            || domain.is_empty()
            || !domain.contains('.')
            || email.len() > 254
            || !email
                .chars()
                .all(|ch| ch.is_ascii_alphanumeric() || "@._%+-".contains(ch))
        {
            return Err("Enter a valid email address.");
        }
        return Ok((email.clone(), email));
    }
    let phone: String = identity
        .chars()
        .filter(|ch| !matches!(ch, ' ' | '(' | ')' | '-'))
        .collect();
    if !(6..=15).contains(&phone.len()) || !phone.chars().all(|ch| ch.is_ascii_digit() || ch == '+')
    {
        return Err("Enter a valid phone number with 6 to 15 digits.");
    }
    Ok((phone, String::new()))
}

async fn unique_tenant_slug(pool: &sqlx::AnyPool, name: &str) -> String {
    let mut base: String = name
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() {
                ch.to_ascii_lowercase()
            } else {
                '-'
            }
        })
        .collect();
    while base.contains("--") {
        base = base.replace("--", "-");
    }
    let base = base.trim_matches('-').chars().take(60).collect::<String>();
    let base = if base.is_empty() {
        "workspace".to_string()
    } else {
        base
    };
    let mut candidate = base.clone();
    for _ in 0..20 {
        let exists =
            sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM core_tenant WHERE slug = ?")
                .bind(&candidate)
                .fetch_one(pool)
                .await
                .unwrap_or(0);
        if exists == 0 {
            return candidate.chars().take(80).collect();
        }
        candidate = format!(
            "{}-{}",
            base.chars().take(70).collect::<String>(),
            uuid::Uuid::new_v4()
                .simple()
                .to_string()
                .chars()
                .take(4)
                .collect::<String>()
        );
    }
    candidate.chars().take(80).collect()
}
