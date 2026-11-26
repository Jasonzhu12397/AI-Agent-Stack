#![allow(clippy::result_large_err)]

//! Native user preference endpoints.
//!
//! Mirrors the Django settings center: defaults are code-owned, unknown keys are
//! rejected, and shortcut combinations are validated per user.

use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Map, Value};
use sqlx::{AnyPool, Row};

use crate::context::{authenticate, AuthError};
use crate::proxy::AppState;
use crate::tenancy::is_postgres;

pub async fn get_preferences(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(namespace): Path<String>,
) -> Response {
    let Some(defaults) = namespace_defaults(&namespace) else {
        return not_found();
    };
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let row = match load_row(pool, user.id, &namespace).await {
        Ok(row) => row,
        Err(response) => return response,
    };
    let stored = row
        .as_ref()
        .map(|row| row.values.clone())
        .unwrap_or_else(|| json!({}));
    let (effective, sources) = fold(&defaults, &stored);
    Json(json!({
        "namespace": namespace,
        "scope": "user",
        "values": stored,
        "effective": effective,
        "defaults": defaults,
        "sources": sources,
        "updated_at": row.and_then(|row| row.updated_at),
    }))
    .into_response()
}

pub async fn update_preferences(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(namespace): Path<String>,
    method: axum::http::Method,
    body: Result<Json<Value>, axum::extract::rejection::JsonRejection>,
) -> Response {
    let replace = method == axum::http::Method::PUT;
    if namespace_defaults(&namespace).is_none() {
        return not_found();
    }
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
            return invalid(
                "Values must be an object.",
                json!({"values": ["must be an object"]}),
            )
        }
    };
    let Some(object) = payload.as_object() else {
        return invalid(
            "Values must be an object.",
            json!({"values": ["must be an object"]}),
        );
    };
    let values = object
        .get("values")
        .cloned()
        .unwrap_or_else(|| payload.clone());
    let existing = match load_row(pool, user.id, &namespace).await {
        Ok(row) => row.map(|row| row.values).unwrap_or_else(|| json!({})),
        Err(response) => return response,
    };
    let candidate = if replace {
        values.clone()
    } else {
        let mut merged = existing.clone();
        if let (Some(base), Some(patch)) = (merged.as_object_mut(), values.as_object()) {
            for (key, value) in patch {
                base.insert(key.clone(), value.clone());
            }
        }
        Value::Object(merged.as_object().cloned().unwrap_or_default())
    };
    let cleaned = match validate(&namespace, &candidate) {
        Ok(cleaned) => cleaned,
        Err((status, body)) => return (status, Json(body)).into_response(),
    };
    let stored = if replace { cleaned.clone() } else { cleaned };
    let values_text = stored.to_string();
    let id = uuid::Uuid::new_v4().simple().to_string();
    let write = if is_postgres(&state) {
        sqlx::query(
            "INSERT INTO core_userpreference (id, user_id, namespace, \"values\", updated_by_id, updated_at) \
             VALUES (?, ?, ?, ?::jsonb, ?, NOW()) \
             ON CONFLICT (user_id, namespace) DO UPDATE SET \"values\" = excluded.\"values\", \
             updated_by_id = excluded.updated_by_id, updated_at = NOW()",
        )
        .bind(&id)
        .bind(user.id)
        .bind(&namespace)
        .bind(&values_text)
        .bind(user.id)
    } else {
        sqlx::query(
            "INSERT INTO core_userpreference (id, user_id, namespace, \"values\", updated_by_id, updated_at) \
             VALUES (?, ?, ?, ?, ?, datetime('now')) \
             ON CONFLICT (user_id, namespace) DO UPDATE SET \"values\" = excluded.\"values\", \
             updated_by_id = excluded.updated_by_id, updated_at = datetime('now')",
        )
        .bind(&id)
        .bind(user.id)
        .bind(&namespace)
        .bind(&values_text)
        .bind(user.id)
    };
    if let Err(error) = write.execute(pool).await {
        return database_error(error.to_string());
    }
    if let Some(database_url) = state.config.database_url.as_deref() {
        let changed: Vec<String> = stored
            .as_object()
            .map(|object| object.keys().cloned().collect())
            .unwrap_or_default();
        let event = crate::audit::AuditEvent {
            tenant_id: None,
            project_id: None,
            event_type: "preference.updated",
            resource_type: "user_preference",
            resource_id: &id,
            outcome: "success",
            actor_id: Some(user.id),
            request_id: "",
            remote_addr: None,
            user_agent: "",
            metadata: json!({"namespace": namespace, "changed_keys": changed}),
        };
        if let Err(error) = crate::audit::record_event(pool, database_url, event).await {
            tracing::error!(%error, "rust preferences could not append the audit event");
            return unavailable();
        }
    }
    let defaults = namespace_defaults(&namespace).unwrap_or_else(|| json!({}));
    let (effective, sources) = fold(&defaults, &stored);
    let updated_at = load_row(pool, user.id, &namespace)
        .await
        .ok()
        .flatten()
        .and_then(|row| row.updated_at);
    Json(json!({
        "namespace": namespace,
        "scope": "user",
        "values": stored,
        "effective": effective,
        "defaults": defaults,
        "sources": sources,
        "updated_at": updated_at,
    }))
    .into_response()
}

struct PreferenceRow {
    values: Value,
    updated_at: Option<String>,
}

async fn load_row(
    pool: &AnyPool,
    user_id: i64,
    namespace: &str,
) -> Result<Option<PreferenceRow>, Response> {
    let row = sqlx::query(
        "SELECT CAST(\"values\" AS TEXT) AS values_text, CAST(updated_at AS TEXT) AS updated_text \
         FROM core_userpreference WHERE user_id = ? AND namespace = ? LIMIT 1",
    )
    .bind(user_id)
    .bind(namespace)
    .fetch_optional(pool)
    .await
    .map_err(|error| database_error(error.to_string()))?;
    Ok(row.map(|row| PreferenceRow {
        values: row
            .try_get::<String, _>("values_text")
            .ok()
            .and_then(|text| serde_json::from_str(&text).ok())
            .unwrap_or_else(|| json!({})),
        updated_at: row.try_get::<String, _>("updated_text").ok(),
    }))
}

fn namespace_defaults(namespace: &str) -> Option<Value> {
    match namespace {
        "appearance" => Some(json!({
            "theme": "system",
            "density": "comfortable",
            "font_size": "medium",
            "reduce_motion": false,
        })),
        "shortcuts" => Some(json!({})),
        "notification-delivery" => Some(json!({
            "in_app": true,
            "email": false,
            "push": false,
            "digest_frequency": "off",
        })),
        _ => None,
    }
}

fn fold(defaults: &Value, stored: &Value) -> (Value, Value) {
    let mut effective = defaults.as_object().cloned().unwrap_or_default();
    let mut sources = effective
        .keys()
        .map(|key| (key.clone(), json!("default")))
        .collect::<Map<String, Value>>();
    if let Some(stored) = stored.as_object() {
        for (key, value) in stored {
            effective.insert(key.clone(), value.clone());
            sources.insert(key.clone(), json!("user"));
        }
    }
    (Value::Object(effective), Value::Object(sources))
}

fn validate(namespace: &str, values: &Value) -> Result<Value, (StatusCode, Value)> {
    let Some(object) = values.as_object() else {
        return Err((
            StatusCode::BAD_REQUEST,
            json!({"error": {"code": "SETTINGS_INVALID_VALUE", "message": "Values must be an object.", "details": {"values": ["must be an object"]}}}),
        ));
    };
    if namespace == "shortcuts" {
        return validate_shortcuts(object);
    }
    let mut cleaned = Map::new();
    let mut invalid = Map::new();
    let mut unknown = Map::new();
    for (key, value) in object {
        let problem = match (namespace, key.as_str()) {
            ("appearance", "theme") => choice(value, &["light", "dark", "system"]),
            ("appearance", "density") => choice(value, &["comfortable", "compact"]),
            ("appearance", "font_size") => choice(value, &["small", "medium", "large"]),
            ("appearance", "reduce_motion") => boolean(value),
            ("notification-delivery", "in_app")
            | ("notification-delivery", "email")
            | ("notification-delivery", "push") => boolean(value),
            ("notification-delivery", "digest_frequency") => {
                choice(value, &["off", "daily", "weekly"])
            }
            _ => {
                unknown.insert(key.clone(), json!(["unknown key"]));
                continue;
            }
        };
        match problem {
            Some(problem) => {
                invalid.insert(key.clone(), json!([problem]));
            }
            None => {
                cleaned.insert(key.clone(), value.clone());
            }
        }
    }
    if !unknown.is_empty() {
        return Err((
            StatusCode::BAD_REQUEST,
            json!({"error": {"code": "SETTINGS_UNKNOWN_KEY", "message": "One or more keys are not recognized.", "details": unknown}}),
        ));
    }
    if !invalid.is_empty() {
        return Err((
            StatusCode::BAD_REQUEST,
            json!({"error": {"code": "SETTINGS_INVALID_VALUE", "message": "One or more values are invalid.", "details": invalid}}),
        ));
    }
    Ok(Value::Object(cleaned))
}

fn validate_shortcuts(object: &Map<String, Value>) -> Result<Value, (StatusCode, Value)> {
    const IDS: &[&str] = &[
        "sidebar.toggle",
        "command.palette",
        "agent.new",
        "workflow.run",
        "settings.open",
    ];
    let mut cleaned = Map::new();
    let mut unknown = Map::new();
    let mut invalid = Map::new();
    for (key, value) in object {
        if !IDS.contains(&key.as_str()) {
            unknown.insert(key.clone(), json!(["unknown shortcut id"]));
            continue;
        }
        match value.as_str() {
            Some(combination) => match combination_problem(combination) {
                Some(problem) => {
                    invalid.insert(key.clone(), json!([problem]));
                }
                None => {
                    cleaned.insert(key.clone(), value.clone());
                }
            },
            None => {
                invalid.insert(
                    key.clone(),
                    json!(["combination must be a non-empty string"]),
                );
            }
        }
    }
    if !unknown.is_empty() {
        return Err((
            StatusCode::BAD_REQUEST,
            json!({"error": {"code": "SETTINGS_UNKNOWN_KEY", "message": "One or more shortcut ids are not recognized.", "details": unknown}}),
        ));
    }
    if !invalid.is_empty() {
        return Err((
            StatusCode::BAD_REQUEST,
            json!({"error": {"code": "SHORTCUT_INVALID_COMBINATION", "message": "One or more shortcut combinations are invalid.", "details": invalid}}),
        ));
    }
    let mut seen = std::collections::HashMap::new();
    let mut conflicts = Map::new();
    for (key, value) in &cleaned {
        let normalized = normalize_combination(value.as_str().unwrap_or_default());
        if let Some(other) = seen.insert(normalized, key.clone()) {
            conflicts.insert(key.clone(), json!([format!("conflicts with {other}")]));
            conflicts.insert(other, json!([format!("conflicts with {key}")]));
        }
    }
    if !conflicts.is_empty() {
        return Err((
            StatusCode::CONFLICT,
            json!({"error": {"code": "SHORTCUT_CONFLICT", "message": "Two shortcuts are bound to the same combination.", "details": conflicts}}),
        ));
    }
    Ok(Value::Object(cleaned))
}

fn choice(value: &Value, choices: &[&str]) -> Option<String> {
    match value.as_str() {
        Some(text) if !text.trim().is_empty() && choices.contains(&text) => None,
        Some(_) => Some(format!("must be one of: {}", choices.join(", "))),
        None => Some("must be a non-empty string".to_string()),
    }
}

fn boolean(value: &Value) -> Option<String> {
    if value.is_boolean() {
        None
    } else {
        Some("must be a boolean".to_string())
    }
}

fn combination_problem(combination: &str) -> Option<String> {
    const MODIFIERS: &[&str] = &["mod", "ctrl", "alt", "shift"];
    let tokens: Vec<&str> = combination
        .split('+')
        .map(str::trim)
        .filter(|token| !token.is_empty())
        .collect();
    if tokens.len() < 2 {
        return Some("combination must include at least one modifier and a key".to_string());
    }
    let (modifiers, key) = tokens.split_at(tokens.len() - 1);
    if modifiers
        .iter()
        .any(|modifier| !MODIFIERS.contains(&modifier.to_lowercase().as_str()))
    {
        return Some("modifiers must be one of: alt, ctrl, mod, shift".to_string());
    }
    let key = key[0];
    if key.is_empty() {
        return Some("combination must include a key".to_string());
    }
    None
}

fn normalize_combination(combination: &str) -> String {
    let tokens: Vec<String> = combination
        .split('+')
        .map(|token| token.trim().to_lowercase())
        .filter(|token| !token.is_empty())
        .collect();
    let (modifiers, key) = tokens.split_at(tokens.len().saturating_sub(1));
    let mut sorted = modifiers.to_vec();
    sorted.sort();
    format!("{}+{}", sorted.join("+"), key.join(""))
}

async fn require_user(
    state: &AppState,
    headers: &HeaderMap,
) -> Result<crate::context::SessionUser, Response> {
    let Some(pool) = state.identity_pool.as_ref() else {
        return Err(unavailable());
    };
    let Some(secret_key) = state.config.django_secret_key.as_deref() else {
        return Err(unavailable());
    };
    match authenticate(pool, secret_key, headers).await {
        Ok(user) => Ok(user),
        Err(AuthError::Unauthenticated(message)) => Err((
            StatusCode::UNAUTHORIZED,
            Json(json!({"error": {"code": "AUTHENTICATION_REQUIRED", "message": message, "details": {}}})),
        )
            .into_response()),
        Err(AuthError::Unavailable(message)) => {
            tracing::error!(%message, "rust preferences could not resolve the session");
            Err(unavailable())
        }
    }
}

fn not_found() -> Response {
    (
        StatusCode::NOT_FOUND,
        Json(json!({"error": {"code": "NOT_FOUND", "message": "Resource not found.", "details": {}}})),
    )
        .into_response()
}

fn invalid(message: &str, details: Value) -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(json!({"error": {"code": "SETTINGS_INVALID_VALUE", "message": message, "details": details}})),
    )
        .into_response()
}

fn unavailable() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({"error": {"code": "SERVICE_UNAVAILABLE", "message": "The settings store is unavailable.", "details": {}}})),
    )
        .into_response()
}

fn database_error(message: String) -> Response {
    tracing::error!(%message, "rust preferences database error");
    (
        StatusCode::INTERNAL_SERVER_ERROR,
        Json(json!({"error": {"code": "DATABASE_ERROR", "message": "The settings store rejected the request.", "details": {}}})),
    )
        .into_response()
}
