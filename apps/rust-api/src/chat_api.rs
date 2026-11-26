#![allow(clippy::result_large_err)]

use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use chrono::{SecondsFormat, Utc};
use serde_json::{json, Value};
use sqlx::{AnyPool, Row};

use crate::context::{authenticate, AuthError};
use crate::proxy::AppState;
use crate::tenancy::{is_postgres, normalize_timestamp, project_access};

pub async fn list_threads(
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
    match project_access(pool, &tenant_id, &project_id, user.id).await {
        Ok(true) => {}
        Ok(false) => return not_found(),
        Err(error) => return database_error(error),
    }
    let false_literal = if is_postgres(&state) { "FALSE" } else { "0" };
    let created = timestamp(&state, "thread", "created_at");
    let updated = timestamp(&state, "thread", "updated_at");
    let sql = format!(
        "SELECT CAST(thread.id AS TEXT) AS id, thread.title, \
         CAST(thread.project_id AS TEXT) AS project_id, \
         CAST(thread.model_config_id AS TEXT) AS model_config_id, \
         CAST(thread.archived AS INTEGER) AS archived_int, thread.execution_target, \
         thread.effort, thread.goal, CAST(thread.plan_first AS INTEGER) AS plan_first_int, \
         {created} AS created, {updated} AS updated FROM core_agentchatthread thread \
         WHERE CAST(thread.tenant_id AS TEXT) = ? AND CAST(thread.project_id AS TEXT) = ? \
           AND thread.archived = {false_literal} ORDER BY thread.updated_at DESC LIMIT 50"
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
    let mut items = Vec::new();
    for row in &rows {
        let thread_id: String = row.try_get("id").unwrap_or_default();
        let record = thread_from_row(row);
        let mut payload = thread_json(&record);
        if let Ok(Some(last)) = last_message(pool, &state, &thread_id).await {
            payload["last_message"] = last;
        }
        items.push(payload);
    }
    Json(json!({"items": items})).into_response()
}

pub async fn create_thread(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id)): Path<(String, String)>,
    body: Bytes,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    match project_access(pool, &tenant_id, &project_id, user.id).await {
        Ok(true) => {}
        Ok(false) => return not_found(),
        Err(error) => return database_error(error),
    }
    let payload = match parse_object(&body) {
        Ok(payload) => payload,
        Err(response) => return response,
    };
    let title = match payload.get("title") {
        None => "New conversation".to_string(),
        Some(Value::String(value)) => value.trim().chars().take(200).collect(),
        Some(_) => {
            return legacy_error(
                "title must be a string.",
                StatusCode::BAD_REQUEST,
                "INVALID_TITLE",
            )
        }
    };
    let title = if title.is_empty() {
        "New conversation".to_string()
    } else {
        title
    };
    let mut execution_target = "cloud".to_string();
    if let Some(value) = payload.get("execution_target") {
        let target = value.as_str().unwrap_or("").trim();
        if !matches!(target, "cloud" | "local") {
            return legacy_error(
                "execution_target must be cloud or local.",
                StatusCode::BAD_REQUEST,
                "INVALID_EXECUTION_TARGET",
            );
        }
        execution_target = target.to_string();
    }
    let mut effort = "auto".to_string();
    if let Some(value) = payload.get("effort") {
        let effort_value = value.as_str().unwrap_or("").trim();
        if !matches!(effort_value, "auto" | "low") {
            return legacy_error(
                "effort must be auto or low.",
                StatusCode::BAD_REQUEST,
                "INVALID_EFFORT",
            );
        }
        effort = effort_value.to_string();
    }
    let goal = match payload.get("goal") {
        None => String::new(),
        Some(Value::String(value)) if value.chars().count() <= 500 => value.trim().to_string(),
        Some(_) => {
            return legacy_error(
                "goal must be a string of at most 500 characters.",
                StatusCode::BAD_REQUEST,
                "INVALID_GOAL",
            )
        }
    };
    let plan_first = payload
        .get("plan_first")
        .map(|value| value.as_bool().unwrap_or(false))
        .unwrap_or(false);
    let model_config_id = match model_id(&payload).await {
        Ok(id) => id,
        Err(response) => return response,
    };
    let model_config_id = match model_config_id {
        Some(id) => match visible_model(pool, &state, &tenant_id, user.id, &id).await {
            Ok(true) => Some(id),
            Ok(false) => return not_found(),
            Err(error) => return database_error(error),
        },
        None => match default_model(pool, &state, &tenant_id, user.id).await {
            Ok(id) => id,
            Err(error) => return database_error(error),
        },
    };
    let thread_id = uuid::Uuid::new_v4().to_string();
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    let postgres = is_postgres(&state);
    let insert = if postgres {
        "INSERT INTO core_agentchatthread (id, tenant_id, project_id, created_by_id, title, \
         model_config_id, archived, execution_target, effort, goal, plan_first, created_at, \
         updated_at) VALUES (?::uuid, ?::uuid, ?::uuid, ?, ?, ?::uuid, FALSE, ?, ?, ?, ?, \
         ?::timestamptz, ?::timestamptz)"
    } else {
        "INSERT INTO core_agentchatthread (id, tenant_id, project_id, created_by_id, title, \
         model_config_id, archived, execution_target, effort, goal, plan_first, created_at, \
         updated_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)"
    };
    if let Err(error) = sqlx::query(insert)
        .bind(&thread_id)
        .bind(&tenant_id)
        .bind(&project_id)
        .bind(user.id)
        .bind(&title)
        .bind(&model_config_id)
        .bind(&execution_target)
        .bind(&effort)
        .bind(&goal)
        .bind(if plan_first { 1 } else { 0 })
        .bind(&now)
        .bind(&now)
        .execute(pool)
        .await
    {
        return database_error(error.to_string());
    }
    match thread_row(pool, &state, &thread_id).await {
        Ok(Some(record)) => (StatusCode::CREATED, Json(thread_json(&record))).into_response(),
        Ok(None) => not_found(),
        Err(error) => database_error(error),
    }
}

pub async fn get_thread(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(thread_id): Path<String>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let record = match thread_row(pool, &state, &thread_id).await {
        Ok(Some(record))
            if tenant_member(pool, &record.tenant_id, user.id)
                .await
                .unwrap_or(false) =>
        {
            record
        }
        Ok(_) => return not_found(),
        Err(error) => return database_error(error),
    };
    let mut payload = thread_json(&record);
    payload["messages"] = match messages(pool, &state, &thread_id).await {
        Ok(items) => Value::Array(items),
        Err(error) => return database_error(error),
    };
    payload["attachments"] = match attachments(pool, &state, &thread_id).await {
        Ok(items) => Value::Array(items),
        Err(error) => return database_error(error),
    };
    Json(payload).into_response()
}

pub async fn update_thread(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(thread_id): Path<String>,
    body: Bytes,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let record = match thread_row(pool, &state, &thread_id).await {
        Ok(Some(record))
            if tenant_member(pool, &record.tenant_id, user.id)
                .await
                .unwrap_or(false) =>
        {
            record
        }
        Ok(_) => return not_found(),
        Err(error) => return database_error(error),
    };
    let payload = match parse_object(&body) {
        Ok(payload) => payload,
        Err(response) => return response,
    };
    let title = match payload.get("title") {
        None => record.title.clone(),
        Some(Value::String(value)) if !value.trim().is_empty() => {
            value.trim().chars().take(200).collect()
        }
        Some(_) => {
            return legacy_error(
                "title must be a non-empty string.",
                StatusCode::BAD_REQUEST,
                "INVALID_TITLE",
            )
        }
    };
    let archived = payload
        .get("archived")
        .map(|value| value.as_bool().unwrap_or(false))
        .unwrap_or(record.archived);
    let execution_target = match payload.get("execution_target") {
        None => record.execution_target.clone(),
        Some(value) => {
            let target = value.as_str().unwrap_or("").trim();
            if !matches!(target, "cloud" | "local") {
                return legacy_error(
                    "execution_target must be cloud or local.",
                    StatusCode::BAD_REQUEST,
                    "INVALID_EXECUTION_TARGET",
                );
            }
            target.to_string()
        }
    };
    let effort = match payload.get("effort") {
        None => record.effort.clone(),
        Some(value) => {
            let effort_value = value.as_str().unwrap_or("").trim();
            if !matches!(effort_value, "auto" | "low") {
                return legacy_error(
                    "effort must be auto or low.",
                    StatusCode::BAD_REQUEST,
                    "INVALID_EFFORT",
                );
            }
            effort_value.to_string()
        }
    };
    let goal = match payload.get("goal") {
        None => record.goal.clone(),
        Some(Value::String(value)) if value.chars().count() <= 500 => value.trim().to_string(),
        Some(_) => {
            return legacy_error(
                "goal must be a string of at most 500 characters.",
                StatusCode::BAD_REQUEST,
                "INVALID_GOAL",
            )
        }
    };
    let plan_first = payload
        .get("plan_first")
        .map(|value| value.as_bool().unwrap_or(false))
        .unwrap_or(record.plan_first);
    let model_config_id = match model_id(&payload).await {
        Ok(Some(id)) => match visible_model(pool, &state, &record.tenant_id, user.id, &id).await {
            Ok(true) => Some(id),
            Ok(false) => return not_found(),
            Err(error) => return database_error(error),
        },
        Ok(None) => record.model_config_id.clone(),
        Err(response) => return response,
    };
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    if let Err(error) = sqlx::query(
        "UPDATE core_agentchatthread SET title = ?, model_config_id = ?, archived = ?, \
         execution_target = ?, effort = ?, goal = ?, plan_first = ?, updated_at = ? \
         WHERE CAST(id AS TEXT) = ?",
    )
    .bind(&title)
    .bind(&model_config_id)
    .bind(if archived { 1 } else { 0 })
    .bind(&execution_target)
    .bind(&effort)
    .bind(&goal)
    .bind(if plan_first { 1 } else { 0 })
    .bind(&now)
    .bind(&thread_id)
    .execute(pool)
    .await
    {
        return database_error(error.to_string());
    }
    match thread_row(pool, &state, &thread_id).await {
        Ok(Some(record)) => Json(thread_json(&record)).into_response(),
        Ok(None) => not_found(),
        Err(error) => database_error(error),
    }
}

pub async fn delete_thread(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(thread_id): Path<String>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let record = match thread_row(pool, &state, &thread_id).await {
        Ok(Some(record))
            if tenant_member(pool, &record.tenant_id, user.id)
                .await
                .unwrap_or(false) =>
        {
            record
        }
        Ok(_) => return not_found(),
        Err(error) => return database_error(error),
    };
    if let Err(error) = sqlx::query("DELETE FROM core_agentchatthread WHERE CAST(id AS TEXT) = ?")
        .bind(&record.id)
        .execute(pool)
        .await
    {
        return database_error(error.to_string());
    }
    StatusCode::NO_CONTENT.into_response()
}

pub async fn chat_context(
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
    match project_access(pool, &tenant_id, &project_id, user.id).await {
        Ok(true) => {}
        Ok(false) => return not_found(),
        Err(error) => return database_error(error),
    };
    let installation_rows = match sqlx::query(
        "SELECT CAST(installation.id AS TEXT) AS id, plugin.plugin_key, plugin.name, \
         installation.plugin_version, plugin.protocol, \
         CAST(plugin.auth_required AS INTEGER) AS auth_required_int, \
         installation.credential_ref, \
         CASE WHEN plugin.protocol = 'mcp' THEN '[]' ELSE '[]' END AS ignored \
         FROM core_agentplugininstallation installation \
         JOIN core_agentplugindefinition plugin \
           ON CAST(plugin.id AS TEXT) = CAST(installation.plugin_id AS TEXT) \
         WHERE CAST(installation.tenant_id AS TEXT) = ? \
           AND CAST(installation.project_id AS TEXT) = ? AND installation.status = 'installed'",
    )
    .bind(&tenant_id)
    .bind(&project_id)
    .fetch_all(pool)
    .await
    {
        Ok(rows) => rows,
        Err(error) => return database_error(error.to_string()),
    };
    let mut plugins = Vec::new();
    let mut skills = Vec::new();
    for row in installation_rows {
        let installation_id: String = row.try_get("id").unwrap_or_default();
        plugins.push(json!({
            "id": installation_id,
            "key": row.try_get::<String, _>("plugin_key").unwrap_or_default(),
            "name": row.try_get::<String, _>("name").unwrap_or_default(),
            "version": row.try_get::<String, _>("plugin_version").unwrap_or_default(),
            "protocol": row.try_get::<String, _>("protocol").unwrap_or_default(),
            "auth_required": row.try_get::<i64, _>("auth_required_int").unwrap_or(0) != 0,
            "credential_configured": !row.try_get::<String, _>("credential_ref").unwrap_or_default().is_empty(),
        }));
        let granted = sqlx::query_scalar::<_, String>(
            "SELECT granted_capabilities FROM core_agentplugininstallation WHERE CAST(id AS TEXT) = ?",
        )
        .bind(&installation_id)
        .fetch_optional(pool)
        .await
        .ok()
        .flatten()
        .and_then(|value| serde_json::from_str::<Value>(&value).ok())
        .and_then(|value| value.as_array().cloned())
        .unwrap_or_default();
        for capability in granted {
            if let Some(name) = capability.as_str() {
                skills.push(json!({"id": name, "name": name, "installation_id": installation_id}));
            }
        }
    }
    let projects = match sqlx::query(
        "SELECT CAST(id AS TEXT) AS id, name, slug FROM core_project \
         WHERE CAST(tenant_id AS TEXT) = ? ORDER BY name",
    )
    .bind(&tenant_id)
    .fetch_all(pool)
    .await
    {
        Ok(rows) => rows,
        Err(error) => return database_error(error.to_string()),
    };
    let project_items: Vec<Value> = projects
        .iter()
        .map(|row| {
            json!({
                "id": row.try_get::<String, _>("id").unwrap_or_default(),
                "name": row.try_get::<String, _>("name").unwrap_or_default(),
                "slug": row.try_get::<String, _>("slug").unwrap_or_default(),
            })
        })
        .collect();
    let mut project_payload = json!({});
    if let Some(item) = project_items
        .iter()
        .find(|item| item.get("id").and_then(Value::as_str) == Some(project_id.as_str()))
    {
        project_payload = item.clone();
    }
    let installed_keys: Vec<String> = plugins
        .iter()
        .filter_map(|plugin| {
            plugin
                .get("key")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .collect();
    let definition_rows = match sqlx::query(
        "SELECT CAST(plugin.id AS TEXT) AS id, plugin.plugin_key, plugin.name, \
         plugin.description, plugin.protocol, CAST(plugin.enabled AS INTEGER) AS enabled_int, \
         CAST(plugin.auth_required AS INTEGER) AS auth_required_int, plugin.capabilities \
         FROM core_agentplugindefinition plugin WHERE CAST(plugin.tenant_id AS TEXT) = ? \
         ORDER BY plugin.name",
    )
    .bind(&tenant_id)
    .fetch_all(pool)
    .await
    {
        Ok(rows) => rows,
        Err(error) => return database_error(error.to_string()),
    };
    let mut catalog = Vec::new();
    for row in definition_rows {
        let key: String = row.try_get("plugin_key").unwrap_or_default();
        if installed_keys.contains(&key) {
            continue;
        }
        let capabilities = parse_json(
            row.try_get::<Option<String>, _>("capabilities")
                .ok()
                .flatten(),
        )
        .and_then(|value| value.as_array().cloned())
        .unwrap_or_default()
        .iter()
        .filter_map(|entry| {
            entry
                .get("name")
                .and_then(Value::as_str)
                .map(str::to_string)
        })
        .collect::<Vec<String>>();
        catalog.push(json!({
            "key": key,
            "name": row.try_get::<String, _>("name").unwrap_or_default(),
            "description": row.try_get::<String, _>("description").unwrap_or_default(),
            "protocol": row.try_get::<String, _>("protocol").unwrap_or_default(),
            "enabled": row.try_get::<i64, _>("enabled_int").unwrap_or(0) != 0,
            "auth_required": row.try_get::<i64, _>("auth_required_int").unwrap_or(0) != 0,
            "capabilities": capabilities,
        }));
    }
    let models = match visible_models(pool, &state, &tenant_id, user.id).await {
        Ok(models) => models,
        Err(error) => return database_error(error),
    };
    Json(json!({
        "project": project_payload,
        "projects": project_items,
        "skills": skills,
        "plugins": plugins,
        "catalog": catalog,
        "models": models,
    }))
    .into_response()
}

struct ThreadRecord {
    id: String,
    tenant_id: String,
    project_id: String,
    title: String,
    model_config_id: Option<String>,
    archived: bool,
    execution_target: String,
    effort: String,
    goal: String,
    plan_first: bool,
    created_at: Option<String>,
    updated_at: Option<String>,
}

fn thread_json(record: &ThreadRecord) -> Value {
    json!({
        "id": record.id,
        "title": record.title,
        "project_id": record.project_id,
        "model_config_id": record.model_config_id,
        "archived": record.archived,
        "execution_target": record.execution_target,
        "effort": record.effort,
        "goal": record.goal,
        "plan_first": record.plan_first,
        "created_at": record.created_at.as_deref().map(normalize_timestamp),
        "updated_at": record.updated_at.as_deref().map(normalize_timestamp),
    })
}

async fn thread_row(
    pool: &AnyPool,
    state: &AppState,
    thread_id: &str,
) -> Result<Option<ThreadRecord>, String> {
    let created = timestamp(state, "thread", "created_at");
    let updated = timestamp(state, "thread", "updated_at");
    let sql = format!(
        "SELECT CAST(thread.id AS TEXT) AS id, CAST(thread.tenant_id AS TEXT) AS tenant_id, \
         CAST(thread.project_id AS TEXT) AS project_id, thread.title, \
         CAST(thread.model_config_id AS TEXT) AS model_config_id, \
         CAST(thread.archived AS INTEGER) AS archived_int, thread.execution_target, \
         thread.effort, thread.goal, CAST(thread.plan_first AS INTEGER) AS plan_first_int, \
         {created} AS created, {updated} AS updated FROM core_agentchatthread thread \
         WHERE CAST(thread.id AS TEXT) = ?"
    );
    let row = sqlx::query(&sql)
        .bind(thread_id)
        .fetch_optional(pool)
        .await
        .map_err(|error| error.to_string())?;
    Ok(row.map(|row| thread_from_row(&row)))
}

fn thread_from_row(row: &sqlx::any::AnyRow) -> ThreadRecord {
    ThreadRecord {
        id: row.try_get("id").unwrap_or_default(),
        tenant_id: row.try_get("tenant_id").unwrap_or_default(),
        project_id: row.try_get("project_id").unwrap_or_default(),
        title: row.try_get("title").unwrap_or_default(),
        model_config_id: row.try_get("model_config_id").ok(),
        archived: row.try_get::<i64, _>("archived_int").unwrap_or(0) != 0,
        execution_target: row.try_get("execution_target").unwrap_or_default(),
        effort: row.try_get("effort").unwrap_or_default(),
        goal: row.try_get("goal").unwrap_or_default(),
        plan_first: row.try_get::<i64, _>("plan_first_int").unwrap_or(0) != 0,
        created_at: row.try_get("created").ok(),
        updated_at: row.try_get("updated").ok(),
    }
}

async fn messages(pool: &AnyPool, state: &AppState, thread_id: &str) -> Result<Vec<Value>, String> {
    let created = timestamp(state, "message", "created_at");
    let context = json_text(state, "message.context");
    let usage = json_text(state, "message.usage");
    let tools = json_text(state, "message.tool_calls");
    let sql = format!(
        "SELECT CAST(message.id AS TEXT) AS id, message.role, message.content, \
         {context} AS context_text, {usage} AS usage_text, {tools} AS tools_text, \
         message.error_code, {created} AS created, author.username AS author \
         FROM core_agentchatmessage message LEFT JOIN auth_user author \
           ON author.id = message.author_id \
         WHERE CAST(message.thread_id AS TEXT) = ? ORDER BY message.created_at"
    );
    let rows = sqlx::query(&sql)
        .bind(thread_id)
        .fetch_all(pool)
        .await
        .map_err(|error| error.to_string())?;
    Ok(rows
        .iter()
        .map(|row| {
            let created_at: Option<String> = row.try_get("created").ok();
            json!({
                "id": row.try_get::<String, _>("id").unwrap_or_default(),
                "role": row.try_get::<String, _>("role").unwrap_or_default(),
                "content": row.try_get::<String, _>("content").unwrap_or_default(),
                "context": parse_json(row.try_get::<Option<String>, _>("context_text").ok().flatten()).unwrap_or_else(|| json!({})),
                "usage": parse_json(row.try_get::<Option<String>, _>("usage_text").ok().flatten()).unwrap_or_else(|| json!({})),
                "tool_calls": parse_json(row.try_get::<Option<String>, _>("tools_text").ok().flatten()).unwrap_or_else(|| json!([])),
                "error_code": row.try_get::<String, _>("error_code").unwrap_or_default(),
                "created_at": created_at.as_deref().map(normalize_timestamp),
                "author": row.try_get::<Option<String>, _>("author").ok().flatten(),
            })
        })
        .collect())
}

async fn last_message(
    pool: &AnyPool,
    state: &AppState,
    thread_id: &str,
) -> Result<Option<Value>, String> {
    let created = timestamp(state, "message", "created_at");
    let sql = format!(
        "SELECT message.role, message.content, {created} AS created \
         FROM core_agentchatmessage message WHERE CAST(message.thread_id AS TEXT) = ? \
         ORDER BY message.created_at DESC LIMIT 1"
    );
    let row = sqlx::query(&sql)
        .bind(thread_id)
        .fetch_optional(pool)
        .await
        .map_err(|error| error.to_string())?;
    Ok(row.map(|row| {
        let content: String = row.try_get("content").unwrap_or_default();
        let created_at: Option<String> = row.try_get("created").ok();
        json!({
            "role": row.try_get::<String, _>("role").unwrap_or_default(),
            "content": content.chars().take(240).collect::<String>(),
            "created_at": created_at.as_deref().map(normalize_timestamp),
        })
    }))
}

async fn attachments(
    pool: &AnyPool,
    state: &AppState,
    thread_id: &str,
) -> Result<Vec<Value>, String> {
    let created = timestamp(state, "attachment", "created_at");
    let sql = format!(
        "SELECT CAST(attachment.id AS TEXT) AS id, attachment.filename, \
         attachment.content_type, attachment.size_bytes, {created} AS created \
         FROM core_agentchatattachment attachment \
         WHERE CAST(attachment.thread_id AS TEXT) = ? ORDER BY attachment.created_at"
    );
    let rows = sqlx::query(&sql)
        .bind(thread_id)
        .fetch_all(pool)
        .await
        .map_err(|error| error.to_string())?;
    Ok(rows
        .iter()
        .map(|row| {
            let created_at: Option<String> = row.try_get("created").ok();
            json!({
                "id": row.try_get::<String, _>("id").unwrap_or_default(),
                "filename": row.try_get::<String, _>("filename").unwrap_or_default(),
                "content_type": row.try_get::<String, _>("content_type").unwrap_or_default(),
                "size_bytes": row.try_get::<i64, _>("size_bytes").unwrap_or_default(),
                "created_at": created_at.as_deref().map(normalize_timestamp),
            })
        })
        .collect())
}

async fn visible_models(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: &str,
    user_id: i64,
) -> Result<Vec<Value>, String> {
    let postgres = is_postgres(state);
    let true_literal = if postgres { "TRUE" } else { "1" };
    let capabilities = if postgres {
        "CAST(config.capabilities AS TEXT)"
    } else {
        "config.capabilities"
    };
    let sql = format!(
        "SELECT CAST(config.id AS TEXT) AS id, CAST(provider.id AS TEXT) AS provider_id, \
         provider.name AS provider_name, provider.provider_type, provider.auth_mode, \
         config.display_name AS name, config.model_name, {capabilities} AS capabilities, \
         config.context_window, config.max_output_tokens \
         FROM core_modelconfig config JOIN core_modelprovider provider \
           ON CAST(provider.id AS TEXT) = CAST(config.provider_id AS TEXT) \
         WHERE config.enabled = {true_literal} AND provider.enabled = {true_literal} \
           AND (CAST(provider.tenant_id AS TEXT) = ? OR provider.tenant_id IS NULL) \
           AND (provider.auth_mode <> 'user_account' OR provider.created_by_id = ?) \
         ORDER BY provider.name, config.model_name"
    );
    let rows = sqlx::query(&sql)
        .bind(tenant_id)
        .bind(user_id)
        .fetch_all(pool)
        .await
        .map_err(|error| error.to_string())?;
    Ok(rows
        .iter()
        .map(|row| json!({
            "id": row.try_get::<String, _>("id").unwrap_or_default(),
            "provider_id": row.try_get::<String, _>("provider_id").unwrap_or_default(),
            "provider_name": row.try_get::<String, _>("provider_name").unwrap_or_default(),
            "provider_type": row.try_get::<String, _>("provider_type").unwrap_or_default(),
            "name": row.try_get::<String, _>("name").unwrap_or_default(),
            "model_name": row.try_get::<String, _>("model_name").unwrap_or_default(),
            "capabilities": parse_json(row.try_get::<Option<String>, _>("capabilities").ok().flatten()).unwrap_or_else(|| json!([])),
            "context_window": row.try_get::<i64, _>("context_window").unwrap_or_default(),
            "max_output_tokens": row.try_get::<i64, _>("max_output_tokens").unwrap_or_default(),
        }))
        .collect())
}

async fn visible_model(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: &str,
    user_id: i64,
    model_id: &str,
) -> Result<bool, String> {
    let true_literal = if is_postgres(state) { "TRUE" } else { "1" };
    let sql = format!(
        "SELECT COUNT(*) FROM core_modelconfig config JOIN core_modelprovider provider \
           ON CAST(provider.id AS TEXT) = CAST(config.provider_id AS TEXT) \
         WHERE CAST(config.id AS TEXT) = ? AND config.enabled = {true_literal} \
           AND provider.enabled = {true_literal} \
           AND (CAST(provider.tenant_id AS TEXT) = ? OR provider.tenant_id IS NULL) \
           AND (provider.auth_mode <> 'user_account' OR provider.created_by_id = ?)"
    );
    let count = sqlx::query_scalar::<_, i64>(&sql)
        .bind(model_id)
        .bind(tenant_id)
        .bind(user_id)
        .fetch_one(pool)
        .await
        .map_err(|error| error.to_string())?;
    Ok(count > 0)
}

async fn default_model(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: &str,
    user_id: i64,
) -> Result<Option<String>, String> {
    let models = visible_models(pool, state, tenant_id, user_id).await?;
    let mut ranked: Vec<(i64, String)> = Vec::new();
    for model in models {
        let provider_type = model
            .get("provider_type")
            .and_then(Value::as_str)
            .unwrap_or("");
        let auth_mode = model.get("auth_mode").and_then(Value::as_str).unwrap_or("");
        let rank = if auth_mode == "user_account" {
            let provider_id = model
                .get("provider_id")
                .and_then(Value::as_str)
                .unwrap_or("");
            let linked = sqlx::query_scalar::<_, i64>(
                "SELECT COUNT(*) FROM core_provideraccountlink \
                 WHERE CAST(provider_id AS TEXT) = ? AND user_id = ? AND revoked_at IS NULL",
            )
            .bind(provider_id)
            .bind(user_id)
            .fetch_one(pool)
            .await
            .unwrap_or(0);
            if linked > 0 {
                0
            } else {
                continue;
            }
        } else if auth_mode == "platform_pool" {
            1
        } else if provider_type == "fake" {
            3
        } else {
            2
        };
        ranked.push((
            rank,
            model
                .get("id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string(),
        ));
    }
    ranked.sort();
    Ok(ranked.first().map(|(_, id)| id.clone()))
}

async fn model_id(payload: &Value) -> Result<Option<String>, Response> {
    match payload.get("model_config_id") {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) if value.is_empty() => Ok(None),
        Some(Value::String(value)) if uuid::Uuid::parse_str(value).is_ok() => {
            Ok(Some(value.clone()))
        }
        Some(_) => Err(legacy_error(
            "model_config_id must be a UUID.",
            StatusCode::BAD_REQUEST,
            "INVALID_MODEL",
        )),
    }
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

fn timestamp(state: &AppState, alias: &str, field: &str) -> String {
    if is_postgres(state) {
        format!("to_char({alias}.{field} AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')")
    } else {
        format!("strftime('%Y-%m-%dT%H:%M:%fZ', {alias}.{field})")
    }
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
            tracing::error!(%message, "rust chat api could not resolve the session");
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
        "The service is temporarily unavailable.",
        StatusCode::SERVICE_UNAVAILABLE,
        "UNAVAILABLE",
    )
}

fn database_error(error: String) -> Response {
    tracing::error!(%error, "rust chat api database operation failed");
    legacy_error(
        "The service is temporarily unavailable.",
        StatusCode::SERVICE_UNAVAILABLE,
        "DATABASE_ERROR",
    )
}
