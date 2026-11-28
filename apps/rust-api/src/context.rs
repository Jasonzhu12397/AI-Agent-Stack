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

pub async fn authenticate(pool: &AnyPool, headers: &HeaderMap) -> Result<SessionUser, AuthError> {
    let Some(session_key) = session_key_from_headers(headers) else {
        return Err(AuthError::Unauthenticated(
            "Authentication credentials were not provided.",
        ));
    };
    let token_hash = session_token_hash(&session_key);
    let user = sqlx::query_as::<_, (i64, String, String, i64, Option<String>)>(
        "SELECT u.id, u.username, u.email, CASE WHEN u.is_staff THEN 1 ELSE 0 END, \
         (SELECT CAST(a.last_activity AS TEXT) FROM platform_session_activity a \
          WHERE a.token_hash = s.token_hash LIMIT 1) AS last_activity \
         FROM rust_sessions s JOIN platform_users u ON u.id = s.user_id \
         WHERE s.token_hash = ? AND s.expires_at > CURRENT_TIMESTAMP \
         AND s.revoked_at IS NULL AND u.is_active = TRUE",
    )
    .bind(&token_hash)
    .fetch_optional(pool)
    .await
    .map_err(|error| AuthError::Unavailable(error.to_string()))?;
    let Some((id, username, email, is_staff, last_activity)) = user else {
        return Err(AuthError::Unauthenticated(
            "This session has expired. Sign in again.",
        ));
    };
    touch_activity(pool, &token_hash, last_activity.as_deref()).await;
    Ok(SessionUser {
        id,
        username,
        email,
        is_staff: is_staff != 0,
        session_key,
    })
}

/// How often an authenticated request may move ``last_activity`` forward.
const ACTIVITY_REFRESH_SECONDS: i64 = 60;

/// Refresh the durable session metadata at most once per interval, mirroring
/// the platform's activity middleware: a row read rides along with the session
/// lookup, and only a stale row is written.
async fn touch_activity(pool: &AnyPool, token_hash: &str, last_activity: Option<&str>) {
    let stale = match last_activity {
        Some(value) => match chrono::NaiveDateTime::parse_from_str(value, "%Y-%m-%d %H:%M:%S") {
            Ok(stamp) => {
                let now = chrono::Utc::now().naive_utc();
                (now - stamp).num_seconds() >= ACTIVITY_REFRESH_SECONDS
            }
            // An unparsable stamp is treated as stale so the row self-heals.
            Err(_) => true,
        },
        None => false,
    };
    if !stale {
        return;
    }
    let result = sqlx::query(
        "UPDATE platform_session_activity SET last_activity = CURRENT_TIMESTAMP \
         WHERE token_hash = ? AND revoked_at IS NULL",
    )
    .bind(token_hash)
    .execute(pool)
    .await;
    if let Err(error) = result {
        tracing::debug!(%error, "rust auth skipped session activity refresh");
    }
}
