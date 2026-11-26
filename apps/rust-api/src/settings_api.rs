#![allow(clippy::result_large_err)]

//! Native tenant and project settings sections.

use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Map, Value};
use sqlx::{AnyPool, Row};

use crate::context::{authenticate, AuthError};
use crate::proxy::AppState;
use crate::tenancy::is_postgres;

const SECTIONS: [&str; 5] = [
    "general",
    "notifications",
    "work-mode",
    "data-controls",
    "privacy",
];

struct Scope {
    tenant: String,
    project: Option<String>,
}

pub async fn tenant_get(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, section)): Path<(String, String)>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    if !SECTIONS.contains(&section.as_str()) {
        return not_found();
    }
    if let Err(response) = require_tenant_member(pool, &tenant_id, user.id).await {
        return response;
    }
    let scope = Scope {
        tenant: canonical(&tenant_id),
        project: None,
    };
    match read_values(pool, &scope, &section).await {
        Ok(stored) => payload("tenant", &section, stored, None).into_response(),
        Err(response) => response,
    }
}

pub async fn tenant_patch(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, section)): Path<(String, String)>,
    body: Result<Json<Value>, axum::extract::rejection::JsonRejection>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    if !SECTIONS.contains(&section.as_str()) {
        return not_found();
    }
    if let Err(response) = require_tenant_admin(pool, &tenant_id, user.id).await {
        return response;
    }
    let Json(body) = match body {
        Ok(body) => body,
        Err(_) => {
            return invalid(
                "Values must be an object.",
                json!({"values": ["must be an object"]}),
            )
        }
    };
    let values = body.get("values").cloned().unwrap_or(body);
    let Some(object) = values.as_object() else {
        return invalid(
            "Values must be an object.",
            json!({"values": ["must be an object"]}),
        );
    };
    let cleaned = match validate_section(&section, object) {
        Ok(cleaned) => cleaned,
        Err(response) => return response,
    };
    let existing = match read_values(
        pool,
        &Scope {
            tenant: canonical(&tenant_id),
            project: None,
        },
        &section,
    )
    .await
    {
        Ok(values) => values,
        Err(response) => return response,
    };
    let mut merged = existing.as_object().cloned().unwrap_or_default();
    for (key, value) in cleaned.as_object().into_iter().flatten() {
        merged.insert(key.clone(), value.clone());
    }
    let stored = Value::Object(merged);
    let scope = Scope {
        tenant: canonical(&tenant_id),
        project: None,
    };
    match write_values(&state, pool, &scope, &section, &stored, user.id).await {
        Ok(()) => {}
        Err(response) => return response,
    }
    audit(
        &state,
        pool,
        &scope,
        user.id,
        "settings.tenant.updated",
        "tenant_setting",
        &section,
        &cleaned,
    )
    .await;
    match read_row(pool, &scope, &section).await {
        Ok(row) => payload(
            "tenant",
            &section,
            stored,
            row.and_then(|row| row.updated_at),
        )
        .into_response(),
        Err(response) => response,
    }
}

pub async fn project_get(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id, section)): Path<(String, String, String)>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    if !SECTIONS.contains(&section.as_str()) {
        return not_found();
    }
    let scope = Scope {
        tenant: canonical(&tenant_id),
        project: Some(canonical(&project_id)),
    };
    if let Err(response) = require_project_member(pool, &scope, user.id).await {
        return response;
    }
    let tenant_values = match read_values(
        pool,
        &Scope {
            tenant: scope.tenant.clone(),
            project: None,
        },
        &section,
    )
    .await
    {
        Ok(values) => values,
        Err(response) => return response,
    };
    let project_values = match read_values(pool, &scope, &section).await {
        Ok(values) => values,
        Err(response) => return response,
    };
    let defaults = section_defaults(&section);
    let (effective, sources) = fold_project(&defaults, &tenant_values, &project_values);
    let updated_at = read_row(pool, &scope, &section)
        .await
        .ok()
        .flatten()
        .and_then(|row| row.updated_at);
    Json(json!({
        "section": section,
        "scope": "project",
        "values": project_values,
        "effective": effective,
        "defaults": defaults,
        "sources": sources,
        "updated_at": updated_at,
    }))
    .into_response()
}

pub async fn project_patch(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id, section)): Path<(String, String, String)>,
    body: Result<Json<Value>, axum::extract::rejection::JsonRejection>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    if !SECTIONS.contains(&section.as_str()) {
        return not_found();
    }
    let scope = Scope {
        tenant: canonical(&tenant_id),
        project: Some(canonical(&project_id)),
    };
    if let Err(response) = require_project_admin(pool, &scope, user.id).await {
        return response;
    }
    let Json(body) = match body {
        Ok(body) => body,
        Err(_) => {
            return invalid(
                "Values must be an object.",
                json!({"values": ["must be an object"]}),
            )
        }
    };
    let values = body.get("values").cloned().unwrap_or(body);
    let Some(object) = values.as_object() else {
        return invalid(
            "Values must be an object.",
            json!({"values": ["must be an object"]}),
        );
    };
    let cleaned = match validate_section(&section, object) {
        Ok(cleaned) => cleaned,
        Err(response) => return response,
    };
    let existing = match read_values(pool, &scope, &section).await {
        Ok(values) => values,
        Err(response) => return response,
    };
    let mut merged = existing.as_object().cloned().unwrap_or_default();
    for (key, value) in cleaned.as_object().into_iter().flatten() {
        merged.insert(key.clone(), value.clone());
    }
    let stored = Value::Object(merged);
    match write_values(&state, pool, &scope, &section, &stored, user.id).await {
        Ok(()) => {}
        Err(response) => return response,
    }
    audit(
        &state,
        pool,
        &scope,
        user.id,
        "settings.project.updated",
        "project_setting",
        &section,
        &cleaned,
    )
    .await;
    let tenant_values = match read_values(
        pool,
        &Scope {
            tenant: scope.tenant.clone(),
            project: None,
        },
        &section,
    )
    .await
    {
        Ok(values) => values,
        Err(response) => return response,
    };
    let defaults = section_defaults(&section);
    let (effective, sources) = fold_project(&defaults, &tenant_values, &stored);
    let updated_at = read_row(pool, &scope, &section)
        .await
        .ok()
        .flatten()
        .and_then(|row| row.updated_at);
    Json(json!({
        "section": section,
        "scope": "project",
        "values": stored,
        "effective": effective,
        "defaults": defaults,
        "sources": sources,
        "updated_at": updated_at,
    }))
    .into_response()
}

struct SettingRow {
    values: Value,
    updated_at: Option<String>,
}

async fn read_row(
    pool: &AnyPool,
    scope: &Scope,
    section: &str,
) -> Result<Option<SettingRow>, Response> {
    let sql = if scope.project.is_some() {
        "SELECT CAST(\"values\" AS TEXT) AS values_text, CAST(updated_at AS TEXT) AS updated_text \
         FROM core_projectsetting WHERE CAST(project_id AS TEXT) = ? AND section = ? LIMIT 1"
    } else {
        "SELECT CAST(\"values\" AS TEXT) AS values_text, CAST(updated_at AS TEXT) AS updated_text \
         FROM core_tenantsetting WHERE CAST(tenant_id AS TEXT) = ? AND section = ? LIMIT 1"
    };
    let mut query = sqlx::query(sql);
    query = if let Some(project) = &scope.project {
        query.bind(project)
    } else {
        query.bind(&scope.tenant)
    };
    let row = query
        .bind(section)
        .fetch_optional(pool)
        .await
        .map_err(|error| database_error(error.to_string()))?;
    Ok(row.map(|row| SettingRow {
        values: row
            .try_get::<String, _>("values_text")
            .ok()
            .and_then(|text| serde_json::from_str(&text).ok())
            .unwrap_or_else(|| json!({})),
        updated_at: row.try_get::<String, _>("updated_text").ok(),
    }))
}

async fn read_values(pool: &AnyPool, scope: &Scope, section: &str) -> Result<Value, Response> {
    Ok(read_row(pool, scope, section)
        .await?
        .map(|row| row.values)
        .unwrap_or_else(|| json!({})))
}

async fn write_values(
    state: &AppState,
    pool: &AnyPool,
    scope: &Scope,
    section: &str,
    values: &Value,
    user_id: i64,
) -> Result<(), Response> {
    let now = if is_postgres(state) {
        "NOW()"
    } else {
        "datetime('now')"
    };
    let id = uuid::Uuid::new_v4().simple().to_string();
    let text = values.to_string();
    let sql = if scope.project.is_some() {
        if is_postgres(state) {
            format!(
                "INSERT INTO core_projectsetting (id, \"values\", updated_at, tenant_id, project_id, section, updated_by_id) \
                 VALUES (?, ?::jsonb, {now}, ?, ?, ?, ?) \
                 ON CONFLICT (project_id, section) DO UPDATE SET \"values\" = excluded.\"values\", \
                 updated_by_id = excluded.updated_by_id, updated_at = {now}"
            )
        } else {
            format!(
                "INSERT INTO core_projectsetting (id, \"values\", updated_at, tenant_id, project_id, section, updated_by_id) \
                 VALUES (?, ?, {now}, ?, ?, ?, ?) \
                 ON CONFLICT (project_id, section) DO UPDATE SET \"values\" = excluded.\"values\", \
                 updated_by_id = excluded.updated_by_id, updated_at = {now}"
            )
        }
    } else if is_postgres(state) {
        format!(
            "INSERT INTO core_tenantsetting (id, \"values\", updated_at, tenant_id, section, updated_by_id) \
             VALUES (?, ?::jsonb, {now}, ?, ?, ?) \
             ON CONFLICT (tenant_id, section) DO UPDATE SET \"values\" = excluded.\"values\", \
             updated_by_id = excluded.updated_by_id, updated_at = {now}"
        )
    } else {
        format!(
            "INSERT INTO core_tenantsetting (id, \"values\", updated_at, tenant_id, section, updated_by_id) \
             VALUES (?, ?, {now}, ?, ?, ?) \
             ON CONFLICT (tenant_id, section) DO UPDATE SET \"values\" = excluded.\"values\", \
             updated_by_id = excluded.updated_by_id, updated_at = {now}"
        )
    };
    let query = sqlx::query(&sql).bind(&id).bind(&text).bind(&scope.tenant);
    let query = if let Some(project) = &scope.project {
        query.bind(project)
    } else {
        query
    };
    if let Err(error) = query.bind(section).bind(user_id).execute(pool).await {
        return Err(database_error(error.to_string()));
    }
    Ok(())
}

fn payload(scope: &str, section: &str, stored: Value, updated_at: Option<String>) -> Response {
    let defaults = section_defaults(section);
    let (effective, sources) = fold(&defaults, &stored);
    Json(json!({
        "section": section,
        "scope": scope,
        "values": stored,
        "effective": effective,
        "defaults": defaults,
        "sources": sources,
        "updated_at": updated_at,
    }))
    .into_response()
}

fn section_defaults(section: &str) -> Value {
    match section {
        "general" => {
            json!({"default_landing_page": "dashboard", "items_per_page": 20, "compact_mode": false})
        }
        "notifications" => {
            json!({"email_digest": "off", "mention_alerts": true, "system_alerts": true, "quiet_hours_enabled": false})
        }
        "work-mode" => {
            json!({"confirm_destructive": true, "auto_save_draft": true, "execution_timeout_seconds": 300, "run_parallelism": 4})
        }
        "data-controls" => {
            json!({"retention_days": 365, "allow_cloud_sync": false, "export_enabled": true})
        }
        "privacy" => {
            json!({"profile_visible": false, "analytics_opt_out": false, "telemetry_enabled": true})
        }
        _ => json!({}),
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
            sources.insert(key.clone(), json!("tenant"));
        }
    }
    (Value::Object(effective), Value::Object(sources))
}

fn fold_project(defaults: &Value, tenant: &Value, project: &Value) -> (Value, Value) {
    let mut effective = defaults.as_object().cloned().unwrap_or_default();
    let mut sources = effective
        .keys()
        .map(|key| (key.clone(), json!("default")))
        .collect::<Map<String, Value>>();
    for (layer, source) in [(tenant, "tenant"), (project, "project")] {
        if let Some(values) = layer.as_object() {
            for (key, value) in values {
                effective.insert(key.clone(), value.clone());
                sources.insert(key.clone(), json!(source));
            }
        }
    }
    (Value::Object(effective), Value::Object(sources))
}

fn validate_section(section: &str, object: &Map<String, Value>) -> Result<Value, Response> {
    let mut cleaned = Map::new();
    let mut unknown = Map::new();
    let mut invalid = Map::new();
    for (key, value) in object {
        let problem = match (section, key.as_str()) {
            ("general", "default_landing_page") => {
                choice(value, &["dashboard", "agents", "workflows", "fleet"])
            }
            ("general", "items_per_page") => integer(value, 5, 200),
            ("general", "compact_mode") => boolean(value),
            ("notifications", "email_digest") => choice(value, &["off", "daily", "weekly"]),
            ("notifications", "mention_alerts")
            | ("notifications", "system_alerts")
            | ("notifications", "quiet_hours_enabled") => boolean(value),
            ("work-mode", "confirm_destructive") | ("work-mode", "auto_save_draft") => {
                boolean(value)
            }
            ("work-mode", "execution_timeout_seconds") => integer(value, 30, 3600),
            ("work-mode", "run_parallelism") => integer(value, 1, 16),
            ("data-controls", "retention_days") => integer(value, 1, 3650),
            ("data-controls", "allow_cloud_sync") | ("data-controls", "export_enabled") => {
                boolean(value)
            }
            ("privacy", "profile_visible")
            | ("privacy", "analytics_opt_out")
            | ("privacy", "telemetry_enabled") => boolean(value),
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
        return Err(invalid_response(
            "SETTINGS_UNKNOWN_KEY",
            "One or more keys are not recognized.",
            Value::Object(unknown),
        ));
    }
    if !invalid.is_empty() {
        return Err(invalid_response(
            "SETTINGS_INVALID_VALUE",
            "One or more values are invalid.",
            Value::Object(invalid),
        ));
    }
    Ok(Value::Object(cleaned))
}

fn choice(value: &Value, choices: &[&str]) -> Option<String> {
    match value.as_str() {
        Some(text) if choices.contains(&text) => None,
        Some(_) => Some(format!("must be one of: {}", choices.join(", "))),
        None => Some("must be a non-empty string".to_string()),
    }
}

fn integer(value: &Value, min: i64, max: i64) -> Option<String> {
    match value.as_i64() {
        Some(number) if number >= min && number <= max => None,
        Some(_) => Some(format!("must be between {min} and {max}")),
        None => Some("must be an integer".to_string()),
    }
}

fn boolean(value: &Value) -> Option<String> {
    if value.is_boolean() {
        None
    } else {
        Some("must be a boolean".to_string())
    }
}

#[allow(clippy::too_many_arguments)]
async fn audit(
    state: &AppState,
    pool: &AnyPool,
    scope: &Scope,
    actor: i64,
    event_type: &'static str,
    resource_type: &'static str,
    section: &str,
    cleaned: &Value,
) {
    if let Some(database_url) = state.config.database_url.as_deref() {
        let changed: Vec<String> = cleaned
            .as_object()
            .map(|values| values.keys().cloned().collect())
            .unwrap_or_default();
        let event = crate::audit::AuditEvent {
            tenant_id: Some(&scope.tenant),
            project_id: scope.project.as_deref(),
            event_type,
            resource_type,
            resource_id: section,
            outcome: "success",
            actor_id: Some(actor),
            request_id: "",
            remote_addr: None,
            user_agent: "",
            metadata: json!({"section": section, "changed_keys": changed}),
        };
        let _ = crate::audit::record_event(pool, database_url, event).await;
    }
}

async fn require_tenant_member(
    pool: &AnyPool,
    tenant_id: &str,
    user_id: i64,
) -> Result<(), Response> {
    role(pool, tenant_id, user_id).await.map(|_| ())
}

async fn require_tenant_admin(
    pool: &AnyPool,
    tenant_id: &str,
    user_id: i64,
) -> Result<(), Response> {
    match role(pool, tenant_id, user_id).await? {
        role if role == "owner" || role == "admin" => Ok(()),
        _ => Err(forbidden("Tenant administrator permission required.")),
    }
}

async fn role(pool: &AnyPool, tenant_id: &str, user_id: i64) -> Result<String, Response> {
    sqlx::query_scalar::<_, String>(
        "SELECT role FROM core_tenantmembership WHERE CAST(tenant_id AS TEXT) = ? AND user_id = ? LIMIT 1",
    )
    .bind(canonical(tenant_id))
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| database_error(error.to_string()))?
    .ok_or_else(not_found)
}

async fn require_project_member(
    pool: &AnyPool,
    scope: &Scope,
    user_id: i64,
) -> Result<(), Response> {
    let tenant_role = role(pool, &scope.tenant, user_id).await?;
    if tenant_role == "owner" || tenant_role == "admin" {
        return Ok(());
    }
    let exists = sqlx::query_scalar::<_, String>(
        "SELECT role FROM core_projectmembership WHERE CAST(project_id AS TEXT) = ? AND user_id = ? LIMIT 1",
    )
    .bind(scope.project.as_deref().unwrap_or_default())
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| database_error(error.to_string()))?;
    if exists.is_some() {
        Ok(())
    } else {
        Err(not_found())
    }
}

async fn require_project_admin(
    pool: &AnyPool,
    scope: &Scope,
    user_id: i64,
) -> Result<(), Response> {
    if let Ok(tenant_role) = role(pool, &scope.tenant, user_id).await {
        if tenant_role == "owner" || tenant_role == "admin" {
            return Ok(());
        }
    }
    let project_role = sqlx::query_scalar::<_, String>(
        "SELECT role FROM core_projectmembership WHERE CAST(project_id AS TEXT) = ? AND user_id = ? LIMIT 1",
    )
    .bind(scope.project.as_deref().unwrap_or_default())
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| database_error(error.to_string()))?;
    match project_role.as_deref() {
        Some("admin") => Ok(()),
        Some(_) => Err(forbidden("Project administrator permission required.")),
        None => Err(not_found()),
    }
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
        Err(AuthError::Unauthenticated(message)) => {
            Err((StatusCode::UNAUTHORIZED, Json(json!({"detail": message}))).into_response())
        }
        Err(AuthError::Unavailable(message)) => {
            tracing::error!(%message, "rust settings could not resolve the session");
            Err(unavailable())
        }
    }
}

fn canonical(value: &str) -> String {
    value.replace('-', "")
}

fn not_found() -> Response {
    error("NOT_FOUND", "Resource not found.", StatusCode::NOT_FOUND)
}

fn forbidden(message: &str) -> Response {
    error("FORBIDDEN", message, StatusCode::FORBIDDEN)
}

fn unavailable() -> Response {
    error(
        "SERVICE_UNAVAILABLE",
        "The settings store is unavailable.",
        StatusCode::SERVICE_UNAVAILABLE,
    )
}

fn database_error(message: String) -> Response {
    tracing::error!(%message, "rust settings database error");
    error(
        "DATABASE_ERROR",
        "The settings store rejected the request.",
        StatusCode::INTERNAL_SERVER_ERROR,
    )
}

fn invalid(message: &str, details: Value) -> Response {
    invalid_response("SETTINGS_INVALID_VALUE", message, details)
}

fn invalid_response(code: &str, message: &str, details: Value) -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(json!({"error": {"code": code, "message": message, "details": details}})),
    )
        .into_response()
}

fn error(code: &str, message: &str, status: StatusCode) -> Response {
    let body = Json(json!({"error": {"code": code, "message": message, "details": {}}}));
    (status, body).into_response()
}
