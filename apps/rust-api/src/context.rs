use axum::http::HeaderMap;
use sqlx::AnyPool;

use crate::identity::{session_key_from_headers, session_token_hash};

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

pub async fn authenticate(
    pool: &AnyPool,
    headers: &HeaderMap,
) -> Result<SessionUser, AuthError> {
    let Some(session_key) = session_key_from_headers(headers) else {
        return Err(AuthError::Unauthenticated("Authentication credentials were not provided."));
    };
    let token_hash = session_token_hash(&session_key);
    let user = sqlx::query_as::<_, (i64, String, String, i64)>(
        "SELECT u.id, u.username, u.email, CASE WHEN u.is_staff THEN 1 ELSE 0 END \
         FROM rust_sessions s JOIN auth_user u ON u.id = s.user_id \
         WHERE s.token_hash = ? AND s.expires_at > CURRENT_TIMESTAMP \
         AND s.revoked_at IS NULL AND u.is_active = TRUE",
    )
    .bind(token_hash)
    .fetch_optional(pool)
    .await
    .map_err(|error| AuthError::Unavailable(error.to_string()))?;
    let Some((id, username, email, is_staff)) = user else {
        return Err(AuthError::Unauthenticated("This session has expired. Sign in again."));
    };
    Ok(SessionUser {
        id,
        username,
        email,
        is_staff: is_staff != 0,
        session_key,
    })
}
