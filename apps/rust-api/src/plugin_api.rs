#![allow(clippy::result_large_err)]

use axum::body::Bytes;
use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use chrono::{SecondsFormat, Utc};
use serde_json::{json, Value};
use sqlx::{AnyPool, Row};
use std::collections::HashMap;

use crate::audit::{record_event_in_transaction, AuditEvent};
use crate::context::{authenticate, AuthError};
use crate::proxy::AppState;
use crate::tenancy::{is_postgres, normalize_timestamp};

fn error(status: StatusCode, message: &str) -> Response {
    legacy_error(message, status, "VALIDATION_ERROR")
}

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

pub async fn create_plugin(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
    Json(payload): Json<Value>,
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
    if !crate::tenancy::tenant_admin(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return error(
            StatusCode::FORBIDDEN,
            "Tenant administrator permission required.",
        );
    }
    if !payload.is_object() {
        return error(
            StatusCode::BAD_REQUEST,
            "Request body must be a JSON object.",
        );
    }
    let required = ["plugin_key", "name", "version", "protocol", "endpoint_url"];
    for field in required {
        match payload.get(field).and_then(Value::as_str) {
            Some(value) if !value.trim().is_empty() => {}
            _ => {
                return error(
                    StatusCode::BAD_REQUEST,
                    "plugin_key, name, version, protocol and endpoint_url are required strings.",
                )
            }
        }
    }
    let plugin_key = payload["plugin_key"].as_str().unwrap_or("").trim();
    let name = payload["name"].as_str().unwrap_or("").trim();
    let version = payload["version"].as_str().unwrap_or("").trim();
    let protocol = payload["protocol"].as_str().unwrap_or("").trim();
    let endpoint_url = payload["endpoint_url"].as_str().unwrap_or("").trim();
    if name.len() > 120 || version.len() > 40 || plugin_key.len() > 100 {
        return error(
            StatusCode::BAD_REQUEST,
            "Agent plugin identity fields exceed their maximum lengths.",
        );
    }
    let valid_key = plugin_key.len() <= 100
        && !plugin_key.starts_with('-')
        && !plugin_key.ends_with('-')
        && plugin_key
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-');
    if !valid_key {
        return error(
            StatusCode::BAD_REQUEST,
            "plugin_key must be a lowercase hyphenated identifier.",
        );
    }
    let description = payload
        .get("description")
        .and_then(Value::as_str)
        .unwrap_or("");
    if description.len() > 4000 {
        return error(
            StatusCode::BAD_REQUEST,
            "description must be a string no longer than 4,000 characters.",
        );
    }
    if !matches!(
        protocol,
        "http_json" | "mcp" | "a2a" | "openai_compatible" | "open_data"
    ) {
        return error(
            StatusCode::BAD_REQUEST,
            "Unsupported agent plugin protocol.",
        );
    }
    let parsed = match url::Url::parse(endpoint_url) {
        Ok(value) => value,
        Err(_) => {
            return error(
                StatusCode::BAD_REQUEST,
                "endpoint_url must be a valid HTTPS URL.",
            )
        }
    };
    if parsed.scheme() != "https"
        || parsed.host_str().is_none()
        || !parsed.username().is_empty()
        || parsed.password().is_some()
        || parsed.query().is_some()
        || parsed.fragment().is_some()
        || !matches!(parsed.port(), None | Some(443))
    {
        return error(
            StatusCode::BAD_REQUEST,
            "endpoint_url must be a valid HTTPS URL.",
        );
    }
    let manifest = payload
        .get("manifest")
        .cloned()
        .unwrap_or_else(|| json!({}));
    if !manifest.is_object() {
        return error(
            StatusCode::BAD_REQUEST,
            "manifest must be a JSON object no larger than 32 KiB.",
        );
    }
    if serde_json::to_vec(&manifest)
        .map(|bytes| bytes.len())
        .unwrap_or(usize::MAX)
        > 32768
    {
        return error(
            StatusCode::BAD_REQUEST,
            "manifest must be a JSON object no larger than 32 KiB.",
        );
    }
    let capabilities = payload
        .get("capabilities")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if !capabilities.iter().all(|value| {
        value
            .as_str()
            .map(|name| {
                let mut chars = name.chars();
                name.len() <= 64
                    && chars
                        .next()
                        .map(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
                        .unwrap_or(false)
                    && chars.all(|c| {
                        c.is_ascii_lowercase()
                            || c.is_ascii_digit()
                            || matches!(c, '.' | '_' | ':' | '-')
                    })
            })
            .unwrap_or(false)
    }) {
        return error(
            StatusCode::BAD_REQUEST,
            "capabilities must be a list of lowercase capability identifiers.",
        );
    }
    for field in ["input_schema", "output_schema"] {
        if let Some(schema) = payload.get(field) {
            if !schema.is_object() {
                return error(
                    StatusCode::BAD_REQUEST,
                    "input_schema and output_schema must be JSON objects.",
                );
            }
        }
    }
    let auth_required = match payload.get("auth_required") {
        None => true,
        Some(value) => match value.as_bool() {
            Some(value) => value,
            None => return error(StatusCode::BAD_REQUEST, "auth_required must be a boolean."),
        },
    };
    let timeout_seconds = payload
        .get("timeout_seconds")
        .and_then(Value::as_i64)
        .unwrap_or(60);
    if !(1..=300).contains(&timeout_seconds) {
        return error(
            StatusCode::BAD_REQUEST,
            "timeout_seconds must be an integer between 1 and 300.",
        );
    }
    let max_concurrency = payload
        .get("max_concurrency")
        .and_then(Value::as_i64)
        .unwrap_or(4);
    if !(1..=64).contains(&max_concurrency) {
        return error(
            StatusCode::BAD_REQUEST,
            "max_concurrency must be an integer between 1 and 64.",
        );
    }
    let plugin_id = uuid::Uuid::new_v4().simple().to_string();
    let now = if is_postgres(&state) {
        "NOW()"
    } else {
        "CURRENT_TIMESTAMP"
    };
    let insert = format!(
        "INSERT INTO core_agentplugindefinition (id, tenant_id, created_by_id, plugin_key, name, \
         description, version, protocol, endpoint_url, auth_required, credential_ref, capabilities, \
         manifest, input_schema, output_schema, timeout_seconds, max_concurrency, enabled, \
         created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, 1, {now}, {now})"
    );
    let duplicate = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_agentplugindefinition WHERE CAST(tenant_id AS TEXT) = ? \
         AND plugin_key = ? AND version = ?",
    )
    .bind(&tenant_id)
    .bind(plugin_key)
    .bind(version)
    .fetch_one(pool)
    .await
    .unwrap_or(0);
    if duplicate > 0 {
        return error(
            StatusCode::CONFLICT,
            "An agent plugin with this key and version already exists.",
        );
    }
    let input_schema = serde_json::to_string(
        &payload
            .get("input_schema")
            .cloned()
            .unwrap_or_else(|| json!({})),
    )
    .unwrap_or_else(|_| "{}".into());
    let output_schema = serde_json::to_string(
        &payload
            .get("output_schema")
            .cloned()
            .unwrap_or_else(|| json!({})),
    )
    .unwrap_or_else(|_| "{}".into());
    if let Err(error) = sqlx::query(&insert)
        .bind(&plugin_id)
        .bind(&tenant_id)
        .bind(user.id)
        .bind(plugin_key)
        .bind(name)
        .bind(description)
        .bind(version)
        .bind(protocol)
        .bind(endpoint_url)
        .bind(if auth_required { 1 } else { 0 })
        .bind(serde_json::to_string(&capabilities).unwrap_or_else(|_| "[]".into()))
        .bind(serde_json::to_string(&manifest).unwrap_or_else(|_| "{}".into()))
        .bind(&input_schema)
        .bind(&output_schema)
        .bind(timeout_seconds)
        .bind(max_concurrency)
        .execute(pool)
        .await
    {
        return database_error(error.to_string());
    }
    let _ = crate::audit::record_event(
        pool,
        state.config.database_url.as_deref().unwrap_or(""),
        crate::audit::AuditEvent {
            tenant_id: Some(tenant_id.as_str()),
            project_id: None,
            event_type: "agent_plugin.registered",
            resource_type: "agent_plugin_definition",
            resource_id: plugin_id.as_str(),
            outcome: "success",
            actor_id: Some(user.id),
            request_id: "",
            remote_addr: None,
            user_agent: "",
            metadata: json!({
                "plugin_key": plugin_key,
                "version": version,
                "protocol": protocol,
            }),
        },
    )
    .await;
    let created = timestamp(&state, "plugin", "created_at");
    let updated = timestamp(&state, "plugin", "updated_at");
    let sql = format!(
        "SELECT CAST(plugin.id AS TEXT) AS id, CAST(plugin.tenant_id AS TEXT) AS tenant_id, \
         plugin.plugin_key, plugin.name, plugin.description, plugin.version, plugin.protocol, \
         plugin.endpoint_url, CAST(plugin.auth_required AS INTEGER) AS auth_required_int, plugin.credential_ref, plugin.capabilities, \
         plugin.manifest, plugin.input_schema, plugin.output_schema, plugin.timeout_seconds, \
         plugin.max_concurrency, CAST(plugin.enabled AS INTEGER) AS enabled_int, \
         {created} AS created, {updated} AS updated FROM core_agentplugindefinition plugin \
         WHERE CAST(plugin.id AS TEXT) = ? LIMIT 1"
    );
    match sqlx::query(&sql).bind(&plugin_id).fetch_one(pool).await {
        Ok(row) => (StatusCode::CREATED, Json(plugin_json(&row))).into_response(),
        Err(error) => database_error(error.to_string()),
    }
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

pub async fn create_installation(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id)): Path<(String, String)>,
    Json(payload): Json<Value>,
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
    if !can_edit(pool, &tenant_id, &project_id, user.id)
        .await
        .unwrap_or(false)
    {
        return error(StatusCode::FORBIDDEN, "Project editor permission required.");
    }
    if !payload.is_object() {
        return error(
            StatusCode::BAD_REQUEST,
            "Request body must be a JSON object.",
        );
    }
    let plugin_id = payload.get("plugin_id").and_then(Value::as_str);
    let plugin_key = payload.get("plugin_key").and_then(Value::as_str);
    if plugin_id.is_none() && plugin_key.is_none() {
        return error(
            StatusCode::BAD_REQUEST,
            "plugin_id or plugin_key is required.",
        );
    }
    let plugin = match sqlx::query(
        "SELECT CAST(id AS TEXT) AS id, plugin_key, version, CAST(auth_required AS INTEGER) AS auth_required_int, credential_ref, \
         capabilities, manifest FROM core_agentplugindefinition \
         WHERE CAST(tenant_id AS TEXT) = ? AND enabled = 1 AND (id = ? OR (? IS NULL AND plugin_key = ?)) \
         ORDER BY created_at DESC LIMIT 1",
    )
    .bind(&tenant_id)
    .bind(plugin_id.unwrap_or(""))
    .bind(plugin_id)
    .bind(plugin_key.unwrap_or(""))
    .fetch_optional(pool)
    .await
    {
        Ok(Some(row)) => row,
        Ok(None) => return error(StatusCode::NOT_FOUND, "Agent plugin definition does not exist."),
        Err(error) => return database_error(error.to_string()),
    };
    let selected_plugin_id: String = plugin.try_get("id").unwrap_or_default();
    let selected_plugin_key: String = plugin.try_get("plugin_key").unwrap_or_default();
    let plugin_version: String = plugin.try_get("version").unwrap_or_default();
    let auth_required = plugin.try_get::<i64, _>("auth_required_int").unwrap_or(0) != 0;
    let plugin_credential: String = plugin.try_get("credential_ref").unwrap_or_default();
    let declared_capabilities: Vec<String> = plugin
        .try_get::<Option<String>, _>("capabilities")
        .ok()
        .flatten()
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .and_then(|value| value.as_array().cloned())
        .unwrap_or_default()
        .into_iter()
        .filter_map(|value| value.as_str().map(str::to_string))
        .collect();
    let granted: Vec<String> = payload
        .get("granted_capabilities")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
        .into_iter()
        .filter_map(|value| value.as_str().map(str::to_string))
        .collect();
    if !granted
        .iter()
        .all(|capability| declared_capabilities.contains(capability))
    {
        return error(
            StatusCode::BAD_REQUEST,
            "Granted capabilities must be declared by the agent plugin.",
        );
    }
    let mut credential_ref = payload
        .get("credential_ref")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    if credential_ref.is_empty() {
        credential_ref = plugin_credential;
    }
    if auth_required && credential_ref.is_empty() {
        return error(
            StatusCode::BAD_REQUEST,
            "credential_ref is required for this agent plugin; provide a secret reference, not a credential value.",
        );
    }
    if !auth_required && !credential_ref.is_empty() {
        return error(
            StatusCode::BAD_REQUEST,
            "This agent plugin does not accept a credential reference.",
        );
    }
    if !credential_ref.is_empty() {
        if let Some(message) = credential_reference_error(pool, &tenant_id, &credential_ref).await {
            return error(StatusCode::BAD_REQUEST, &message);
        }
    }
    let existing = sqlx::query(
        "SELECT CAST(id AS TEXT) AS id, status FROM core_agentplugininstallation \
         WHERE CAST(tenant_id AS TEXT) = ? AND CAST(project_id AS TEXT) = ? \
         AND CAST(plugin_id AS TEXT) = ? LIMIT 1",
    )
    .bind(&tenant_id)
    .bind(&project_id)
    .bind(&selected_plugin_id)
    .fetch_optional(pool)
    .await;
    let existing = match existing {
        Ok(row) => row,
        Err(error) => return database_error(error.to_string()),
    };
    let mut created = false;
    let mut revived = false;
    let installation_id = if let Some(row) = existing {
        let id: String = row.try_get("id").unwrap_or_default();
        let status: String = row.try_get("status").unwrap_or_default();
        if status == "uninstalled" {
            let now = if is_postgres(&state) {
                "NOW()"
            } else {
                "CURRENT_TIMESTAMP"
            };
            let update = format!(
                "UPDATE core_agentplugininstallation SET status = 'installed', plugin_version = ?, \
                 granted_capabilities = ?, credential_ref = ?, installed_by_id = ?, \
                 updated_at = {now} WHERE CAST(id AS TEXT) = ?"
            );
            if let Err(error) = sqlx::query(&update)
                .bind(&plugin_version)
                .bind(serde_json::to_string(&granted).unwrap_or_else(|_| "[]".into()))
                .bind(&credential_ref)
                .bind(user.id)
                .bind(&id)
                .execute(pool)
                .await
            {
                return database_error(error.to_string());
            }
            revived = true;
        }
        id
    } else {
        created = true;
        let id = uuid::Uuid::new_v4().simple().to_string();
        let now = if is_postgres(&state) {
            "NOW()"
        } else {
            "CURRENT_TIMESTAMP"
        };
        let insert = format!(
            "INSERT INTO core_agentplugininstallation (id, tenant_id, project_id, plugin_id, \
             plugin_version, status, granted_capabilities, credential_ref, installed_by_id, \
             revision, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'installed', ?, ?, ?, 0, {now}, {now})"
        );
        if let Err(error) = sqlx::query(&insert)
            .bind(&id)
            .bind(&tenant_id)
            .bind(&project_id)
            .bind(&selected_plugin_id)
            .bind(&plugin_version)
            .bind(serde_json::to_string(&granted).unwrap_or_else(|_| "[]".into()))
            .bind(&credential_ref)
            .bind(user.id)
            .execute(pool)
            .await
        {
            return database_error(error.to_string());
        }
        id
    };
    if created || revived {
        let _ = crate::audit::record_event(
            pool,
            state.config.database_url.as_deref().unwrap_or(""),
            crate::audit::AuditEvent {
                tenant_id: Some(tenant_id.as_str()),
                project_id: Some(project_id.as_str()),
                event_type: "agent_plugin.installed",
                resource_type: "agent_plugin_installation",
                resource_id: installation_id.as_str(),
                outcome: "success",
                actor_id: Some(user.id),
                request_id: "",
                remote_addr: None,
                user_agent: "",
                metadata: json!({
                    "plugin_key": selected_plugin_key,
                    "granted_capabilities": granted,
                }),
            },
        )
        .await;
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
    let sql = format!(
        "SELECT CAST(installation.id AS TEXT) AS id, \
         CAST(installation.tenant_id AS TEXT) AS tenant_id, \
         CAST(installation.project_id AS TEXT) AS project_id, \
         installation.plugin_version, installation.status, {granted_text} AS granted, \
         installation.credential_ref AS installation_credential, \
         {} AS created, {} AS updated, \
         CAST(plugin.id AS TEXT) AS plugin_id, CAST(plugin.tenant_id AS TEXT) AS plugin_tenant_id, \
         plugin.plugin_key, plugin.name, plugin.description, plugin.version, plugin.protocol, \
         plugin.endpoint_url, CAST(plugin.auth_required AS INTEGER) AS auth_required_int, plugin.credential_ref AS plugin_credential, \
         {capabilities_text} AS plugin_capabilities, {manifest_text} AS plugin_manifest, \
         {input_text} AS plugin_input, {output_text} AS plugin_output, plugin.timeout_seconds, \
         plugin.max_concurrency, CAST(plugin.enabled AS INTEGER) AS enabled_int, \
         {} AS plugin_created, {} AS plugin_updated \
         FROM core_agentplugininstallation installation \
         JOIN core_agentplugindefinition plugin ON CAST(plugin.id AS TEXT) = CAST(installation.plugin_id AS TEXT) \
         WHERE CAST(installation.id AS TEXT) = ? LIMIT 1",
        timestamp(&state, "installation", "created_at"),
        timestamp(&state, "installation", "updated_at"),
        timestamp(&state, "plugin", "created_at"),
        timestamp(&state, "plugin", "updated_at"),
    );
    match sqlx::query(&sql)
        .bind(&installation_id)
        .fetch_one(pool)
        .await
    {
        Ok(row) => (
            if created {
                StatusCode::CREATED
            } else {
                StatusCode::OK
            },
            Json(installation_json(&row)),
        )
            .into_response(),
        Err(error) => database_error(error.to_string()),
    }
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

pub(crate) fn capability_definitions(
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

async fn credential_reference_error(
    pool: &AnyPool,
    tenant_id: &str,
    reference: &str,
) -> Option<String> {
    if reference.len() > 200 {
        return Some("credential_ref must be at most 200 characters.".into());
    }
    let (scheme, rest) = match reference.split_once("://") {
        Some(value) => value,
        None => return Some("Connector credential reference is invalid.".into()),
    };
    if rest.is_empty()
        || scheme.is_empty()
        || !scheme
            .chars()
            .next()
            .is_some_and(|first| first.is_ascii_lowercase())
        || !scheme.chars().all(|value| {
            value.is_ascii_lowercase() || value.is_ascii_digit() || matches!(value, '+' | '.' | '-')
        })
        || reference.chars().any(char::is_whitespace)
    {
        return Some("Connector credential reference is invalid.".into());
    }
    if !matches!(scheme, "env" | "vault" | "plugin") {
        return Some(format!(
            "No credential provider is registered for scheme '{scheme}'."
        ));
    }
    if scheme == "plugin" {
        let owned = sqlx::query_scalar::<_, i64>(
            "SELECT COUNT(*) FROM core_agentplugincredential \
             WHERE CAST(id AS TEXT) = ? AND CAST(tenant_id AS TEXT) = ?",
        )
        .bind(rest)
        .bind(tenant_id)
        .fetch_one(pool)
        .await
        .unwrap_or(0);
        if owned == 0 {
            return Some("Plugin credential is not available in this workspace.".into());
        }
    }
    None
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
    match authenticate(pool, headers).await {
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

pub async fn marketplace_catalog(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
    Query(query): Query<HashMap<String, String>>,
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
    let project_id = query
        .get("project_id")
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty());
    if let Some(project_id) = project_id.as_deref() {
        match crate::tenancy::project_access(pool, &tenant_id, project_id, user.id).await {
            Ok(true) => {}
            Ok(false) => return not_found(),
            Err(error) => return database_error(error),
        }
    }
    let filters = match catalog_filters(&query) {
        Ok(filters) => filters,
        Err(response) => return response,
    };
    let plugins = match latest_plugins(pool, &state, &tenant_id).await {
        Ok(plugins) => plugins,
        Err(error) => return database_error(error),
    };
    let installations = match installation_map(pool, &tenant_id, project_id.as_deref()).await {
        Ok(installations) => installations,
        Err(error) => return database_error(error),
    };
    let mut entries = Vec::new();
    for plugin in plugins {
        let entry = catalog_entry(
            &plugin,
            project_id.as_deref(),
            installations.get(&plugin.id),
        );
        if let Some(protocol) = filters.protocol.as_deref() {
            if plugin.protocol != protocol {
                continue;
            }
        }
        if let Some(installed) = filters.installed {
            if entry["installed"].as_bool().unwrap_or(false) != installed {
                continue;
            }
        }
        if let Some(capability) = filters.capability.as_deref() {
            let needle = capability.to_lowercase();
            let matches_capability = entry["capabilities"]
                .as_array()
                .map(|items| {
                    items.iter().any(|item| {
                        item["name"]
                            .as_str()
                            .map(|name| name.to_lowercase().contains(&needle))
                            .unwrap_or(false)
                    })
                })
                .unwrap_or(false);
            if !matches_capability
                && !plugin.name.to_lowercase().contains(&needle)
                && !plugin.description.to_lowercase().contains(&needle)
            {
                continue;
            }
        }
        entries.push(entry);
    }
    let count = entries.len();
    Json(json!({"entries": entries, "count": count})).into_response()
}

pub async fn marketplace_catalog_detail(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, plugin_key)): Path<(String, String)>,
    Query(query): Query<HashMap<String, String>>,
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
    let project_id = query
        .get("project_id")
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty());
    if let Some(project_id) = project_id.as_deref() {
        match crate::tenancy::project_access(pool, &tenant_id, project_id, user.id).await {
            Ok(true) => {}
            Ok(false) => return not_found(),
            Err(error) => return database_error(error),
        }
    }
    let plugins = match latest_plugins(pool, &state, &tenant_id).await {
        Ok(plugins) => plugins,
        Err(error) => return database_error(error),
    };
    let Some(plugin) = plugins
        .into_iter()
        .find(|plugin| plugin.plugin_key == plugin_key)
    else {
        return not_found();
    };
    let installations = match installation_map(pool, &tenant_id, project_id.as_deref()).await {
        Ok(installations) => installations,
        Err(error) => return database_error(error),
    };
    let mut entry = catalog_entry(
        &plugin,
        project_id.as_deref(),
        installations.get(&plugin.id),
    );
    entry["capabilities"] = json!(capability_detail(&plugin));
    entry["manifest"] = plugin.manifest.clone();
    entry["input_schema"] = plugin.input_schema.clone();
    entry["output_schema"] = plugin.output_schema.clone();
    Json(entry).into_response()
}

struct CatalogFilters {
    capability: Option<String>,
    protocol: Option<String>,
    installed: Option<bool>,
}

fn catalog_filters(query: &HashMap<String, String>) -> Result<CatalogFilters, Response> {
    let capability = query
        .get("capability")
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty());
    let protocol = query
        .get("protocol")
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty());
    if let Some(protocol) = protocol.as_deref() {
        if !["http_json", "mcp", "a2a", "openai_compatible", "open_data"].contains(&protocol) {
            return Err((
                StatusCode::BAD_REQUEST,
                Json(json!({"detail": "Unsupported protocol filter."})),
            )
                .into_response());
        }
    }
    let installed = match query.get("installed") {
        Some(value) if value.trim().eq_ignore_ascii_case("true") => Some(true),
        Some(value) if value.trim().eq_ignore_ascii_case("false") => Some(false),
        Some(_) => {
            return Err((
                StatusCode::BAD_REQUEST,
                Json(json!({"detail": "installed filter must be true or false."})),
            )
                .into_response())
        }
        None => None,
    };
    Ok(CatalogFilters {
        capability,
        protocol,
        installed,
    })
}

struct CatalogPlugin {
    id: String,
    plugin_key: String,
    name: String,
    description: String,
    version: String,
    protocol: String,
    enabled: bool,
    auth_required: bool,
    capabilities: Vec<Value>,
    manifest: Value,
    input_schema: Value,
    output_schema: Value,
}

async fn latest_plugins(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: &str,
) -> Result<Vec<CatalogPlugin>, String> {
    let capabilities_text = json_column(state, "plugin.capabilities");
    let manifest_text = json_column(state, "plugin.manifest");
    let input_text = json_column(state, "plugin.input_schema");
    let output_text = json_column(state, "plugin.output_schema");
    let sql = format!(
        "SELECT CAST(plugin.id AS TEXT) AS id, plugin.plugin_key, plugin.name, plugin.description, \
         plugin.version, plugin.protocol, CAST(plugin.enabled AS INTEGER) AS enabled_int, \
         CAST(plugin.auth_required AS INTEGER) AS auth_int, {capabilities_text} AS capabilities_text, \
         {manifest_text} AS manifest_text, {input_text} AS input_text, {output_text} AS output_text \
         FROM core_agentplugindefinition plugin WHERE CAST(plugin.tenant_id AS TEXT) = ? \
         ORDER BY plugin.created_at DESC"
    );
    let rows = sqlx::query(&sql)
        .bind(tenant_id)
        .fetch_all(pool)
        .await
        .map_err(|error| error.to_string())?;
    let mut latest: HashMap<String, CatalogPlugin> = HashMap::new();
    for row in rows {
        let plugin = CatalogPlugin {
            id: row.try_get("id").unwrap_or_default(),
            plugin_key: row.try_get("plugin_key").unwrap_or_default(),
            name: row.try_get("name").unwrap_or_default(),
            description: row.try_get("description").unwrap_or_default(),
            version: row.try_get("version").unwrap_or_default(),
            protocol: row.try_get("protocol").unwrap_or_default(),
            enabled: row.try_get::<i64, _>("enabled_int").unwrap_or(0) != 0,
            auth_required: row.try_get::<i64, _>("auth_int").unwrap_or(0) != 0,
            capabilities: parse_json(
                row.try_get::<Option<String>, _>("capabilities_text")
                    .ok()
                    .flatten(),
            )
            .and_then(|value| value.as_array().cloned())
            .unwrap_or_default(),
            manifest: parse_json(
                row.try_get::<Option<String>, _>("manifest_text")
                    .ok()
                    .flatten(),
            )
            .unwrap_or_else(|| json!({})),
            input_schema: parse_json(
                row.try_get::<Option<String>, _>("input_text")
                    .ok()
                    .flatten(),
            )
            .unwrap_or_else(|| json!({"type": "object", "properties": {}})),
            output_schema: parse_json(
                row.try_get::<Option<String>, _>("output_text")
                    .ok()
                    .flatten(),
            )
            .unwrap_or_else(|| json!({"type": "object", "properties": {}})),
        };
        let replace = latest
            .get(&plugin.plugin_key)
            .map(|current| version_key(&plugin.version) > version_key(&current.version))
            .unwrap_or(true);
        if replace {
            latest.insert(plugin.plugin_key.clone(), plugin);
        }
    }
    let mut plugins: Vec<CatalogPlugin> = latest.into_values().collect();
    plugins.sort_by(|left, right| {
        left.name
            .to_lowercase()
            .cmp(&right.name.to_lowercase())
            .then(left.plugin_key.cmp(&right.plugin_key))
    });
    Ok(plugins)
}

fn version_key(version: &str) -> Vec<(u8, u64, String)> {
    version
        .split('.')
        .map(|segment| {
            if let Ok(number) = segment.parse::<u64>() {
                (0, number, String::new())
            } else {
                (1, 0, segment.to_string())
            }
        })
        .collect()
}

async fn installation_map(
    pool: &AnyPool,
    tenant_id: &str,
    project_id: Option<&str>,
) -> Result<HashMap<String, (String, String)>, String> {
    let Some(project_id) = project_id else {
        return Ok(HashMap::new());
    };
    let rows = sqlx::query(
        "SELECT CAST(installation.id AS TEXT) AS id, CAST(installation.plugin_id AS TEXT) AS plugin_id, \
         installation.status FROM core_agentplugininstallation installation \
         WHERE CAST(installation.tenant_id AS TEXT) = ? AND CAST(installation.project_id AS TEXT) = ?",
    )
    .bind(tenant_id)
    .bind(project_id)
    .fetch_all(pool)
    .await
    .map_err(|error| error.to_string())?;
    Ok(rows
        .iter()
        .map(|row| {
            (
                row.try_get::<String, _>("plugin_id").unwrap_or_default(),
                (
                    row.try_get::<String, _>("id").unwrap_or_default(),
                    row.try_get::<String, _>("status").unwrap_or_default(),
                ),
            )
        })
        .collect())
}

fn catalog_entry(
    plugin: &CatalogPlugin,
    project_id: Option<&str>,
    installation: Option<&(String, String)>,
) -> Value {
    let (status, installation_id) = match installation {
        Some((id, status)) => (status.clone(), Some(id.clone())),
        None => ("not_installed".to_string(), None),
    };
    let installed = status != "uninstalled" && status != "not_installed";
    json!({
        "plugin_key": plugin.plugin_key,
        "name": plugin.name,
        "description": plugin.description,
        "version": plugin.version,
        "protocol": plugin.protocol,
        "enabled": plugin.enabled,
        "auth_required": plugin.auth_required,
        "capabilities": capability_brief(plugin),
        "installed": installed,
        "installation_status": status,
        "installation_id": installation_id,
        "project_id": project_id,
    })
}

fn declared_capabilities(plugin: &CatalogPlugin) -> Vec<Value> {
    if let Some(entries) = plugin
        .manifest
        .get("capabilities")
        .and_then(Value::as_array)
    {
        if !entries.is_empty() {
            return entries
                .iter()
                .filter(|entry| entry.is_object())
                .cloned()
                .collect();
        }
    }
    let mut result = Vec::new();
    for entry in &plugin.capabilities {
        if let Some(name) = entry.get("name").and_then(Value::as_str) {
            let mut declaration = json!({
                "name": name,
                "description": "",
                "risk_level": "low",
            });
            if let Some(object) = entry.as_object() {
                for (key, value) in object {
                    declaration[key] = value.clone();
                }
            }
            result.push(declaration);
        }
    }
    result
}

fn capability_brief(plugin: &CatalogPlugin) -> Vec<Value> {
    declared_capabilities(plugin)
        .into_iter()
        .filter_map(|declaration| {
            let name = declaration.get("name").and_then(Value::as_str)?;
            Some(json!({
                "name": name,
                "description": declaration.get("description").and_then(Value::as_str).unwrap_or(""),
                "risk_level": declaration.get("risk_level").and_then(Value::as_str).unwrap_or("low"),
            }))
        })
        .collect()
}

fn capability_detail(plugin: &CatalogPlugin) -> Vec<Value> {
    declared_capabilities(plugin)
        .into_iter()
        .filter_map(|declaration| {
            let name = declaration.get("name").and_then(Value::as_str)?;
            Some(json!({
                "name": name,
                "description": declaration.get("description").and_then(Value::as_str).unwrap_or(""),
                "risk_level": declaration.get("risk_level").and_then(Value::as_str).unwrap_or("low"),
                "timeout_seconds": declaration.get("timeout_seconds").and_then(Value::as_i64).unwrap_or(30),
                "input_schema": declaration.get("input_schema").cloned().unwrap_or_else(|| json!({})),
                "output_schema": declaration.get("output_schema").cloned().unwrap_or_else(|| json!({})),
            }))
        })
        .collect()
}

fn json_column(state: &AppState, column: &str) -> String {
    if is_postgres(state) {
        format!("CAST({column} AS TEXT)")
    } else {
        column.to_string()
    }
}

/// Native replacement for `GET|POST /api/v1/agent-plugins/{plugin}/`.
pub async fn plugin_detail(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(plugin_id): Path<String>,
    method: axum::http::Method,
    payload: Option<Json<Value>>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let tenant_id: String = match sqlx::query_scalar(
        "SELECT CAST(tenant_id AS TEXT) FROM core_agentplugindefinition \
         WHERE CAST(id AS TEXT) = ? LIMIT 1",
    )
    .bind(&plugin_id)
    .fetch_optional(pool)
    .await
    {
        Ok(Some(value)) => value,
        Ok(None) => return not_found(),
        Err(error) => return database_error(error.to_string()),
    };
    if !tenant_member(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return not_found();
    }
    if method == axum::http::Method::POST {
        match crate::billing_api::tenant_admin(pool, &tenant_id, user.id).await {
            Ok(true) => {}
            Ok(false) => {
                return (
                    StatusCode::FORBIDDEN,
                    Json(json!({"detail": "Tenant administrator permission required."})),
                )
                    .into_response()
            }
            Err(error) => return database_error(error),
        }
        let body = payload
            .map(|Json(value)| value)
            .unwrap_or_else(|| json!({}));
        let Some(enabled) = body.get("enabled").and_then(Value::as_bool) else {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"detail": "enabled must be a boolean."})),
            )
                .into_response();
        };
        let now = Utc::now().format("%Y-%m-%d %H:%M:%S").to_string();
        let now_expr = if is_postgres(&state) {
            "NOW()".to_string()
        } else {
            format!("'{now}'")
        };
        let sql = format!(
            "UPDATE core_agentplugindefinition SET enabled = ?, updated_at = {now_expr} \
             WHERE CAST(id AS TEXT) = ?"
        );
        if let Err(error) = sqlx::query(&sql)
            .bind(i64::from(enabled))
            .bind(&plugin_id)
            .execute(pool)
            .await
        {
            return database_error(error.to_string());
        }
        let plugin_key: String = sqlx::query_scalar(
            "SELECT plugin_key FROM core_agentplugindefinition WHERE CAST(id AS TEXT) = ? LIMIT 1",
        )
        .bind(&plugin_id)
        .fetch_one(pool)
        .await
        .unwrap_or_default();
        let _ = crate::audit::record_event(
            pool,
            state.config.database_url.as_deref().unwrap_or(""),
            crate::audit::AuditEvent {
                tenant_id: Some(tenant_id.as_str()),
                project_id: None,
                event_type: "agent_plugin.enabled_changed",
                resource_type: "agent_plugin_definition",
                resource_id: plugin_id.as_str(),
                outcome: "success",
                actor_id: Some(user.id),
                request_id: "",
                remote_addr: None,
                user_agent: "",
                metadata: json!({"plugin_key": plugin_key, "enabled": enabled}),
            },
        )
        .await;
    }
    let created = timestamp(&state, "plugin", "created_at");
    let updated = timestamp(&state, "plugin", "updated_at");
    let sql = format!(
        "SELECT CAST(plugin.id AS TEXT) AS id, CAST(plugin.tenant_id AS TEXT) AS tenant_id, \
         plugin.plugin_key, plugin.name, plugin.description, plugin.version, plugin.protocol, \
         plugin.endpoint_url, CAST(plugin.auth_required AS INTEGER) AS auth_required_int, \
         plugin.credential_ref, plugin.capabilities, plugin.manifest, plugin.input_schema, \
         plugin.output_schema, plugin.timeout_seconds, plugin.max_concurrency, \
         CAST(plugin.enabled AS INTEGER) AS enabled_int, {created} AS created, {updated} AS updated \
         FROM core_agentplugindefinition plugin WHERE CAST(plugin.id AS TEXT) = ? LIMIT 1"
    );
    match sqlx::query(&sql)
        .bind(&plugin_id)
        .fetch_optional(pool)
        .await
    {
        Ok(Some(row)) => Json(plugin_json(&row)).into_response(),
        Ok(None) => not_found(),
        Err(error) => database_error(error.to_string()),
    }
}

/// Native replacement for `GET /api/v1/tenants/{tenant}/projects/{project}/agent-capabilities/`.
pub async fn list_capabilities(
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
    let sql = format!(
        "SELECT CAST(installation.id AS TEXT) AS installation_id, installation.plugin_version, \
         installation.status AS installation_status, plugin.plugin_key, plugin.protocol, \
         CAST(plugin.enabled AS INTEGER) AS enabled_int, plugin.timeout_seconds, \
         {manifest_text} AS manifest, {capabilities_text} AS capabilities, \
         {input_text} AS input_schema, {output_text} AS output_schema \
         FROM core_agentplugininstallation installation \
         JOIN core_agentplugindefinition plugin ON plugin.id = installation.plugin_id \
         WHERE CAST(installation.tenant_id AS TEXT) = ? AND CAST(installation.project_id AS TEXT) = ? \
         AND installation.status = 'installed' ORDER BY plugin.name, CAST(installation.id AS TEXT)"
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
    let created = timestamp(&state, "invocation", "created_at");
    let mut entries = Vec::<Value>::new();
    for row in &rows {
        let enabled: i64 = row.try_get("enabled_int").unwrap_or(0);
        if enabled == 0 {
            continue;
        }
        let installation_id: String = row.try_get("installation_id").unwrap_or_default();
        let plugin_key: String = row.try_get("plugin_key").unwrap_or_default();
        let plugin_version: String = row.try_get("plugin_version").unwrap_or_default();
        let protocol: String = row.try_get("protocol").unwrap_or_default();
        let definitions = capability_definitions(
            row.try_get::<Option<String>, _>("capabilities")
                .ok()
                .flatten(),
            row.try_get::<Option<String>, _>("manifest").ok().flatten(),
            row.try_get::<Option<String>, _>("input_schema")
                .ok()
                .flatten(),
            row.try_get::<Option<String>, _>("output_schema")
                .ok()
                .flatten(),
            row.try_get::<i64, _>("timeout_seconds").unwrap_or(30),
        );
        for definition in definitions {
            let Some(name) = definition.get("name").and_then(Value::as_str) else {
                continue;
            };
            let last = sqlx::query(&format!(
                "SELECT status, error_code, {created} AS created FROM core_agentcapabilityinvocation \
                 WHERE CAST(installation_id AS TEXT) = ? AND capability = ? \
                 ORDER BY created_at DESC LIMIT 1"
            ))
            .bind(&installation_id)
            .bind(name)
            .fetch_optional(pool)
            .await
            .ok()
            .flatten();
            let last_value = last.map(|row| {
                json!({
                    "status": row.try_get::<String, _>("status").unwrap_or_default(),
                    "error_code": row.try_get::<String, _>("error_code").unwrap_or_default(),
                    "created_at": row.try_get::<Option<String>, _>("created").ok().flatten(),
                })
            });
            entries.push(json!({
                "name": name,
                "plugin_key": plugin_key,
                "plugin_version": plugin_version,
                "protocol": protocol,
                "risk_level": definition.get("risk_level").cloned().unwrap_or_else(|| json!("low")),
                "installation_id": installation_id,
                "input_schema": definition.get("input_schema").cloned().unwrap_or_else(|| json!({})),
                "last_invocation": last_value,
            }));
        }
    }
    entries.sort_by(|left, right| {
        let left_key = (
            left.get("name").and_then(Value::as_str).unwrap_or(""),
            left.get("plugin_key").and_then(Value::as_str).unwrap_or(""),
            left.get("installation_id")
                .and_then(Value::as_str)
                .unwrap_or(""),
        );
        let right_key = (
            right.get("name").and_then(Value::as_str).unwrap_or(""),
            right
                .get("plugin_key")
                .and_then(Value::as_str)
                .unwrap_or(""),
            right
                .get("installation_id")
                .and_then(Value::as_str)
                .unwrap_or(""),
        );
        left_key.cmp(&right_key)
    });
    Json(json!({"capabilities": entries})).into_response()
}

/// Native replacement for `GET /api/v1/tenants/{tenant}/projects/{project}/marketplace-agents/`.
pub async fn list_marketplace_agents(
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
    let draft_text = if is_postgres(&state) {
        "CAST(agent.draft_config AS TEXT)"
    } else {
        "agent.draft_config"
    };
    let sql = format!(
        "SELECT CAST(agent.id AS TEXT) AS id, agent.slug, agent.name, agent.current_version, \
         {draft_text} AS draft_text FROM core_agentdefinition agent \
         WHERE CAST(agent.tenant_id AS TEXT) = ? AND CAST(agent.project_id AS TEXT) = ? \
         AND NOT agent.is_archived ORDER BY agent.name"
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
    let mut entries = Vec::<Value>::new();
    for row in &rows {
        let draft: Value = row
            .try_get::<String, _>("draft_text")
            .ok()
            .and_then(|value| serde_json::from_str(&value).ok())
            .unwrap_or_else(|| json!({}));
        if draft.get("kind").and_then(Value::as_str) != Some("marketplace_agent") {
            continue;
        }
        let agent_id: String = row.try_get("id").unwrap_or_default();
        let version: i64 = row.try_get("current_version").unwrap_or(0);
        let version_row = sqlx::query(
            "SELECT CAST(id AS TEXT) AS id, version FROM core_agentversion \
             WHERE CAST(agent_id AS TEXT) = ? AND version = ? LIMIT 1",
        )
        .bind(&agent_id)
        .bind(version)
        .fetch_optional(pool)
        .await
        .ok()
        .flatten();
        let installation_id = draft
            .get("installation_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let installation = sqlx::query(
            "SELECT installation.status, installation.plugin_version, \
             CAST(installation.granted_capabilities AS TEXT) AS grants, \
             CAST(plugin.enabled AS INTEGER) AS plugin_enabled \
             FROM core_agentplugininstallation installation \
             JOIN core_agentplugindefinition plugin ON plugin.id = installation.plugin_id \
             WHERE CAST(installation.id AS TEXT) = ? LIMIT 1",
        )
        .bind(installation_id)
        .fetch_optional(pool)
        .await
        .ok()
        .flatten();
        let (available, reason) = availability(installation.as_ref(), &draft);
        entries.push(json!({
            "agent_id": agent_id,
            "slug": row.try_get::<String, _>("slug").unwrap_or_default(),
            "name": row.try_get::<String, _>("name").unwrap_or_default(),
            "plugin_key": draft.get("plugin_key").cloned().unwrap_or(Value::Null),
            "plugin_version": draft.get("plugin_version").cloned().unwrap_or(Value::Null),
            "capability": draft.get("capability").cloned().unwrap_or(Value::Null),
            "protocol": draft.get("protocol").cloned().unwrap_or(Value::Null),
            "risk_level": draft.get("risk_level").cloned().unwrap_or(Value::Null),
            "agent_version": version_row.as_ref().map(|row| row.try_get::<i64, _>("version").unwrap_or(0)).unwrap_or(0),
            "agent_version_id": version_row.as_ref().and_then(|row| row.try_get::<String, _>("id").ok()),
            "installation_id": installation_id,
            "installation_status": installation.as_ref().and_then(|row| row.try_get::<String, _>("status").ok()),
            "available": available,
            "availability_reason": reason,
        }));
    }
    Json(json!({"agents": entries})).into_response()
}

fn availability(installation: Option<&sqlx::any::AnyRow>, draft: &Value) -> (bool, Option<String>) {
    let Some(row) = installation else {
        return (false, Some("installation_missing".to_string()));
    };
    let status: String = row.try_get("status").unwrap_or_default();
    if status == "uninstalled" {
        return (false, Some("installation_uninstalled".to_string()));
    }
    if status == "disabled" {
        return (false, Some("installation_disabled".to_string()));
    }
    if row.try_get::<i64, _>("plugin_enabled").unwrap_or(0) == 0 {
        return (false, Some("plugin_disabled".to_string()));
    }
    let plugin_version: String = row.try_get("plugin_version").unwrap_or_default();
    if draft.get("plugin_version").and_then(Value::as_str) != Some(plugin_version.as_str()) {
        return (false, Some("version_mismatch".to_string()));
    }
    let grants: Vec<String> = row
        .try_get::<String, _>("grants")
        .ok()
        .and_then(|value| serde_json::from_str::<Vec<String>>(&value).ok())
        .unwrap_or_default();
    let capability = draft
        .get("capability")
        .and_then(Value::as_str)
        .unwrap_or("");
    if !grants.is_empty() && !grants.iter().any(|value| value == capability) {
        return (false, Some("capability_not_granted".to_string()));
    }
    (true, None)
}

/// `GET /api/v1/tenants/{tenant}/agent-plugins/versions/?plugin_key=...`
pub async fn list_plugin_versions(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(tenant_id): Path<String>,
    Query(params): Query<std::collections::HashMap<String, String>>,
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
    let plugin_key = params.get("plugin_key").cloned().unwrap_or_default();
    if plugin_key.is_empty()
        || plugin_key.len() > 100
        || !plugin_key
            .chars()
            .all(|ch| ch.is_ascii_lowercase() || ch.is_ascii_digit() || ch == '-')
        || plugin_key.starts_with('-')
        || plugin_key.ends_with('-')
    {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"detail": "plugin_key must be a lowercase hyphenated identifier."})),
        )
            .into_response();
    }
    let created = timestamp(&state, "plugin", "created_at");
    let sql = format!(
        "SELECT CAST(plugin.id AS TEXT) AS id, plugin.version, \
         CAST(plugin.enabled AS INTEGER) AS enabled_int, plugin.capabilities, plugin.manifest, \
         {created} AS created FROM core_agentplugindefinition plugin \
         WHERE CAST(plugin.tenant_id AS TEXT) = ? AND plugin.plugin_key = ? \
         ORDER BY plugin.created_at DESC"
    );
    let rows = match sqlx::query(&sql)
        .bind(&tenant_id)
        .bind(&plugin_key)
        .fetch_all(pool)
        .await
    {
        Ok(rows) => rows,
        Err(error) => return database_error(error.to_string()),
    };
    let active_id = rows
        .iter()
        .find(|row| row.try_get::<i64, _>("enabled_int").unwrap_or(0) != 0)
        .and_then(|row| row.try_get::<String, _>("id").ok());
    let results: Vec<Value> = rows
        .iter()
        .map(|row| {
            let manifest: Value = row
                .try_get::<String, _>("manifest")
                .ok()
                .and_then(|value| serde_json::from_str(&value).ok())
                .unwrap_or_else(|| json!({}));
            let id = row.try_get::<String, _>("id").unwrap_or_default();
            json!({
                "id": id,
                "version": row.try_get::<String, _>("version").unwrap_or_default(),
                "enabled": row.try_get::<i64, _>("enabled_int").unwrap_or(0) != 0,
                "is_active": active_id.as_deref() == Some(id.as_str()),
                "schema_fingerprint": manifest.get("studio").and_then(|studio| studio.get("schema_fingerprint")).cloned().unwrap_or_else(|| json!("")),
                "capabilities": capability_names(
                    row.try_get::<Option<String>, _>("capabilities").ok().flatten(),
                    row.try_get::<Option<String>, _>("manifest").ok().flatten(),
                ).len(),
                "created_at": row.try_get::<Option<String>, _>("created").ok().flatten(),
            })
        })
        .collect();
    Json(Value::Array(results)).into_response()
}

/// `POST /api/v1/agent-plugins/{plugin_id}/activate/`
pub async fn activate_plugin_version(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(plugin_id): Path<String>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let plugin = match sqlx::query(
        "SELECT CAST(plugin.tenant_id AS TEXT) AS tenant_id, plugin.plugin_key, plugin.version, \
         plugin.capabilities, plugin.manifest FROM core_agentplugindefinition plugin \
         WHERE CAST(plugin.id AS TEXT) = ? LIMIT 1",
    )
    .bind(&plugin_id)
    .fetch_optional(pool)
    .await
    {
        Ok(Some(row)) => row,
        Ok(None) => return not_found(),
        Err(error) => return database_error(error.to_string()),
    };
    let tenant_id: String = plugin.try_get("tenant_id").unwrap_or_default();
    match crate::billing_api::tenant_admin(pool, &tenant_id, user.id).await {
        Ok(true) => {}
        Ok(false) => {
            return (
                StatusCode::FORBIDDEN,
                Json(json!({"detail": "Tenant administrator permission required."})),
            )
                .into_response()
        }
        Err(error) => return database_error(error),
    }
    let plugin_key: String = plugin.try_get("plugin_key").unwrap_or_default();
    let plugin_version: String = plugin.try_get("version").unwrap_or_default();
    let target_names = capability_names(
        plugin
            .try_get::<Option<String>, _>("capabilities")
            .ok()
            .flatten(),
        plugin
            .try_get::<Option<String>, _>("manifest")
            .ok()
            .flatten(),
    );
    let now = Utc::now().format("%Y-%m-%d %H:%M:%S").to_string();
    let now_expr = if is_postgres(&state) {
        "NOW()".to_string()
    } else {
        format!("'{now}'")
    };
    let _ = sqlx::query(&format!(
        "UPDATE core_agentplugindefinition SET enabled = 0, updated_at = {now_expr} \
         WHERE CAST(tenant_id AS TEXT) = ? AND plugin_key = ? AND CAST(id AS TEXT) <> ?"
    ))
    .bind(&tenant_id)
    .bind(&plugin_key)
    .bind(&plugin_id)
    .execute(pool)
    .await;
    if let Err(error) = sqlx::query(&format!(
        "UPDATE core_agentplugindefinition SET enabled = 1, updated_at = {now_expr} \
         WHERE CAST(id AS TEXT) = ?"
    ))
    .bind(&plugin_id)
    .execute(pool)
    .await
    {
        return database_error(error.to_string());
    }
    let grant_text = if is_postgres(&state) {
        "CAST(installation.granted_capabilities AS TEXT)"
    } else {
        "installation.granted_capabilities"
    };
    let installations = sqlx::query(&format!(
        "SELECT CAST(installation.id AS TEXT) AS id, CAST(installation.project_id AS TEXT) AS project_id, \
         CAST(installation.plugin_id AS TEXT) AS current_plugin_id, installation.status, \
         installation.plugin_version, {grant_text} AS grants FROM core_agentplugininstallation installation \
         JOIN core_agentplugindefinition source ON source.id = installation.plugin_id \
         WHERE CAST(installation.tenant_id AS TEXT) = ? AND source.plugin_key = ? \
         ORDER BY installation.project_id, CAST(installation.id AS TEXT)"
    ))
    .bind(&tenant_id)
    .bind(&plugin_key)
    .fetch_all(pool)
    .await
    .unwrap_or_default();
    let mut grouped: std::collections::BTreeMap<String, Vec<&sqlx::any::AnyRow>> =
        std::collections::BTreeMap::new();
    for row in &installations {
        grouped
            .entry(row.try_get::<String, _>("project_id").unwrap_or_default())
            .or_default()
            .push(row);
    }
    let mut migrated = 0i64;
    let mut dropped: serde_json::Map<String, Value> = serde_json::Map::new();
    for rows in grouped.values() {
        if rows
            .iter()
            .all(|row| row.try_get::<String, _>("status").unwrap_or_default() == "uninstalled")
        {
            continue;
        }
        let target = rows
            .iter()
            .find(|row| {
                row.try_get::<String, _>("current_plugin_id")
                    .ok()
                    .as_deref()
                    == Some(plugin_id.as_str())
            })
            .copied()
            .unwrap_or_else(|| {
                rows.iter()
                    .max_by_key(|row| {
                        (
                            row.try_get::<String, _>("status").unwrap_or_default() == "installed",
                            row.try_get::<String, _>("id").unwrap_or_default(),
                        )
                    })
                    .copied()
                    .unwrap()
            });
        let mut kept: Vec<String> = Vec::new();
        let mut removed: Vec<String> = Vec::new();
        for row in rows {
            let grants: Vec<String> = row
                .try_get::<String, _>("grants")
                .ok()
                .and_then(|value| serde_json::from_str::<Vec<String>>(&value).ok())
                .unwrap_or_default();
            for name in grants {
                if target_names.iter().any(|item| item == &name) {
                    if !kept.contains(&name) {
                        kept.push(name);
                    }
                } else if !removed.contains(&name) {
                    removed.push(name);
                }
            }
        }
        let target_id: String = target.try_get("id").unwrap_or_default();
        if !removed.is_empty() {
            dropped.insert(target_id.clone(), json!(removed));
        }
        let any_installed = rows
            .iter()
            .any(|row| row.try_get::<String, _>("status").unwrap_or_default() == "installed");
        let _ = sqlx::query(
            "UPDATE core_agentplugininstallation SET plugin_id = ?, plugin_version = ?, \
             status = ?, granted_capabilities = ? WHERE CAST(id AS TEXT) = ?",
        )
        .bind(&plugin_id)
        .bind(&plugin_version)
        .bind(if any_installed {
            "installed"
        } else {
            "disabled"
        })
        .bind(json!(kept).to_string())
        .bind(&target_id)
        .execute(pool)
        .await;
        migrated += 1;
        for row in rows {
            let row_id: String = row.try_get("id").unwrap_or_default();
            if row_id == target_id {
                continue;
            }
            if row.try_get::<String, _>("status").unwrap_or_default() == "installed" {
                let _ = sqlx::query(
                    "UPDATE core_agentplugininstallation SET status = 'disabled' WHERE CAST(id AS TEXT) = ?",
                )
                .bind(&row_id)
                .execute(pool)
                .await;
            }
        }
    }
    let _ = crate::audit::record_event(
        pool,
        state.config.database_url.as_deref().unwrap_or(""),
        crate::audit::AuditEvent {
            tenant_id: Some(tenant_id.as_str()),
            project_id: None,
            event_type: "agent_plugin.version_activated",
            resource_type: "agent_plugin_definition",
            resource_id: plugin_id.as_str(),
            outcome: "success",
            actor_id: Some(user.id),
            request_id: "",
            remote_addr: None,
            user_agent: "",
            metadata: json!({
                "plugin_key": plugin_key,
                "version": plugin_version,
                "migrated_installations": migrated,
                "dropped_capabilities": dropped,
            }),
        },
    )
    .await;
    let created = timestamp(&state, "plugin", "created_at");
    let updated = timestamp(&state, "plugin", "updated_at");
    let sql = format!(
        "SELECT CAST(plugin.id AS TEXT) AS id, CAST(plugin.tenant_id AS TEXT) AS tenant_id, \
         plugin.plugin_key, plugin.name, plugin.description, plugin.version, plugin.protocol, \
         plugin.endpoint_url, CAST(plugin.auth_required AS INTEGER) AS auth_required_int, \
         plugin.credential_ref, plugin.capabilities, plugin.manifest, plugin.input_schema, \
         plugin.output_schema, plugin.timeout_seconds, plugin.max_concurrency, \
         CAST(plugin.enabled AS INTEGER) AS enabled_int, {created} AS created, {updated} AS updated \
         FROM core_agentplugindefinition plugin WHERE CAST(plugin.id AS TEXT) = ? LIMIT 1"
    );
    match sqlx::query(&sql)
        .bind(&plugin_id)
        .fetch_optional(pool)
        .await
    {
        Ok(Some(row)) => {
            let mut body = plugin_json(&row);
            if let Some(object) = body.as_object_mut() {
                object.insert(
                    "migration".to_string(),
                    json!({"installations": migrated, "dropped_capabilities": dropped}),
                );
            }
            Json(body).into_response()
        }
        Ok(None) => not_found(),
        Err(error) => database_error(error.to_string()),
    }
}

pub(crate) async fn installation_response(
    state: &AppState,
    pool: &AnyPool,
    installation_id: &str,
) -> Response {
    let created = timestamp(state, "installation", "created_at");
    let updated = timestamp(state, "installation", "updated_at");
    let plugin_created = timestamp(state, "plugin", "created_at");
    let plugin_updated = timestamp(state, "plugin", "updated_at");
    let sql = format!(
        "SELECT CAST(installation.id AS TEXT) AS id, \
         CAST(installation.tenant_id AS TEXT) AS tenant_id, \
         CAST(installation.project_id AS TEXT) AS project_id, \
         installation.plugin_version, installation.status, \
         {} AS granted, installation.credential_ref AS installation_credential, \
         {created} AS created, {updated} AS updated, \
         CAST(plugin.id AS TEXT) AS plugin_id, CAST(plugin.tenant_id AS TEXT) AS plugin_tenant_id, \
         plugin.plugin_key, plugin.name, plugin.description, plugin.version, plugin.protocol, \
         plugin.endpoint_url, CAST(plugin.auth_required AS INTEGER) AS auth_required_int, \
         plugin.credential_ref AS plugin_credential, {} AS plugin_capabilities, \
         {} AS plugin_manifest, {} AS plugin_input, {} AS plugin_output, \
         plugin.timeout_seconds, plugin.max_concurrency, \
         CAST(plugin.enabled AS INTEGER) AS enabled_int, {plugin_created} AS plugin_created, \
         {plugin_updated} AS plugin_updated \
         FROM core_agentplugininstallation installation \
         JOIN core_agentplugindefinition plugin \
           ON CAST(plugin.id AS TEXT) = CAST(installation.plugin_id AS TEXT) \
         WHERE CAST(installation.id AS TEXT) = ?",
        json_text(state, "installation.granted_capabilities"),
        json_text(state, "plugin.capabilities"),
        json_text(state, "plugin.manifest"),
        json_text(state, "plugin.input_schema"),
        json_text(state, "plugin.output_schema"),
    );
    match sqlx::query(&sql)
        .bind(installation_id)
        .fetch_one(pool)
        .await
    {
        Ok(row) => Json(installation_json(&row)).into_response(),
        Err(error) => database_error(error.to_string()),
    }
}

/// `POST /api/v1/agent-plugin-installations/{installation_id}/credential/`
pub async fn rotate_installation_credential(
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
         installation.credential_ref AS credential_ref, \
         CAST(plugin.auth_required AS INTEGER) AS auth_required_int, plugin.plugin_key \
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
    if scope.try_get::<String, _>("status").unwrap_or_default() == "uninstalled" {
        return legacy_error(
            "Uninstalled installations cannot rotate a credential; install it again.",
            StatusCode::CONFLICT,
            "INSTALLATION_NOT_ACTIVE",
        );
    }
    if scope.try_get::<i64, _>("auth_required_int").unwrap_or(0) == 0 {
        return legacy_error(
            "This agent plugin does not accept a credential reference.",
            StatusCode::BAD_REQUEST,
            "CREDENTIAL_NOT_ACCEPTED",
        );
    }
    let payload = match parse_object(&body) {
        Ok(payload) => payload,
        Err(response) => return response,
    };
    let reference = payload
        .get("credential_ref")
        .and_then(Value::as_str)
        .unwrap_or("");
    if reference.is_empty() || reference.chars().any(char::is_whitespace) {
        return legacy_error(
            "Connector credential reference is invalid.",
            StatusCode::BAD_REQUEST,
            "CREDENTIAL_REFERENCE_INVALID",
        );
    }
    if reference.len() > 200 {
        return legacy_error(
            "credential_ref must be at most 200 characters.",
            StatusCode::BAD_REQUEST,
            "CREDENTIAL_REFERENCE_INVALID",
        );
    }
    let (scheme, rest) = match reference.split_once("://") {
        Some(value) => value,
        None => {
            return legacy_error(
                "Connector credential reference is invalid.",
                StatusCode::BAD_REQUEST,
                "CREDENTIAL_REFERENCE_INVALID",
            )
        }
    };
    if rest.is_empty()
        || scheme.is_empty()
        || !scheme
            .chars()
            .next()
            .is_some_and(|first| first.is_ascii_lowercase())
        || !scheme.chars().all(|value| {
            value.is_ascii_lowercase() || value.is_ascii_digit() || matches!(value, '+' | '.' | '-')
        })
    {
        return legacy_error(
            "Connector credential reference is invalid.",
            StatusCode::BAD_REQUEST,
            "CREDENTIAL_REFERENCE_INVALID",
        );
    }
    if !matches!(scheme, "env" | "vault" | "plugin") {
        return legacy_error(
            &format!("No credential provider is registered for scheme '{scheme}'."),
            StatusCode::BAD_REQUEST,
            "CREDENTIAL_PROVIDER_UNSUPPORTED",
        );
    }
    if scheme == "plugin" {
        let owned = sqlx::query(
            "SELECT 1 FROM core_agentplugincredential \
             WHERE CAST(id AS TEXT) = ? AND CAST(tenant_id AS TEXT) = ? LIMIT 1",
        )
        .bind(rest)
        .bind(&tenant_id)
        .fetch_optional(pool)
        .await;
        match owned {
            Ok(Some(_)) => {}
            Ok(None) => {
                return legacy_error(
                    "Plugin credential is not available in this workspace.",
                    StatusCode::BAD_REQUEST,
                    "CREDENTIAL_ACCESS_DENIED",
                )
            }
            Err(error) => return database_error(error.to_string()),
        }
    } else if scheme == "env" {
        if !rest.starts_with("PLUGIN_SECRET_") {
            return legacy_error(
                "Credential variable is outside the configured provider allowlist.",
                StatusCode::BAD_REQUEST,
                "CREDENTIAL_NOT_FOUND",
            );
        }
        if std::env::var(rest).is_err() {
            return legacy_error(
                "Credential variable is not available to the process.",
                StatusCode::BAD_REQUEST,
                "CREDENTIAL_NOT_FOUND",
            );
        }
    } else if std::env::var("VAULT_ADDR").is_err() {
        return legacy_error(
            "Credential resolver is not configured.",
            StatusCode::BAD_REQUEST,
            "CREDENTIAL_RESOLVER_NOT_CONFIGURED",
        );
    }
    let previous = scope
        .try_get::<String, _>("credential_ref")
        .unwrap_or_default();
    let previous_scheme = previous
        .split_once("://")
        .map(|value| value.0)
        .unwrap_or("");
    if let Err(error) = sqlx::query(
        "UPDATE core_agentplugininstallation SET credential_ref = ?, \
         revision = revision + 1, updated_at = CURRENT_TIMESTAMP WHERE CAST(id AS TEXT) = ?",
    )
    .bind(reference)
    .bind(&installation_id)
    .execute(pool)
    .await
    {
        return database_error(error.to_string());
    }
    let postgres = is_postgres(&state);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return sqlx_error(error),
    };
    if let Err(error) = record_event_in_transaction(
        &mut transaction,
        postgres,
        &AuditEvent {
            tenant_id: Some(&tenant_id),
            project_id: Some(&project_id),
            event_type: "agent_plugin.credential_rotated",
            resource_type: "agent_plugin_installation",
            resource_id: &installation_id,
            outcome: "success",
            actor_id: Some(user.id),
            request_id: header(&headers, "x-request-id"),
            remote_addr: None,
            user_agent: header(&headers, "user-agent"),
            metadata: json!({
                "plugin_key": scope.try_get::<String, _>("plugin_key").unwrap_or_default(),
                "previous_scheme": previous_scheme,
                "new_scheme": scheme,
            }),
        },
    )
    .await
    {
        let _ = transaction.rollback().await;
        return database_error(error.to_string());
    }
    if let Err(error) = transaction.commit().await {
        return sqlx_error(error);
    }
    installation_response(&state, pool, &installation_id).await
}

pub async fn invoke_project_capability(
    State(state): State<AppState>,
    Path((tenant_id, project_id, plugin_id, capability)): Path<(String, String, String, String)>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    if !crate::tenancy::project_access(pool, &tenant_id, &project_id, user.id)
        .await
        .unwrap_or(false)
    {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"detail": "Project access required."})),
        )
            .into_response();
    }
    let arguments = body.get("arguments").cloned().unwrap_or_else(|| json!({}));
    let request = json!({"capability": capability, "arguments": arguments});
    match crate::mcp_api::invoke(&state, &tenant_id, &project_id, &request, Some(&plugin_id)).await
    {
        Ok(result) => (StatusCode::OK, Json(result)).into_response(),
        Err(error) => {
            let message = error
                .pointer("/error/message")
                .and_then(|value| value.as_str())
                .unwrap_or("Capability invocation failed.");
            let code = error
                .pointer("/error/data/code")
                .and_then(|value| value.as_str())
                .unwrap_or("CAPABILITY_INVOCATION_FAILED");
            let status = if code == "CAPABILITY_NOT_INSTALLED" || code == "CAPABILITY_NOT_DECLARED"
            {
                StatusCode::NOT_FOUND
            } else if code == "APPROVAL_REQUIRED" {
                StatusCode::CONFLICT
            } else {
                StatusCode::UNPROCESSABLE_ENTITY
            };
            (
                status,
                Json(json!({"error": {"code": code, "message": message, "details": {}}})),
            )
                .into_response()
        }
    }
}

fn declared_capabilities_from_row(plugin: &sqlx::any::AnyRow) -> Vec<Value> {
    let manifest: Value = plugin
        .try_get::<String, _>("manifest_text")
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_else(|| json!({}));
    if let Some(entries) = manifest.get("capabilities").and_then(Value::as_array) {
        if !entries.is_empty() {
            return entries
                .iter()
                .filter(|entry| entry.is_object())
                .cloned()
                .collect();
        }
    }
    let legacy: Value = plugin
        .try_get::<String, _>("capabilities_text")
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_else(|| json!([]));
    let input_schema: Value = plugin
        .try_get::<String, _>("input_schema_text")
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_else(|| json!({"type": "object", "properties": {}}));
    let output_schema: Value = plugin
        .try_get::<String, _>("output_schema_text")
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_else(|| json!({"type": "object", "properties": {}}));
    let timeout = plugin.try_get::<i64, _>("timeout_seconds").unwrap_or(30);
    legacy
        .as_array()
        .map(|entries| {
            entries
                .iter()
                .filter_map(|entry| {
                    if let Some(name) = entry.as_str() {
                        Some(json!({
                            "name": name,
                            "description": "",
                            "risk_level": "low",
                            "timeout_seconds": timeout,
                            "input_schema": input_schema,
                            "output_schema": output_schema,
                        }))
                    } else if entry.get("name").and_then(Value::as_str).is_some() {
                        let mut object = entry.clone();
                        if let Value::Object(ref mut map) = object {
                            map.entry("description").or_insert(json!(""));
                            map.entry("risk_level").or_insert(json!("low"));
                            map.entry("timeout_seconds").or_insert(json!(timeout));
                            map.entry("input_schema").or_insert(input_schema.clone());
                            map.entry("output_schema").or_insert(output_schema.clone());
                        }
                        Some(object)
                    } else {
                        None
                    }
                })
                .collect()
        })
        .unwrap_or_default()
}

fn hex_sha256(value: &str) -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(value.as_bytes());
    hex::encode(hasher.finalize())
}

fn binding_slug(plugin_key: &str, capability: &str) -> String {
    fn part(value: &str) -> String {
        let normalized: String = value
            .to_lowercase()
            .chars()
            .map(|ch| if ch.is_ascii_alphanumeric() { ch } else { '-' })
            .collect();
        normalized.trim_matches('-').to_string()
    }
    let base = format!("{}-{}", part(plugin_key), part(capability));
    let digest = hex_sha256(&format!("{plugin_key}:{capability}"));
    let mut base = if base.len() > 72 {
        base[..72].trim_end_matches('-').to_string()
    } else {
        base
    };
    base.push('-');
    base.push_str(&digest[..6]);
    base.chars().take(80).collect()
}

/// `POST /api/v1/agent-plugin-installations/{installation_id}/bind-agent/`
pub async fn bind_installation_agents(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(installation_id): Path<String>,
    Json(body): Json<Value>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
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
    let sql = format!(
        "SELECT CAST(installation.id AS TEXT) AS id, CAST(installation.tenant_id AS TEXT) AS tenant_id, \
         CAST(installation.project_id AS TEXT) AS project_id, installation.plugin_version, \
         installation.status, {capabilities_text} AS capabilities_text, {manifest_text} AS manifest_text, \
         {input_text} AS input_schema_text, {output_text} AS output_schema_text, plugin.timeout_seconds, \
         plugin.protocol, plugin.plugin_key, plugin.name, CAST(plugin.enabled AS INTEGER) AS enabled_int \
         FROM core_agentplugininstallation installation JOIN core_agentplugindefinition plugin \
           ON CAST(plugin.id AS TEXT) = CAST(installation.plugin_id AS TEXT) \
         WHERE CAST(installation.id AS TEXT) = ?"
    );
    let row = match sqlx::query(&sql)
        .bind(&installation_id)
        .fetch_optional(pool)
        .await
    {
        Ok(Some(row)) => row,
        Ok(None) => return not_found(),
        Err(error) => return database_error(error.to_string()),
    };
    let tenant_id: String = row.try_get("tenant_id").unwrap_or_default();
    let project_id: String = row.try_get("project_id").unwrap_or_default();
    if !tenant_member(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return not_found();
    }
    let editable = sqlx::query(
        "SELECT 1 FROM core_tenantmembership \
         WHERE CAST(tenant_id AS TEXT) = ? AND user_id = ? AND role IN ('owner', 'admin') \
         UNION SELECT 1 FROM core_projectmembership \
         WHERE CAST(project_id AS TEXT) = ? AND user_id = ? AND role IN ('admin', 'editor') \
         LIMIT 1",
    )
    .bind(&tenant_id)
    .bind(user.id)
    .bind(&project_id)
    .bind(user.id)
    .fetch_optional(pool)
    .await;
    match editable {
        Ok(Some(_)) => {}
        Ok(None) => {
            return (
                StatusCode::FORBIDDEN,
                Json(json!({"detail": "Project editor permission required."})),
            )
                .into_response()
        }
        Err(error) => return database_error(error.to_string()),
    }
    let status: String = row.try_get("status").unwrap_or_default();
    let enabled = row.try_get::<i64, _>("enabled_int").unwrap_or(0) != 0;
    if status != "installed" || !enabled {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({
                "detail": "The agent plugin installation is not installed and cannot be bound.",
                "code": "BINDING_INSTALLATION_INACTIVE",
            })),
        )
            .into_response();
    }
    let declared = declared_capabilities_from_row(&row);
    let names: Vec<String> = declared
        .iter()
        .filter_map(|entry| {
            entry
                .get("name")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .collect();
    let requested: Vec<String> = match body.get("capabilities") {
        None | Some(Value::Null) => names.clone(),
        Some(Value::Array(items)) => {
            let mut result = Vec::new();
            for item in items {
                let Some(name) = item.as_str() else {
                    return (
                        StatusCode::BAD_REQUEST,
                        Json(
                            json!({"detail": "capabilities must be an array of capability names."}),
                        ),
                    )
                        .into_response();
                };
                if !result.iter().any(|existing| existing == name) {
                    result.push(name.to_string());
                }
            }
            result
        }
        Some(_) => {
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"detail": "capabilities must be an array of capability names."})),
            )
                .into_response()
        }
    };
    let unknown: Vec<&String> = requested
        .iter()
        .filter(|name| !names.iter().any(|known| known == *name))
        .collect();
    if !unknown.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({
                "detail": format!("The plugin does not declare capabilities: {}.", unknown.iter().map(|n| n.as_str()).collect::<Vec<_>>().join(", ")),
                "code": "BINDING_CAPABILITY_NOT_DECLARED",
            })),
        )
            .into_response();
    }
    let plugin_key: String = row.try_get("plugin_key").unwrap_or_default();
    let plugin_name: String = row.try_get("name").unwrap_or_default();
    let plugin_version: String = row.try_get("plugin_version").unwrap_or_default();
    let protocol: String = row.try_get("protocol").unwrap_or_default();
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return database_error(error.to_string()),
    };
    let mut bound = Vec::new();
    for capability in &requested {
        let Some(definition) = declared
            .iter()
            .find(|entry| entry.get("name").and_then(Value::as_str) == Some(capability.as_str()))
        else {
            continue;
        };
        let snapshot = json!({
            "kind": "marketplace_agent",
            "plugin_key": plugin_key,
            "plugin_version": plugin_version,
            "installation_id": installation_id,
            "capability": capability,
            "protocol": protocol,
            "input_schema": definition.get("input_schema").cloned().unwrap_or_else(|| json!({})),
            "output_schema": definition.get("output_schema").cloned().unwrap_or_else(|| json!({})),
            "risk_level": definition.get("risk_level").cloned().unwrap_or_else(|| json!("low")),
        });
        let snapshot_text = snapshot.to_string();
        let existing = sqlx::query(
            "SELECT CAST(id AS TEXT) AS id, name, slug, draft_config, current_version \
             FROM core_agentdefinition WHERE CAST(project_id AS TEXT) = ?",
        )
        .bind(&project_id)
        .fetch_all(&mut *transaction)
        .await;
        let existing = match existing {
            Ok(rows) => rows.into_iter().find(|candidate| {
                let config: Value = candidate
                    .try_get::<String, _>("draft_config")
                    .ok()
                    .and_then(|text| serde_json::from_str(&text).ok())
                    .unwrap_or_else(|| json!({}));
                config.get("kind").and_then(Value::as_str) == Some("marketplace_agent")
                    && config.get("installation_id").and_then(Value::as_str)
                        == Some(installation_id.as_str())
                    && config.get("capability").and_then(Value::as_str) == Some(capability.as_str())
            }),
            Err(error) => {
                let _ = transaction.rollback().await;
                return database_error(error.to_string());
            }
        };
        let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
        let postgres = is_postgres(&state);
        if let Some(existing) = existing {
            let definition_id: String = existing.try_get("id").unwrap_or_default();
            let slug: String = existing.try_get("slug").unwrap_or_default();
            let name: String = existing.try_get("name").unwrap_or_default();
            let latest = sqlx::query(
                "SELECT version, config_snapshot, CAST(id AS TEXT) AS id FROM core_agentversion \
                 WHERE CAST(agent_id AS TEXT) = ? ORDER BY version DESC LIMIT 1",
            )
            .bind(&definition_id)
            .fetch_optional(&mut *transaction)
            .await;
            let (version_number, version_id, _version_created, previous_snapshot) = match latest {
                Ok(Some(version)) => {
                    let number: i64 = version.try_get("version").unwrap_or(0);
                    let id: String = version.try_get("id").unwrap_or_default();
                    let previous: String = version.try_get("config_snapshot").unwrap_or_default();
                    (number, id, false, previous)
                }
                Ok(None) => (0, String::new(), false, String::new()),
                Err(error) => {
                    let _ = transaction.rollback().await;
                    return database_error(error.to_string());
                }
            };
            if previous_snapshot == snapshot_text {
                bound.push(json!({
                    "definition_id": definition_id,
                    "slug": slug,
                    "name": name,
                    "agent_version": version_number,
                    "agent_version_id": version_id,
                    "capability": capability,
                    "plugin_key": plugin_key,
                    "plugin_version": plugin_version,
                    "definition_created": false,
                    "version_created": false,
                }));
                continue;
            }
            let next = version_number + 1;
            let new_version_id = uuid::Uuid::new_v4().to_string();
            let insert = if postgres {
                "INSERT INTO core_agentversion (id, agent_id, version, config_snapshot, created_by_id, \
                 published_at) VALUES (?::uuid, ?::uuid, ?, ?::jsonb, ?, ?::timestamptz)"
            } else {
                "INSERT INTO core_agentversion (id, agent_id, version, config_snapshot, created_by_id, \
                 published_at) VALUES (?, ?, ?, ?, ?, ?)"
            };
            if let Err(error) = sqlx::query(insert)
                .bind(&new_version_id)
                .bind(&definition_id)
                .bind(next)
                .bind(&snapshot_text)
                .bind(user.id)
                .bind(&now)
                .execute(&mut *transaction)
                .await
            {
                let _ = transaction.rollback().await;
                return database_error(error.to_string());
            }
            let update = if postgres {
                "UPDATE core_agentdefinition SET draft_config = ?::jsonb, current_version = ?, \
                 updated_at = ?::timestamptz WHERE CAST(id AS TEXT) = ?"
            } else {
                "UPDATE core_agentdefinition SET draft_config = ?, current_version = ?, \
                 updated_at = ? WHERE CAST(id AS TEXT) = ?"
            };
            if let Err(error) = sqlx::query(update)
                .bind(&snapshot_text)
                .bind(next)
                .bind(&now)
                .bind(&definition_id)
                .execute(&mut *transaction)
                .await
            {
                let _ = transaction.rollback().await;
                return database_error(error.to_string());
            }
            bound.push(json!({
                "definition_id": definition_id,
                "slug": slug,
                "name": name,
                "agent_version": next,
                "agent_version_id": new_version_id,
                "capability": capability,
                "plugin_key": plugin_key,
                "plugin_version": plugin_version,
                "definition_created": false,
                "version_created": true,
            }));
        } else {
            let definition_id = uuid::Uuid::new_v4().to_string();
            let version_id = uuid::Uuid::new_v4().to_string();
            let slug = binding_slug(&plugin_key, capability);
            let name = {
                let text = format!("{plugin_name}: {capability}");
                text.chars().take(120).collect::<String>()
            };
            let description = definition
                .get("description")
                .and_then(Value::as_str)
                .unwrap_or("")
                .chars()
                .take(1000)
                .collect::<String>();
            let insert = if postgres {
                "INSERT INTO core_agentdefinition (id, tenant_id, project_id, created_by_id, name, slug, \
                 description, draft_config, current_version, is_archived, created_at, updated_at) \
                 VALUES (?::uuid, ?::uuid, ?::uuid, ?, ?, ?, ?, ?::jsonb, 1, FALSE, ?::timestamptz, ?::timestamptz)"
            } else {
                "INSERT INTO core_agentdefinition (id, tenant_id, project_id, created_by_id, name, slug, \
                 description, draft_config, current_version, is_archived, created_at, updated_at) \
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?)"
            };
            if let Err(error) = sqlx::query(insert)
                .bind(&definition_id)
                .bind(&tenant_id)
                .bind(&project_id)
                .bind(user.id)
                .bind(&name)
                .bind(&slug)
                .bind(&description)
                .bind(&snapshot_text)
                .bind(&now)
                .bind(&now)
                .execute(&mut *transaction)
                .await
            {
                let _ = transaction.rollback().await;
                return database_error(error.to_string());
            }
            let insert_version = if postgres {
                "INSERT INTO core_agentversion (id, agent_id, version, config_snapshot, created_by_id, \
                 published_at) VALUES (?::uuid, ?::uuid, 1, ?::jsonb, ?, ?::timestamptz)"
            } else {
                "INSERT INTO core_agentversion (id, agent_id, version, config_snapshot, created_by_id, \
                 published_at) VALUES (?, ?, 1, ?, ?, ?)"
            };
            if let Err(error) = sqlx::query(insert_version)
                .bind(&version_id)
                .bind(&definition_id)
                .bind(&snapshot_text)
                .bind(user.id)
                .bind(&now)
                .execute(&mut *transaction)
                .await
            {
                let _ = transaction.rollback().await;
                return database_error(error.to_string());
            }
            bound.push(json!({
                "definition_id": definition_id,
                "slug": slug,
                "name": name,
                "agent_version": 1,
                "agent_version_id": version_id,
                "capability": capability,
                "plugin_key": plugin_key,
                "plugin_version": plugin_version,
                "definition_created": true,
                "version_created": true,
            }));
        }
    }
    if let Err(error) = transaction.commit().await {
        return database_error(error.to_string());
    }
    Json(json!({"bound": bound})).into_response()
}
