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
use crate::identity::{
    decode_session_data, encode_session_data, new_session_key, session_age_seconds,
};
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
/// masked value Django hands to the console; the mask changes on every call,
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
    let Some(secret_key) = state.config.django_secret_key.as_deref() else {
        return unavailable("DJANGO_SECRET_KEY is not configured.");
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
        "SELECT id, username, email, password, \
         CASE WHEN is_active THEN 1 ELSE 0 END \
         FROM auth_user WHERE username = ?",
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
    let session_data = match encode_session_data(user_id, &encoded_password, secret_key) {
        Ok(value) => value,
        Err(error) => {
            tracing::error!(%error, "rust auth could not encode the session");
            return unavailable("The session could not be created.");
        }
    };
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
    if let Err(error) = persist_session(&state, pool, &session_key, &session_data).await {
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
        if let Err(cleanup_error) = sqlx::query("DELETE FROM django_session WHERE session_key = ?")
            .bind(&session_key)
            .execute(pool)
            .await
        {
            tracing::error!(%cleanup_error, "rust auth could not clean up an unaudited login session");
        }
        return unavailable("The login audit event could not be recorded.");
    }
    let _ = sqlx::query("UPDATE auth_user SET last_login = CURRENT_TIMESTAMP WHERE id = ?")
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
        "INSERT INTO core_sessionactivity (session_key, user_id, created_at, last_activity, revoked_at) \
         VALUES (?, ?, {now}, {now}, NULL) \
         ON CONFLICT(session_key) DO UPDATE SET user_id = excluded.user_id, \
         last_activity = excluded.last_activity, revoked_at = NULL"
    );
    let result = sqlx::query(&sql)
        .bind(session_key)
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
        "UPDATE core_sessionactivity SET revoked_at = {now} \
         WHERE session_key = ? AND revoked_at IS NULL"
    );
    let result = sqlx::query(&sql).bind(session_key).execute(pool).await;
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
    let Some(secret_key) = state.config.django_secret_key.as_deref() else {
        return unavailable("DJANGO_SECRET_KEY is not configured.");
    };
    if let Err(response) = enforce_csrf(&headers) {
        return *response;
    }
    let Some(session_key) = crate::identity::session_key_from_headers(&headers) else {
        return unauthenticated("Authentication credentials were not provided.");
    };
    let stored = match sqlx::query_scalar::<_, String>(
        "SELECT session_data FROM django_session \
         WHERE session_key = ? AND expire_date > CURRENT_TIMESTAMP",
    )
    .bind(&session_key)
    .fetch_optional(pool)
    .await
    {
        Ok(Some(stored)) => stored,
        Ok(None) => {
            return unauthenticated("This session has expired. Sign in again.");
        }
        Err(error) => {
            tracing::error!(%error, "rust auth could not read the logout session");
            return unavailable("The session store could not be read.");
        }
    };
    let actor_id = decode_session_data(&stored, secret_key)
        .ok()
        .and_then(|value| {
            value
                .get("_auth_user_id")
                .and_then(|id| id.as_str())
                .and_then(|id| id.parse::<i64>().ok())
        });

    let request_id = header_string(&headers, "x-request-id");
    let remote = peer.ip().to_string();
    let user_agent = header_string(&headers, "user-agent");
    if let Err(error) = sqlx::query("DELETE FROM django_session WHERE session_key = ?")
        .bind(&session_key)
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
    session_key: &str,
    session_data: &str,
) -> Result<(), String> {
    let postgres = database_url(state).starts_with("postgres://")
        || database_url(state).starts_with("postgresql://");
    let sql = if postgres {
        "INSERT INTO django_session (session_key, session_data, expire_date) \
         VALUES (?, ?, NOW() + INTERVAL '1209600 seconds')"
    } else {
        "INSERT INTO django_session (session_key, session_data, expire_date) \
         VALUES (?, ?, datetime('now', '+1209600 seconds'))"
    };
    sqlx::query(sql)
        .bind(session_key)
        .bind(session_data)
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
