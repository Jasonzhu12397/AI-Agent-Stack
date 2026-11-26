use axum::extract::State;
use axum::http::{header, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

use crate::context::{authenticate, AuthError};
use crate::proxy::AppState;

/// Native replacement for `GET /api/v1/auth/me/`.
pub async fn current_user(
    State(state): State<AppState>,
    headers: axum::http::HeaderMap,
) -> Response {
    let Some(pool) = state.identity_pool.as_ref() else {
        return service_unavailable("The Rust identity pool is not configured.");
    };
    let Some(secret_key) = state.config.django_secret_key.as_deref() else {
        return service_unavailable("DJANGO_SECRET_KEY is not configured.");
    };
    match authenticate(pool, secret_key, &headers).await {
        Ok(user) => Json(json!({
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "is_staff": user.is_staff,
        }))
        .into_response(),
        Err(AuthError::Unauthenticated(message)) => unauthenticated(message),
        Err(AuthError::Unavailable(error)) => {
            tracing::error!(%error, "rust identity could not resolve the session");
            service_unavailable("The session store could not be read.")
        }
    }
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
