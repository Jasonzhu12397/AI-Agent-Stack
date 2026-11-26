#![allow(clippy::result_large_err)]

use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Value};
use sqlx::{AnyPool, Row};

use crate::context::{authenticate, AuthError, SessionUser};
use crate::proxy::AppState;
use crate::tenancy::is_postgres;

pub async fn get_profile(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let profile = match ensure_profile(pool, &state, &user).await {
        Ok(profile) => profile,
        Err(response) => return response,
    };
    Json(profile_payload(&user, &profile)).into_response()
}

pub async fn update_profile(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Result<Json<Value>, axum::extract::rejection::JsonRejection>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let Json(payload) = match body {
        Ok(body) => body,
        Err(_) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": {"code": "ACCOUNT_NO_FIELDS", "message": "No updatable fields supplied.", "details": {}}})),
            )
                .into_response()
        }
    };
    let Some(object) = payload.as_object() else {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": {"code": "ACCOUNT_NO_FIELDS", "message": "No updatable fields supplied.", "details": {}}})),
        )
            .into_response();
    };
    let mut display_name: Option<String> = None;
    let mut email: Option<String> = None;
    let mut timezone: Option<String> = None;
    let mut locale: Option<String> = None;
    for (key, value) in object {
        match key.as_str() {
            "display_name" => {
                display_name = Some(
                    value
                        .as_str()
                        .map(|value| value.chars().take(160).collect())
                        .unwrap_or_else(|| value.to_string().chars().take(160).collect()),
                )
            }
            "email" => email = Some(value.as_str().unwrap_or_default().to_string()),
            "timezone" => {
                let text = value
                    .as_str()
                    .unwrap_or_default()
                    .chars()
                    .take(64)
                    .collect::<String>();
                if text.trim().is_empty() {
                    return invalid_value("timezone", "Timezone must be a non-empty string.");
                }
                timezone = Some(text);
            }
            "locale" => {
                let text = value
                    .as_str()
                    .unwrap_or_default()
                    .chars()
                    .take(16)
                    .collect::<String>();
                if text.trim().is_empty() {
                    return invalid_value("locale", "Locale must be a non-empty string.");
                }
                locale = Some(text);
            }
            _ => {}
        }
    }
    if display_name.is_none() && email.is_none() && timezone.is_none() && locale.is_none() {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": {"code": "ACCOUNT_NO_FIELDS", "message": "No updatable fields supplied.", "details": {}}})),
        )
            .into_response();
    }
    let normalized_email = if let Some(candidate) = email.as_deref() {
        let normalized = candidate.trim().to_lowercase();
        if !normalized.contains('@') {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": {"code": "ACCOUNT_INVALID_EMAIL", "message": "A valid email address is required.", "details": {"email": ["must be a valid email address"]}}})),
            )
                .into_response();
        }
        let taken = sqlx::query_scalar::<_, i64>(
            "SELECT COUNT(*) FROM auth_user WHERE LOWER(email) = ? AND id <> ?",
        )
        .bind(&normalized)
        .bind(user.id)
        .fetch_one(pool)
        .await
        .unwrap_or(0);
        if taken > 0 {
            return (
                StatusCode::CONFLICT,
                Json(json!({"error": {"code": "ACCOUNT_EMAIL_TAKEN", "message": "This email address is already in use.", "details": {"email": ["already taken"]}}})),
            )
                .into_response();
        }
        Some(normalized)
    } else {
        None
    };
    let profile = match ensure_profile(pool, &state, &user).await {
        Ok(profile) => profile,
        Err(response) => return response,
    };
    let mut updates: Vec<String> = Vec::new();
    if let Some(value) = normalized_email.as_deref() {
        if let Err(error) = sqlx::query("UPDATE auth_user SET email = ? WHERE id = ?")
            .bind(value)
            .bind(user.id)
            .execute(pool)
            .await
        {
            return database_error(error.to_string());
        }
        updates.push("email".to_string());
    }
    let now = if is_postgres(&state) {
        "NOW()"
    } else {
        "datetime('now')"
    };
    let mut assignments: Vec<String> = Vec::new();
    let mut values: Vec<Option<String>> = Vec::new();
    if let Some(value) = display_name {
        assignments.push("display_name = ?".to_string());
        values.push(Some(value));
        updates.push("display_name".to_string());
    }
    if let Some(value) = timezone {
        assignments.push("timezone = ?".to_string());
        values.push(Some(value));
        updates.push("timezone".to_string());
    }
    if let Some(value) = locale {
        assignments.push("locale = ?".to_string());
        values.push(Some(value));
        updates.push("locale".to_string());
    }
    if !assignments.is_empty() {
        let sql = format!(
            "UPDATE core_useraccountprofile SET {}, updated_by_id = ?, updated_at = {now} WHERE user_id = ?",
            assignments.join(", ")
        );
        let mut query = sqlx::query(&sql);
        for value in values {
            query = query.bind(value);
        }
        if let Err(error) = query.bind(user.id).bind(user.id).execute(pool).await {
            return database_error(error.to_string());
        }
    }
    if let Some(database_url) = state.config.database_url.as_deref() {
        let event = crate::audit::AuditEvent {
            tenant_id: None,
            project_id: None,
            event_type: "account.profile.updated",
            resource_type: "user_profile",
            resource_id: &profile.id,
            outcome: "success",
            actor_id: Some(user.id),
            request_id: "",
            remote_addr: None,
            user_agent: "",
            metadata: json!({"fields": updates}),
        };
        if let Err(error) = crate::audit::record_event(pool, database_url, event).await {
            tracing::error!(%error, "rust account api could not append the profile audit event");
            return unavailable();
        }
    }
    let email = normalized_email.unwrap_or_else(|| user.email.clone());
    let updated_user = SessionUser { email, ..user };
    match load_profile(pool, user.id).await {
        Ok(Some(updated)) => Json(profile_payload(&updated_user, &updated)).into_response(),
        Ok(None) => unavailable(),
        Err(response) => response,
    }
}

struct Profile {
    id: String,
    display_name: String,
    timezone: String,
    locale: String,
}

fn profile_payload(user: &SessionUser, profile: &Profile) -> Value {
    json!({
        "id": user.id.to_string(),
        "username": user.username,
        "display_name": profile.display_name,
        "email": user.email,
        "timezone": profile.timezone,
        "locale": profile.locale,
    })
}

async fn ensure_profile(
    pool: &AnyPool,
    state: &AppState,
    user: &SessionUser,
) -> Result<Profile, Response> {
    if let Some(profile) = load_profile(pool, user.id).await? {
        return Ok(profile);
    }
    let now = if is_postgres(state) {
        "NOW()"
    } else {
        "datetime('now')"
    };
    let sql = format!(
        "INSERT INTO core_useraccountprofile (id, user_id, display_name, timezone, locale, updated_by_id, updated_at) \
         VALUES (?, ?, '', 'UTC', 'zh-hans', ?, {now})"
    );
    let id = uuid::Uuid::new_v4().to_string();
    if let Err(error) = sqlx::query(&sql)
        .bind(&id)
        .bind(user.id)
        .bind(user.id)
        .execute(pool)
        .await
    {
        return Err(database_error(error.to_string()));
    }
    Ok(Profile {
        id,
        display_name: String::new(),
        timezone: "UTC".to_string(),
        locale: "zh-hans".to_string(),
    })
}

async fn load_profile(pool: &AnyPool, user_id: i64) -> Result<Option<Profile>, Response> {
    let row = sqlx::query(
        "SELECT CAST(id AS TEXT) AS id, display_name, timezone, locale \
         FROM core_useraccountprofile WHERE user_id = ? LIMIT 1",
    )
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| database_error(error.to_string()))?;
    Ok(row.map(|row| Profile {
        id: row.try_get::<String, _>("id").unwrap_or_default(),
        display_name: row.try_get::<String, _>("display_name").unwrap_or_default(),
        timezone: row.try_get::<String, _>("timezone").unwrap_or_default(),
        locale: row.try_get::<String, _>("locale").unwrap_or_default(),
    }))
}

async fn require_user(state: &AppState, headers: &HeaderMap) -> Result<SessionUser, Response> {
    let Some(pool) = state.identity_pool.as_ref() else {
        return Err(unavailable());
    };
    let Some(secret_key) = state.config.django_secret_key.as_deref() else {
        return Err(unavailable());
    };
    match authenticate(pool, secret_key, headers).await {
        Ok(user) => Ok(user),
        Err(AuthError::Unauthenticated(message)) => {
            Err((StatusCode::UNAUTHORIZED, Json(json!({"detail": message}))).into_response())
        }
        Err(AuthError::Unavailable(message)) => {
            tracing::error!(%message, "rust account api could not resolve the session");
            Err(unavailable())
        }
    }
}

fn invalid_value(field: &str, message: &str) -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(json!({"error": {"code": "ACCOUNT_INVALID_VALUE", "message": message, "details": {field: [message]}}})),
    )
        .into_response()
}

fn unavailable() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({"detail": "The service is temporarily unavailable."})),
    )
        .into_response()
}

fn database_error(error: String) -> Response {
    tracing::error!(%error, "rust account api database operation failed");
    unavailable()
}
