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
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return database_error(error.to_string()),
    };
    for statement in [
        "DELETE FROM core_agentchatmessage WHERE CAST(thread_id AS TEXT) = ?",
        "DELETE FROM core_agentchatattachment WHERE CAST(thread_id AS TEXT) = ?",
        "DELETE FROM core_agentchatthread WHERE CAST(id AS TEXT) = ?",
    ] {
        if let Err(error) = sqlx::query(statement)
            .bind(&record.id)
            .execute(&mut *transaction)
            .await
        {
            let _ = transaction.rollback().await;
            return database_error(error.to_string());
        }
    }
    if let Err(error) = transaction.commit().await {
        return database_error(error.to_string());
    }
    StatusCode::NO_CONTENT.into_response()
}

pub async fn list_messages(
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
    let _ = record;
    match messages(pool, &state, &thread_id).await {
        Ok(items) => Json(json!({"items": items})).into_response(),
        Err(error) => database_error(error),
    }
}

struct ChatModel {
    id: String,
    provider_id: String,
    provider_name: String,
    model_name: String,
    provider_type: String,
    auth_mode: String,
    base_url: String,
    credential_ref: String,
    config: Value,
    max_output_tokens: i64,
}

async fn chat_model(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: &str,
    user_id: i64,
    model_id: &str,
) -> Result<Option<ChatModel>, String> {
    let true_literal = if is_postgres(state) { "TRUE" } else { "1" };
    let config_text = if is_postgres(state) {
        "CAST(provider.config AS TEXT)"
    } else {
        "provider.config"
    };
    let sql = format!(
        "SELECT CAST(config.id AS TEXT) AS id, CAST(provider.id AS TEXT) AS provider_id, \
         provider.name AS provider_name, config.model_name, provider.provider_type, \
         provider.auth_mode, provider.base_url, provider.credential_ref, \
         {config_text} AS config_text, config.max_output_tokens \
         FROM core_modelconfig config JOIN core_modelprovider provider \
           ON CAST(provider.id AS TEXT) = CAST(config.provider_id AS TEXT) \
         WHERE CAST(config.id AS TEXT) = ? AND config.enabled = {true_literal} \
           AND provider.enabled = {true_literal} \
           AND (CAST(provider.tenant_id AS TEXT) = ? OR provider.tenant_id IS NULL) \
           AND (provider.auth_mode <> 'user_account' OR provider.created_by_id = ?)"
    );
    let row = sqlx::query(&sql)
        .bind(model_id)
        .bind(tenant_id)
        .bind(user_id)
        .fetch_optional(pool)
        .await
        .map_err(|error| error.to_string())?;
    Ok(row.map(|row| ChatModel {
        id: row.try_get("id").unwrap_or_default(),
        provider_id: row.try_get("provider_id").unwrap_or_default(),
        provider_name: row.try_get("provider_name").unwrap_or_default(),
        model_name: row.try_get("model_name").unwrap_or_default(),
        provider_type: row.try_get("provider_type").unwrap_or_default(),
        auth_mode: row.try_get("auth_mode").unwrap_or_default(),
        base_url: row.try_get("base_url").unwrap_or_default(),
        credential_ref: row.try_get("credential_ref").unwrap_or_default(),
        config: parse_json(
            row.try_get::<Option<String>, _>("config_text")
                .ok()
                .flatten(),
        )
        .unwrap_or_else(|| json!({})),
        max_output_tokens: row.try_get::<i64, _>("max_output_tokens").unwrap_or(4096),
    }))
}

async fn chat_prompt(
    pool: &AnyPool,
    state: &AppState,
    thread: &ThreadRecord,
    context: &Value,
) -> Result<String, String> {
    let tenant_name = sqlx::query_scalar::<_, String>("SELECT name FROM core_tenant WHERE id = ?")
        .bind(&thread.tenant_id)
        .fetch_optional(pool)
        .await
        .map_err(|error| error.to_string())?
        .unwrap_or_default();
    let project_name =
        sqlx::query_scalar::<_, String>("SELECT name FROM core_project WHERE id = ?")
            .bind(&thread.project_id)
            .fetch_optional(pool)
            .await
            .map_err(|error| error.to_string())?
            .unwrap_or_default();
    let _ = state;
    let mut lines = vec![
        "You are the Control Agent of AI-Agent-Stack, a multi-agent platform.".to_string(),
        "You own the delivery chain (Cloud Control, Target and EPH agents), the agent marketplace,"
            .to_string(),
        "the skill catalog and the plugin installs. Answer as that operator.".to_string(),
        format!("Tenant: {tenant_name}"),
        format!("Project: {project_name}"),
    ];
    if !thread.goal.trim().is_empty() {
        lines.push(format!("Goal of this task: {}", thread.goal.trim()));
    }
    if thread.plan_first {
        lines.push("Answer with an execution plan first: numbered steps, the owner of each step, then the risks.".to_string());
    }
    if thread.execution_target == "local" {
        lines.push("Execution environment: the local machine workspace. Do not assume a cloud cluster is reachable.".to_string());
    } else {
        lines.push("Execution environment: the cloud control plane. Work is dispatched to Cloud Control, EPH and Target agents.".to_string());
    }
    if thread.effort == "low" {
        lines.push(
            "Reasoning level: low. Be brief, prefer the shortest answer that is still correct."
                .to_string(),
        );
    }
    if let Some(skills) = context.get("skills").and_then(Value::as_array) {
        if !skills.is_empty() {
            lines.push(format!(
                "Skills in scope for this conversation: {}",
                skills
                    .iter()
                    .filter_map(Value::as_str)
                    .collect::<Vec<_>>()
                    .join(", ")
            ));
        }
    }
    if let Some(files) = context.get("attachments").and_then(Value::as_array) {
        if !files.is_empty() {
            lines.push(format!(
                "Files attached by the member: {}",
                files
                    .iter()
                    .filter_map(Value::as_str)
                    .collect::<Vec<_>>()
                    .join(", ")
            ));
        }
    }
    lines.push("Be concrete: name the resources, the commands and the next step.".to_string());
    Ok(lines.join("\n"))
}

async fn resolve_credential(
    pool: &AnyPool,
    state: &AppState,
    model: &ChatModel,
    user_id: i64,
) -> Result<String, Response> {
    let mut reference = model.credential_ref.clone();
    if model.auth_mode == "user_account" {
        reference = match sqlx::query_scalar::<_, String>(
            "SELECT credential_ref FROM core_provideraccountlink WHERE user_id = ? \
             AND CAST(provider_id AS TEXT) = ? AND status = 'linked' ORDER BY linked_at DESC LIMIT 1",
        )
        .bind(user_id)
        .bind(&model.provider_id)
        .fetch_optional(pool)
        .await
        {
            Ok(Some(value)) => value,
            Ok(None) => {
                return Err(legacy_error(
                    "Link your own account on this provider before using it.",
                    StatusCode::CONFLICT,
                    "MODEL_CREDENTIAL_MISSING",
                ))
            }
            Err(error) => return Err(database_error(error.to_string())),
        };
    }
    if reference.is_empty() {
        return Ok(String::new());
    }
    if let Some(name) = reference
        .strip_prefix("env://")
        .or_else(|| reference.strip_prefix("env:"))
    {
        return Ok(std::env::var(name).unwrap_or_default());
    }
    if let Some(secret_id) = reference.strip_prefix("member://") {
        let sealed = sqlx::query_scalar::<_, String>(
            "SELECT sealed_value FROM core_membercredentialsecret WHERE id = ? AND user_id = ?",
        )
        .bind(secret_id)
        .bind(user_id)
        .fetch_optional(pool)
        .await;
        let sealed = match sealed {
            Ok(Some(value)) => value,
            Ok(None) => {
                return Err(legacy_error(
                    "Your saved platform credential could not be read. Link the account again.",
                    StatusCode::CONFLICT,
                    "MODEL_CREDENTIAL_MISSING",
                ))
            }
            Err(error) => return Err(database_error(error.to_string())),
        };
        let Some(secret_key) = state.config.django_secret_key.as_deref() else {
            return Err(unavailable());
        };
        return match crate::model_api::unseal_value(secret_key, &sealed, "member-credential-secret")
        {
            Ok(value) => Ok(value),
            Err(_) => Err(legacy_error(
                "Your saved platform credential could not be read. Link the account again.",
                StatusCode::CONFLICT,
                "MODEL_CREDENTIAL_MISSING",
            )),
        };
    }
    Ok(reference)
}

async fn complete_model(
    state: &AppState,
    model: &ChatModel,
    credential: &str,
    prompt: &str,
    history: &[Value],
) -> Result<(String, Value), Response> {
    if model.provider_type == "fake" {
        let content = model
            .config
            .get("response")
            .and_then(Value::as_str)
            .unwrap_or("Fake model response")
            .to_string();
        let usage = json!({
            "content": content,
            "input_tokens": 0,
            "output_tokens": 0,
            "provider": model.provider_name,
            "model": model.model_name,
            "latency_ms": 0,
            "tool_calls": [],
            "fallback_attempts": [],
        });
        return Ok((content, usage));
    }
    let mut messages = vec![json!({"role": "system", "content": prompt})];
    for message in history {
        let role = message.get("role").and_then(Value::as_str).unwrap_or("");
        let content = message.get("content").and_then(Value::as_str).unwrap_or("");
        if (role == "user" || role == "assistant") && !content.is_empty() {
            messages.push(json!({"role": role, "content": content}));
        }
    }
    let url = format!("{}/chat/completions", model.base_url.trim_end_matches('/'));
    let mut request = state
        .client
        .post(&url)
        .header("Accept", "application/json")
        .timeout(std::time::Duration::from_secs(30))
        .json(&json!({
            "model": model.model_name,
            "messages": messages,
            "max_tokens": model.max_output_tokens,
        }));
    if !credential.is_empty() {
        request = request.header("Authorization", format!("Bearer {credential}"));
    }
    let started = std::time::Instant::now();
    let response = match request.send().await {
        Ok(response) => response,
        Err(_) => {
            return Err(legacy_error(
                "The model provider could not be reached.",
                StatusCode::BAD_GATEWAY,
                "MODEL_PROVIDER_UNREACHABLE",
            ))
        }
    };
    let status = response.status();
    if !status.is_success() {
        return Err(legacy_error(
            &format!(
                "The model provider refused the request (HTTP {}).",
                status.as_u16()
            ),
            StatusCode::BAD_GATEWAY,
            "MODEL_PROVIDER_ERROR",
        ));
    }
    let document: Value = match response.json().await {
        Ok(document) => document,
        Err(_) => {
            return Err(legacy_error(
                "The model provider returned an unreadable response.",
                StatusCode::BAD_GATEWAY,
                "MODEL_PROVIDER_ERROR",
            ))
        }
    };
    let content = document
        .pointer("/choices/0/message/content")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    if content.is_empty() {
        return Err(legacy_error(
            "The model provider returned an empty response.",
            StatusCode::BAD_GATEWAY,
            "MODEL_PROVIDER_ERROR",
        ));
    }
    let input_tokens = document
        .pointer("/usage/prompt_tokens")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let output_tokens = document
        .pointer("/usage/completion_tokens")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let usage = json!({
        "content": content,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "provider": model.provider_name,
        "model": model.model_name,
        "latency_ms": started.elapsed().as_millis() as u64,
        "tool_calls": [],
        "fallback_attempts": [],
    });
    Ok((content, usage))
}

fn new_message_json(
    id: &str,
    role: &str,
    content: &str,
    author: Option<&str>,
    context: &Value,
    usage: &Value,
) -> Value {
    json!({
        "id": id,
        "role": role,
        "content": content,
        "context": context,
        "usage": usage,
        "tool_calls": [],
        "error_code": "",
        "created_at": normalize_timestamp(&Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false)),
        "author": author,
    })
}

pub async fn create_message(
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
    let thread = match thread_row(pool, &state, &thread_id).await {
        Ok(Some(thread))
            if tenant_member(pool, &thread.tenant_id, user.id)
                .await
                .unwrap_or(false) =>
        {
            thread
        }
        Ok(_) => return not_found(),
        Err(error) => return database_error(error),
    };
    let payload = match parse_object(&body) {
        Ok(payload) => payload,
        Err(response) => return response,
    };
    let content = match payload.get("content").and_then(Value::as_str) {
        Some(content) if !content.trim().is_empty() => content.trim().to_string(),
        _ => {
            return legacy_error(
                "content must be a non-empty string.",
                StatusCode::BAD_REQUEST,
                "INVALID_MESSAGE",
            )
        }
    };
    if content.chars().count() > 20000 {
        return legacy_error(
            "content must be at most 20000 characters.",
            StatusCode::BAD_REQUEST,
            "INVALID_MESSAGE",
        );
    }
    let context = match payload.get("context") {
        None | Some(Value::Null) => json!({
            "project_id": thread.project_id,
            "skills": [],
            "plugins": [],
            "attachments": [],
            "knowledge_documents": [],
        }),
        Some(Value::Object(map)) => Value::Object(map.clone()),
        Some(_) => {
            return legacy_error(
                "context must be an object.",
                StatusCode::BAD_REQUEST,
                "INVALID_CONTEXT",
            )
        }
    };
    let requested_model = payload
        .get("model_config_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string);
    let model_id = if let Some(model_id) = requested_model {
        match visible_model(pool, &state, &thread.tenant_id, user.id, &model_id).await {
            Ok(true) => {}
            Ok(false) => return not_found(),
            Err(error) => return database_error(error),
        }
        Some(model_id)
    } else if let Some(model_id) = thread.model_config_id.clone() {
        match visible_model(pool, &state, &thread.tenant_id, user.id, &model_id).await {
            Ok(true) => Some(model_id),
            Ok(false) => default_model(pool, &state, &thread.tenant_id, user.id)
                .await
                .ok()
                .flatten(),
            Err(error) => return database_error(error),
        }
    } else {
        match default_model(pool, &state, &thread.tenant_id, user.id).await {
            Ok(model) => model,
            Err(error) => return database_error(error),
        }
    };
    let Some(model_id) = model_id else {
        return legacy_error(
            "Configure a model provider for this tenant, or link your own account on a marketplace platform.",
            StatusCode::CONFLICT,
            "MODEL_NOT_CONFIGURED",
        );
    };
    let model = match chat_model(pool, &state, &thread.tenant_id, user.id, &model_id).await {
        Ok(Some(model)) => model,
        Ok(None) => return not_found(),
        Err(error) => return database_error(error),
    };
    let credential = match resolve_credential(pool, &state, &model, user.id).await {
        Ok(credential) => credential,
        Err(response) => return response,
    };
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    let user_message_id = uuid::Uuid::new_v4().to_string();
    let mut title = thread.title.clone();
    if title.is_empty() || title == "New conversation" {
        title = content
            .lines()
            .next()
            .unwrap_or("")
            .chars()
            .take(60)
            .collect();
    }
    let postgres = is_postgres(&state);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return database_error(error.to_string()),
    };
    if let Err(error) = sqlx::query(
        "INSERT INTO core_agentchatmessage (id, thread_id, role, content, author_id, context, \
         usage, tool_calls, error_code, created_at) VALUES (?, ?, 'user', ?, ?, ?, '{}', '[]', '', ?)",
    )
    .bind(&user_message_id)
    .bind(&thread.id)
    .bind(&content)
    .bind(user.id)
    .bind(serde_json::to_string(&context).unwrap_or_else(|_| "{}".to_string()))
    .bind(&now)
    .execute(&mut *transaction)
    .await
    {
        let _ = transaction.rollback().await;
        return database_error(error.to_string());
    }
    if let Err(error) = sqlx::query(
        "UPDATE core_agentchatthread SET title = ?, model_config_id = ?, updated_at = ? \
         WHERE CAST(id AS TEXT) = ?",
    )
    .bind(&title)
    .bind(&model.id)
    .bind(&now)
    .bind(&thread.id)
    .execute(&mut *transaction)
    .await
    {
        let _ = transaction.rollback().await;
        return database_error(error.to_string());
    }
    if let Err(error) = transaction.commit().await {
        return database_error(error.to_string());
    }
    let prompt = match chat_prompt(pool, &state, &thread, &context).await {
        Ok(prompt) => prompt,
        Err(error) => return database_error(error),
    };
    let history = match messages(pool, &state, &thread.id).await {
        Ok(items) => items
            .into_iter()
            .rev()
            .take(20)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect::<Vec<_>>(),
        Err(error) => return database_error(error),
    };
    let (reply, usage) = match complete_model(&state, &model, &credential, &prompt, &history).await
    {
        Ok(result) => result,
        Err(response) => return response,
    };
    let assistant_id = uuid::Uuid::new_v4().to_string();
    let assistant_now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    let usage_text = serde_json::to_string(&usage).unwrap_or_else(|_| "{}".to_string());
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return database_error(error.to_string()),
    };
    if let Err(error) = sqlx::query(
        "INSERT INTO core_agentchatmessage (id, thread_id, role, content, author_id, context, \
         usage, tool_calls, error_code, created_at) VALUES (?, ?, 'assistant', ?, NULL, '{}', ?, '[]', '', ?)",
    )
    .bind(&assistant_id)
    .bind(&thread.id)
    .bind(&reply)
    .bind(&usage_text)
    .bind(&assistant_now)
    .execute(&mut *transaction)
    .await
    {
        let _ = transaction.rollback().await;
        return database_error(error.to_string());
    }
    if let Err(error) =
        sqlx::query("UPDATE core_agentchatthread SET updated_at = ? WHERE CAST(id AS TEXT) = ?")
            .bind(&assistant_now)
            .bind(&thread.id)
            .execute(&mut *transaction)
            .await
    {
        let _ = transaction.rollback().await;
        return database_error(error.to_string());
    }
    if let Err(error) = sqlx::query(
        "UPDATE core_agentchatthread SET model_config_id = ? WHERE CAST(id AS TEXT) = ?",
    )
    .bind(&model.id)
    .bind(&thread.id)
    .execute(&mut *transaction)
    .await
    {
        let _ = transaction.rollback().await;
        return database_error(error.to_string());
    }
    if let Err(error) = crate::audit::record_event_in_transaction(
        &mut transaction,
        postgres,
        &crate::audit::AuditEvent {
            tenant_id: Some(&thread.tenant_id),
            project_id: Some(&thread.project_id),
            event_type: "agent.chat.message",
            resource_type: "agent_chat_thread",
            resource_id: &thread.id,
            outcome: "success",
            actor_id: Some(user.id),
            request_id: headers
                .get("x-request-id")
                .and_then(|value| value.to_str().ok())
                .unwrap_or(""),
            remote_addr: None,
            user_agent: headers
                .get("user-agent")
                .and_then(|value| value.to_str().ok())
                .unwrap_or(""),
            metadata: json!({"thread_id": thread.id, "model_config_id": model.id}),
        },
    )
    .await
    {
        let _ = transaction.rollback().await;
        return database_error(error);
    }
    if let Err(error) = transaction.commit().await {
        return database_error(error.to_string());
    }
    let reply_context = json!({});
    let response = json!({
        "user": new_message_json(&user_message_id, "user", &content, Some(&user.username), &context, &json!({})),
        "assistant": new_message_json(&assistant_id, "assistant", &reply, None, &reply_context, &usage),
    });
    (StatusCode::CREATED, Json(response)).into_response()
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

pub async fn test_model(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, model_id)): Path<(String, String)>,
    payload: Option<Json<Value>>,
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
    let model = match chat_model(pool, &state, &tenant_id, user.id, &model_id).await {
        Ok(Some(model)) => model,
        Ok(None) => return not_found(),
        Err(error) => return database_error(error),
    };
    let linked_account = model.auth_mode == "user_account";
    if linked_account {
        if model.auth_mode == "user_account"
            && sqlx::query_scalar::<_, i64>(
                "SELECT COUNT(*) FROM core_provideraccountlink WHERE user_id = ? \
                 AND CAST(provider_id AS TEXT) = ? AND status = 'linked'",
            )
            .bind(user.id)
            .bind(&model.provider_id)
            .fetch_one(pool)
            .await
            .unwrap_or(0)
                == 0
        {
            return legacy_error(
                "Link your own account on this platform before testing its models.",
                StatusCode::FORBIDDEN,
                "PROVIDER_ACCOUNT_NOT_LINKED",
            );
        }
    } else if !crate::tenancy::tenant_admin(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return legacy_error(
            "Tenant administrator permission required.",
            StatusCode::FORBIDDEN,
            "TENANT_ADMIN_REQUIRED",
        );
    }
    let credential = match resolve_credential(pool, &state, &model, user.id).await {
        Ok(credential) => credential,
        Err(response) => return response,
    };
    let messages = match payload
        .as_ref()
        .and_then(|Json(value)| value.get("messages"))
    {
        Some(Value::Array(messages)) if !messages.is_empty() => messages.clone(),
        Some(Value::Array(_)) => {
            return legacy_error(
                "messages must contain at least one message",
                StatusCode::BAD_REQUEST,
                "INVALID_MESSAGES",
            )
        }
        Some(_) => {
            return legacy_error(
                "messages must be a list",
                StatusCode::BAD_REQUEST,
                "INVALID_MESSAGES",
            )
        }
        None => vec![json!({
            "role": "user",
            "content": "Reply with a short connection test.",
        })],
    };
    let (content, usage) = match complete_model(
        &state,
        &model,
        &credential,
        "You are a model connectivity probe. Reply briefly.",
        &messages,
    )
    .await
    {
        Ok(result) => result,
        Err(response) => {
            model_test_audit(
                &state,
                pool,
                &tenant_id,
                &headers,
                &model.id,
                "failure",
                json!({"error": "MODEL_PROVIDER_ERROR"}),
            )
            .await;
            return response;
        }
    };
    model_test_audit(
        &state,
        pool,
        &tenant_id,
        &headers,
        &model.id,
        "success",
        json!({"provider_id": model.provider_id, "model_name": model.model_name}),
    )
    .await;
    let mut result = usage;
    if linked_account {
        result["billing"] = json!("linked_account_free_quota");
    }
    result["content"] = json!(content);
    (StatusCode::OK, Json(result)).into_response()
}

async fn model_test_audit(
    state: &AppState,
    pool: &sqlx::AnyPool,
    tenant_id: &str,
    headers: &HeaderMap,
    model_id: &str,
    outcome: &str,
    metadata: Value,
) {
    let Some(database_url) = state.config.database_url.as_deref() else {
        return;
    };
    let request_id = headers
        .get("x-request-id")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("");
    let user_agent = headers
        .get("user-agent")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("");
    let event = crate::audit::AuditEvent {
        tenant_id: Some(tenant_id),
        project_id: None,
        event_type: "model.config.tested",
        resource_type: "model_config",
        resource_id: model_id,
        outcome,
        actor_id: None,
        request_id,
        remote_addr: None,
        user_agent,
        metadata,
    };
    if let Err(error) = crate::audit::record_event(pool, database_url, event).await {
        tracing::error!(%error, "rust model test could not append the audit event");
    }
}
