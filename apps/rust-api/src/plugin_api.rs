#![allow(clippy::result_large_err)]

use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use chrono::{SecondsFormat, Utc};
use serde_json::{json, Value};
use sqlx::{AnyPool, Row};

use crate::audit::{record_event_in_transaction, AuditEvent};
use crate::context::{authenticate, AuthError};
use crate::proxy::AppState;
use crate::tenancy::{is_postgres, normalize_timestamp};

pub async fn list_plugins(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    if !tenant_member(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return not_found();
    }
    let created = timestamp(&state, "plugin", "created_at");
    let updated = timestamp(&state, "plugin", "updated_at");
    let sql = format!(
        "SELECT CAST(plugin.id AS TEXT) AS id, CAST(plugin.tenant_id AS TEXT) AS tenant_id, \
         plugin.plugin_key, plugin.name, plugin.description, plugin.version, plugin.protocol, \
         plugin.endpoint_url, CAST(plugin.auth_required AS INTEGER) AS auth_required_int, plugin.credential_ref, plugin.capabilities, \
         plugin.manifest, plugin.input_schema, plugin.output_schema, plugin.timeout_seconds, \
         plugin.max_concurrency, CAST(plugin.enabled AS INTEGER) AS enabled_int, \
         {created} AS created, {updated} AS updated FROM core_agentplugindefinition plugin \
         WHERE CAST(plugin.tenant_id AS TEXT) = ? ORDER BY plugin.name, plugin.created_at DESC"
    );
    let rows = match sqlx::query(&sql).bind(&tenant_id).fetch_all(pool).await {
        Ok(rows) => rows,
        Err(error) => return database_error(error.to_string()),
    };
    Json(rows.iter().map(plugin_json).collect::<Vec<Value>>()).into_response()
}

pub async fn list_installations(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id)): Path<(String, String)>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    match crate::tenancy::project_access(pool, &tenant_id, &project_id, user.id).await {
        Ok(true) => {}
        Ok(false) => return not_found(),
        Err(error) => return database_error(error),
    }
    let manifest_text = if is_postgres(&state) {
        "CAST(plugin.manifest AS TEXT)"
    } else {
        "plugin.manifest"
    };
    let capabilities_text = if is_postgres(&state) {
        "CAST(plugin.capabilities AS TEXT)"
    } else {
        "plugin.capabilities"
    };
    let input_text = if is_postgres(&state) {
        "CAST(plugin.input_schema AS TEXT)"
    } else {
        "plugin.input_schema"
    };
    let output_text = if is_postgres(&state) {
        "CAST(plugin.output_schema AS TEXT)"
    } else {
        "plugin.output_schema"
    };
    let granted_text = if is_postgres(&state) {
        "CAST(installation.granted_capabilities AS TEXT)"
    } else {
        "installation.granted_capabilities"
    };
    let created = timestamp(&state, "installation", "created_at");
    let updated = timestamp(&state, "installation", "updated_at");
    let plugin_created = timestamp(&state, "plugin", "created_at");
    let plugin_updated = timestamp(&state, "plugin", "updated_at");
    let sql = format!(
        "SELECT CAST(installation.id AS TEXT) AS id, \
         CAST(installation.tenant_id AS TEXT) AS tenant_id, \
         CAST(installation.project_id AS TEXT) AS project_id, \
         installation.plugin_version, installation.status, {granted_text} AS granted, \
         installation.credential_ref AS installation_credential, \
         {created} AS created, {updated} AS updated, \
         CAST(plugin.id AS TEXT) AS plugin_id, CAST(plugin.tenant_id AS TEXT) AS plugin_tenant_id, \
         plugin.plugin_key, plugin.name, plugin.description, plugin.version, plugin.protocol, \
         plugin.endpoint_url, CAST(plugin.auth_required AS INTEGER) AS auth_required_int, plugin.credential_ref AS plugin_credential, \
         {capabilities_text} AS plugin_capabilities, {manifest_text} AS plugin_manifest, \
         {input_text} AS plugin_input, {output_text} AS plugin_output, plugin.timeout_seconds, \
         plugin.max_concurrency, CAST(plugin.enabled AS INTEGER) AS enabled_int, \
         {plugin_created} AS plugin_created, {plugin_updated} AS plugin_updated \
         FROM core_agentplugininstallation installation \
         JOIN core_agentplugindefinition plugin \
           ON CAST(plugin.id AS TEXT) = CAST(installation.plugin_id AS TEXT) \
         WHERE CAST(installation.tenant_id AS TEXT) = ? \
           AND CAST(installation.project_id AS TEXT) = ? ORDER BY plugin.name"
    );
    let rows = match sqlx::query(&sql)
        .bind(&tenant_id)
        .bind(&project_id)
        .fetch_all(pool)
        .await
    {
        Ok(rows) => rows,
        Err(error) => return database_error(error.to_string()),
    };
    Json(rows.iter().map(installation_json).collect::<Vec<Value>>()).into_response()
}

pub async fn update_installation_status(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(installation_id): Path<String>,
    body: Bytes,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let scope = sqlx::query(
        "SELECT CAST(installation.tenant_id AS TEXT) AS tenant_id, \
         CAST(installation.project_id AS TEXT) AS project_id, installation.status, \
         CAST(plugin.enabled AS INTEGER) AS enabled_int, plugin.plugin_key \
         FROM core_agentplugininstallation installation \
         JOIN core_agentplugindefinition plugin \
           ON CAST(plugin.id AS TEXT) = CAST(installation.plugin_id AS TEXT) \
         WHERE CAST(installation.id AS TEXT) = ?",
    )
    .bind(&installation_id)
    .fetch_optional(pool)
    .await;
    let scope = match scope {
        Ok(Some(scope)) => scope,
        Ok(None) => return not_found(),
        Err(error) => return database_error(error.to_string()),
    };
    let tenant_id: String = scope.try_get("tenant_id").unwrap_or_default();
    let project_id: String = scope.try_get("project_id").unwrap_or_default();
    if !crate::tenancy::project_access(pool, &tenant_id, &project_id, user.id)
        .await
        .unwrap_or(false)
        || !can_edit(pool, &tenant_id, &project_id, user.id)
            .await
            .unwrap_or(false)
    {
        return legacy_error(
            "Project editor permission required.",
            StatusCode::FORBIDDEN,
            "PERMISSION_DENIED",
        );
    }
    let payload = match parse_object(&body) {
        Ok(payload) => payload,
        Err(response) => return response,
    };
    let status = payload.get("status").and_then(Value::as_str).unwrap_or("");
    if !matches!(status, "installed" | "disabled" | "uninstalled") {
        return legacy_error(
            "Unsupported agent plugin installation status.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        );
    }
    if status == "installed" && scope.try_get::<i64, _>("enabled_int").unwrap_or(0) == 0 {
        return legacy_error(
            "Disabled agent plugins cannot be reactivated.",
            StatusCode::CONFLICT,
            "AGENT_PLUGIN_DISABLED",
        );
    }
    let previous: String = scope.try_get("status").unwrap_or_default();
    let postgres = is_postgres(&state);
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return sqlx_error(error),
    };
    if let Err(error) = sqlx::query(
        "UPDATE core_agentplugininstallation SET status = ?, updated_at = ? \
         WHERE CAST(id AS TEXT) = ?",
    )
    .bind(status)
    .bind(&now)
    .bind(&installation_id)
    .execute(&mut *transaction)
    .await
    {
        let _ = transaction.rollback().await;
        return database_error(error.to_string());
    }
    if let Err(error) = record_event_in_transaction(
        &mut transaction,
        postgres,
        &AuditEvent {
            tenant_id: Some(&tenant_id),
            project_id: Some(&project_id),
            event_type: "agent_plugin.installation_status_changed",
            resource_type: "agent_plugin_installation",
            resource_id: &installation_id,
            outcome: "success",
            actor_id: Some(user.id),
            request_id: header(&headers, "x-request-id"),
            remote_addr: None,
            user_agent: header(&headers, "user-agent"),
            metadata: json!({
                "plugin_key": scope.try_get::<String, _>("plugin_key").unwrap_or_default(),
                "previous_status": previous,
                "status": status,
            }),
        },
    )
    .await
    {
        let _ = transaction.rollback().await;
        return database_error(error);
    }
    if let Err(error) = transaction.commit().await {
        return sqlx_error(error);
    }
    let created = timestamp(&state, "installation", "created_at");
    let updated = timestamp(&state, "installation", "updated_at");
    let plugin_created = timestamp(&state, "plugin", "created_at");
    let plugin_updated = timestamp(&state, "plugin", "updated_at");
    let sql = format!(
        "SELECT CAST(installation.id AS TEXT) AS id, \
         CAST(installation.tenant_id AS TEXT) AS tenant_id, \
         CAST(installation.project_id AS TEXT) AS project_id, \
         installation.plugin_version, installation.status, \
         {} AS granted, installation.credential_ref AS installation_credential, \
         {created} AS created, {updated} AS updated, \
         CAST(plugin.id AS TEXT) AS plugin_id, CAST(plugin.tenant_id AS TEXT) AS plugin_tenant_id, \
         plugin.plugin_key, plugin.name, plugin.description, plugin.version, plugin.protocol, \
         plugin.endpoint_url, CAST(plugin.auth_required AS INTEGER) AS auth_required_int, plugin.credential_ref AS plugin_credential, \
         {} AS plugin_capabilities, {} AS plugin_manifest, {} AS plugin_input, \
         {} AS plugin_output, plugin.timeout_seconds, plugin.max_concurrency, \
         CAST(plugin.enabled AS INTEGER) AS enabled_int, {plugin_created} AS plugin_created, \
         {plugin_updated} AS plugin_updated \
         FROM core_agentplugininstallation installation \
         JOIN core_agentplugindefinition plugin \
           ON CAST(plugin.id AS TEXT) = CAST(installation.plugin_id AS TEXT) \
         WHERE CAST(installation.id AS TEXT) = ?",
        json_text(&state, "installation.granted_capabilities"),
        json_text(&state, "plugin.capabilities"),
        json_text(&state, "plugin.manifest"),
        json_text(&state, "plugin.input_schema"),
        json_text(&state, "plugin.output_schema"),
    );
    match sqlx::query(&sql)
        .bind(&installation_id)
        .fetch_one(pool)
        .await
    {
        Ok(row) => Json(installation_json(&row)).into_response(),
        Err(error) => database_error(error.to_string()),
    }
}

fn plugin_json(row: &sqlx::any::AnyRow) -> Value {
    json!({
        "id": row.try_get::<String, _>("id").unwrap_or_default(),
        "tenant_id": row.try_get::<String, _>("tenant_id").unwrap_or_default(),
        "plugin_key": row.try_get::<String, _>("plugin_key").unwrap_or_default(),
        "name": row.try_get::<String, _>("name").unwrap_or_default(),
        "description": row.try_get::<String, _>("description").unwrap_or_default(),
        "version": row.try_get::<String, _>("version").unwrap_or_default(),
        "protocol": row.try_get::<String, _>("protocol").unwrap_or_default(),
        "endpoint_url": row.try_get::<String, _>("endpoint_url").unwrap_or_default(),
        "auth_required": row.try_get::<i64, _>("auth_required_int").unwrap_or(0) != 0,
        "credential_configured": !row.try_get::<String, _>("credential_ref").unwrap_or_default().is_empty(),
        "capabilities": capability_names(row.try_get::<Option<String>, _>("capabilities").ok().flatten(), row.try_get::<Option<String>, _>("manifest").ok().flatten()),
        "capability_definitions": capability_definitions(row.try_get::<Option<String>, _>("capabilities").ok().flatten(), row.try_get::<Option<String>, _>("manifest").ok().flatten(), row.try_get::<Option<String>, _>("input_schema").ok().flatten(), row.try_get::<Option<String>, _>("output_schema").ok().flatten(), row.try_get::<i64, _>("timeout_seconds").unwrap_or(30)),
        "manifest": parse_json(row.try_get::<Option<String>, _>("manifest").ok().flatten()).unwrap_or_else(|| json!({})),
        "input_schema": parse_json(row.try_get::<Option<String>, _>("input_schema").ok().flatten()).unwrap_or_else(|| json!({})),
        "output_schema": parse_json(row.try_get::<Option<String>, _>("output_schema").ok().flatten()).unwrap_or_else(|| json!({})),
        "timeout_seconds": row.try_get::<i64, _>("timeout_seconds").unwrap_or(0),
        "max_concurrency": row.try_get::<i64, _>("max_concurrency").unwrap_or(0),
        "enabled": row.try_get::<i64, _>("enabled_int").unwrap_or(0) != 0,
        "schema_fingerprint": parse_json(row.try_get::<Option<String>, _>("manifest").ok().flatten()).and_then(|value| value.get("studio").and_then(|studio| studio.get("schema_fingerprint")).and_then(Value::as_str).map(str::to_string)).unwrap_or_default(),
        "created_at": row.try_get::<Option<String>, _>("created").ok().flatten().as_deref().map(normalize_timestamp),
        "updated_at": row.try_get::<Option<String>, _>("updated").ok().flatten().as_deref().map(normalize_timestamp),
    })
}

fn installation_json(row: &sqlx::any::AnyRow) -> Value {
    let created: Option<String> = row.try_get("created").ok();
    let updated: Option<String> = row.try_get("updated").ok();
    let plugin_created: Option<String> = row.try_get("plugin_created").ok();
    let plugin_updated: Option<String> = row.try_get("plugin_updated").ok();
    json!({
        "id": row.try_get::<String, _>("id").unwrap_or_default(),
        "tenant_id": row.try_get::<String, _>("tenant_id").unwrap_or_default(),
        "project_id": row.try_get::<String, _>("project_id").unwrap_or_default(),
        "plugin": json!({
            "id": row.try_get::<String, _>("plugin_id").unwrap_or_default(),
            "tenant_id": row.try_get::<String, _>("plugin_tenant_id").unwrap_or_default(),
            "plugin_key": row.try_get::<String, _>("plugin_key").unwrap_or_default(),
            "name": row.try_get::<String, _>("name").unwrap_or_default(),
            "description": row.try_get::<String, _>("description").unwrap_or_default(),
            "version": row.try_get::<String, _>("version").unwrap_or_default(),
            "protocol": row.try_get::<String, _>("protocol").unwrap_or_default(),
            "endpoint_url": row.try_get::<String, _>("endpoint_url").unwrap_or_default(),
            "auth_required": row.try_get::<i64, _>("auth_required_int").unwrap_or(0) != 0,
            "credential_configured": !row.try_get::<String, _>("plugin_credential").unwrap_or_default().is_empty(),
            "capabilities": capability_names(row.try_get::<Option<String>, _>("plugin_capabilities").ok().flatten(), row.try_get::<Option<String>, _>("plugin_manifest").ok().flatten()),
            "capability_definitions": capability_definitions(row.try_get::<Option<String>, _>("plugin_capabilities").ok().flatten(), row.try_get::<Option<String>, _>("plugin_manifest").ok().flatten(), row.try_get::<Option<String>, _>("plugin_input").ok().flatten(), row.try_get::<Option<String>, _>("plugin_output").ok().flatten(), row.try_get::<i64, _>("timeout_seconds").unwrap_or(30)),
            "manifest": parse_json(row.try_get::<Option<String>, _>("plugin_manifest").ok().flatten()).unwrap_or_else(|| json!({})),
            "input_schema": parse_json(row.try_get::<Option<String>, _>("plugin_input").ok().flatten()).unwrap_or_else(|| json!({})),
            "output_schema": parse_json(row.try_get::<Option<String>, _>("plugin_output").ok().flatten()).unwrap_or_else(|| json!({})),
            "timeout_seconds": row.try_get::<i64, _>("timeout_seconds").unwrap_or(0),
            "max_concurrency": row.try_get::<i64, _>("max_concurrency").unwrap_or(0),
            "enabled": row.try_get::<i64, _>("enabled_int").unwrap_or(0) != 0,
            "schema_fingerprint": parse_json(row.try_get::<Option<String>, _>("plugin_manifest").ok().flatten()).and_then(|value| value.get("studio").and_then(|studio| studio.get("schema_fingerprint")).and_then(Value::as_str).map(str::to_string)).unwrap_or_default(),
            "created_at": plugin_created.as_deref().map(normalize_timestamp),
            "updated_at": plugin_updated.as_deref().map(normalize_timestamp),
        }),
        "plugin_version": row.try_get::<String, _>("plugin_version").unwrap_or_default(),
        "status": row.try_get::<String, _>("status").unwrap_or_default(),
        "granted_capabilities": parse_json(row.try_get::<Option<String>, _>("granted").ok().flatten()).unwrap_or_else(|| json!([])),
        "credential_configured": !row.try_get::<String, _>("installation_credential").unwrap_or_default().is_empty(),
        "created_at": created.as_deref().map(normalize_timestamp),
        "updated_at": updated.as_deref().map(normalize_timestamp),
    })
}

fn capability_entries(capabilities: Option<String>, manifest: Option<String>) -> Vec<Value> {
    let manifest = parse_json(manifest).unwrap_or_else(|| json!({}));
    if let Some(entries) = manifest.get("capabilities").and_then(Value::as_array) {
        if !entries.is_empty() {
            return entries
                .iter()
                .filter(|entry| entry.is_object())
                .cloned()
                .collect();
        }
    }
    parse_json(capabilities)
        .and_then(|value| value.as_array().cloned())
        .unwrap_or_default()
}

fn capability_names(capabilities: Option<String>, manifest: Option<String>) -> Vec<String> {
    capability_entries(capabilities, manifest)
        .iter()
        .filter_map(|entry| {
            entry
                .get("name")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .collect()
}

fn capability_definitions(
    capabilities: Option<String>,
    manifest: Option<String>,
    input: Option<String>,
    output: Option<String>,
    timeout: i64,
) -> Vec<Value> {
    let input_schema = parse_json(input).unwrap_or_else(|| json!({}));
    let output_schema = parse_json(output).unwrap_or_else(|| json!({}));
    capability_entries(capabilities, manifest)
        .into_iter()
        .map(|entry| {
            let name = entry.get("name").and_then(Value::as_str).unwrap_or("");
            json!({
                "description": entry.get("description").and_then(Value::as_str).unwrap_or(""),
                "risk_level": entry.get("risk_level").and_then(Value::as_str).unwrap_or("low"),
                "timeout_seconds": entry.get("timeout_seconds").and_then(Value::as_i64).unwrap_or(timeout),
                "input_schema": entry.get("input_schema").cloned().unwrap_or_else(|| input_schema.clone()),
                "output_schema": entry.get("output_schema").cloned().unwrap_or_else(|| output_schema.clone()),
                "name": name,
            })
        })
        .collect()
}

fn json_text(state: &AppState, column: &str) -> String {
    if is_postgres(state) {
        format!("CAST({column} AS TEXT)")
    } else {
        column.to_string()
    }
}

fn parse_json(value: Option<String>) -> Option<Value> {
    value.and_then(|value| serde_json::from_str(&value).ok())
}

async fn tenant_member(pool: &AnyPool, tenant_id: &str, user_id: i64) -> Result<bool, String> {
    let count = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_tenantmembership \
         WHERE CAST(tenant_id AS TEXT) = ? AND user_id = ?",
    )
    .bind(tenant_id)
    .bind(user_id)
    .fetch_one(pool)
    .await
    .map_err(|error| error.to_string())?;
    Ok(count > 0)
}

async fn can_edit(
    pool: &AnyPool,
    tenant_id: &str,
    project_id: &str,
    user_id: i64,
) -> Result<bool, String> {
    if crate::tenancy::tenant_admin(pool, tenant_id, user_id)
        .await
        .unwrap_or(false)
    {
        return Ok(true);
    }
    let role = sqlx::query_scalar::<_, String>(
        "SELECT role FROM core_projectmembership \
         WHERE CAST(project_id AS TEXT) = ? AND user_id = ?",
    )
    .bind(project_id)
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())?;
    Ok(matches!(role.as_deref(), Some("editor" | "admin")))
}

fn timestamp(state: &AppState, alias: &str, column: &str) -> String {
    if is_postgres(state) {
        format!("to_char({alias}.{column} AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')")
    } else {
        format!("strftime('%Y-%m-%dT%H:%M:%fZ', {alias}.{column})")
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
            tracing::error!(%message, "rust plugin api could not resolve the session");
            Err(unavailable())
        }
    }
}

fn parse_object(body: &Bytes) -> Result<Value, Response> {
    match serde_json::from_slice::<Value>(body) {
        Ok(value) if value.is_object() => Ok(value),
        _ => Err(legacy_error(
            "Invalid JSON body.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        )),
    }
}

fn legacy_error(message: &str, status: StatusCode, code: &str) -> Response {
    (
        status,
        Json(json!({"error": {"code": code, "message": message, "details": {}}})),
    )
        .into_response()
}

fn not_found() -> Response {
    (StatusCode::NOT_FOUND, Json(json!({"detail": "Not found."}))).into_response()
}

fn unavailable() -> Response {
    legacy_error(
        "Native identity storage is unavailable.",
        StatusCode::SERVICE_UNAVAILABLE,
        "NATIVE_IDENTITY_UNAVAILABLE",
    )
}

fn database_error(error: String) -> Response {
    legacy_error(
        &format!("Database error: {error}"),
        StatusCode::INTERNAL_SERVER_ERROR,
        "DATABASE_ERROR",
    )
}

fn sqlx_error(error: sqlx::Error) -> Response {
    database_error(error.to_string())
}

fn header<'a>(headers: &'a HeaderMap, name: &str) -> &'a str {
    headers
        .get(name)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
}
