use axum::http::HeaderMap;
use sqlx::AnyPool;

use crate::identity::{decode_session_data, session_key_from_headers};

#[derive(Debug, Clone)]
pub struct SessionUser {
    pub id: i64,
    pub username: String,
    pub email: String,
    pub is_staff: bool,
    pub session_key: String,
}

#[derive(Debug)]
pub enum AuthError {
    Unauthenticated(&'static str),
    Unavailable(String),
}

/// Resolve the Django session cookie into an active `auth_user` row.
///
/// Both the edge and Django share one session table, so the Rust API accepts
/// cookies minted by either process until the migration is complete.
pub async fn authenticate(
    pool: &AnyPool,
    secret_key: &str,
    headers: &HeaderMap,
) -> Result<SessionUser, AuthError> {
    let Some(session_key) = session_key_from_headers(headers) else {
        return Err(AuthError::Unauthenticated(
            "Authentication credentials were not provided.",
        ));
    };
    let stored = sqlx::query_scalar::<_, String>(
        "SELECT session_data FROM django_session \
         WHERE session_key = ? AND expire_date > CURRENT_TIMESTAMP",
    )
    .bind(&session_key)
    .fetch_optional(pool)
    .await
    .map_err(|error| AuthError::Unavailable(error.to_string()))?;
    let Some(stored) = stored else {
        return Err(AuthError::Unauthenticated(
            "This session has expired. Sign in again.",
        ));
    };
    let session = decode_session_data(&stored, secret_key)
        .map_err(|_| AuthError::Unauthenticated("This session has expired. Sign in again."))?;
    let Some(user_id) = session
        .get("_auth_user_id")
        .and_then(|value| value.as_str())
        .and_then(|value| value.parse::<i64>().ok())
    else {
        return Err(AuthError::Unauthenticated(
            "This session has expired. Sign in again.",
        ));
    };
    let user = sqlx::query_as::<_, (i64, String, String, i64, i64)>(
        "SELECT id, username, email, \
         CASE WHEN is_staff THEN 1 ELSE 0 END, \
         CASE WHEN is_active THEN 1 ELSE 0 END \
         FROM auth_user WHERE id = ?",
    )
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| AuthError::Unavailable(error.to_string()))?;
    let (id, username, email, is_staff, is_active) = match user {
        Some(user) => user,
        None => {
            return Err(AuthError::Unauthenticated(
                "This session has expired. Sign in again.",
            ))
        }
    };
    if is_active == 0 {
        return Err(AuthError::Unauthenticated(
            "This session has expired. Sign in again.",
        ));
    }
    Ok(SessionUser {
        id,
        username,
        email,
        is_staff: is_staff != 0,
        session_key,
    })
}
