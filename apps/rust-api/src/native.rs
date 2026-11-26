use axum::extract::State;
use axum::http::{header, HeaderMap, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

use crate::identity::{decode_session_data, session_key_from_headers};
use crate::proxy::AppState;

/// Native replacement for `GET /api/v1/auth/me/`.
///
/// The session row stays in the shared `django_session` table and the password
/// hash stays in `auth_user`; this handler only reads both, so Django and Rust
/// accept the same cookie and the same `X-Session-Token` during migration.
pub async fn current_user(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let Some(pool) = state.identity_pool.as_ref() else {
        return service_unavailable("The Rust identity pool is not configured.");
    };
    let Some(secret_key) = state.config.django_secret_key.as_deref() else {
        return service_unavailable("DJANGO_SECRET_KEY is not configured.");
    };
    let Some(session_key) = session_key_from_headers(&headers) else {
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
        Ok(value) => value,
        Err(error) => {
            tracing::error!(%error, "rust identity could not read the session");
            return service_unavailable("The session store could not be read.");
        }
    };
    let Some(stored) = stored else {
        return unauthenticated("This session has expired. Sign in again.");
    };

    let session = match decode_session_data(&stored, secret_key) {
        Ok(value) => value,
        Err(error) => {
            tracing::warn!(%error, "rust identity rejected a session payload");
            return unauthenticated("This session has expired. Sign in again.");
        }
    };
    let Some(user_id) = session
        .get("_auth_user_id")
        .and_then(|value| value.as_str())
        .and_then(|value| value.parse::<i64>().ok())
    else {
        return unauthenticated("This session has expired. Sign in again.");
    };

    let user = sqlx::query_as::<_, (i64, String, String, i64, i64)>(
        "SELECT id, username, email, \
         CASE WHEN is_staff THEN 1 ELSE 0 END, \
         CASE WHEN is_active THEN 1 ELSE 0 END \
         FROM auth_user WHERE id = ?",
    )
    .bind(user_id)
    .fetch_optional(pool)
    .await;
    let (id, username, email, is_staff, is_active) = match user {
        Ok(Some(user)) => user,
        Ok(None) => return unauthenticated("This session has expired. Sign in again."),
        Err(error) => {
            tracing::error!(%error, "rust identity could not read the user");
            return service_unavailable("The user store could not be read.");
        }
    };
    if is_active == 0 {
        return unauthenticated("This session has expired. Sign in again.");
    }

    Json(json!({
        "id": id,
        "username": username,
        "email": email,
        "is_staff": is_staff != 0,
    }))
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

fn service_unavailable(message: &str) -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({ "detail": message })),
    )
        .into_response()
}
