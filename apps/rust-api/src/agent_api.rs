#![allow(clippy::result_large_err)]

use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use chrono::{SecondsFormat, Utc};
use serde_json::{json, Map, Value};
use sqlx::{AnyPool, Row};

use crate::audit::{record_event_in_transaction, AuditEvent};
use crate::context::{authenticate, AuthError};
use crate::proxy::AppState;
use crate::tenancy::{is_postgres, normalize_timestamp, project_access, tenant_admin};

pub async fn list_agents(
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
    match agent_rows(pool, &state, &tenant_id, &project_id, None, false).await {
        Ok(rows) => Json(rows.iter().map(agent_json).collect::<Vec<_>>()).into_response(),
        Err(error) => database_error(error),
    }
}

pub async fn create_agent(
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
    if !can_edit(pool, &tenant_id, &project_id, user.id)
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
    let name = match text_field(&payload, "name", 120, true) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let slug = match text_field(&payload, "slug", 80, true) {
        Ok(value) => value,
        Err(response) => return response,
    };
    if !valid_slug(&slug) {
        return validation_error(
            "slug",
            "Enter a valid \"slug\" consisting of letters, numbers, underscores or hyphens.",
        );
    }
    let description = match text_field(&payload, "description", 4000, false) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let draft_config = match draft_config(&payload) {
        Ok(value) => value,
        Err(response) => return response,
    };
    let duplicate = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_agentdefinition \
         WHERE CAST(project_id AS TEXT) = ? AND slug = ?",
    )
    .bind(&project_id)
    .bind(&slug)
    .fetch_one(pool)
    .await
    .unwrap_or(0);
    if duplicate > 0 {
        return validation_error(
            "slug",
            "Agent with this slug already exists in this project.",
        );
    }
    let agent_id = uuid::Uuid::new_v4().to_string();
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    let postgres = is_postgres(&state);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return sqlx_error(error),
    };
    let insert = if postgres {
        "INSERT INTO core_agentdefinition (id, tenant_id, project_id, created_by_id, name, slug, \
         description, draft_config, current_version, is_archived, created_at, updated_at) \
         VALUES (?::uuid, ?::uuid, ?::uuid, ?, ?, ?, ?, ?::jsonb, 0, FALSE, ?::timestamptz, ?::timestamptz)"
    } else {
        "INSERT INTO core_agentdefinition (id, tenant_id, project_id, created_by_id, name, slug, \
         description, draft_config, current_version, is_archived, created_at, updated_at) \
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)"
    };
    if let Err(error) = sqlx::query(insert)
        .bind(&agent_id)
        .bind(&tenant_id)
        .bind(&project_id)
        .bind(user.id)
        .bind(&name)
        .bind(&slug)
        .bind(&description)
        .bind(Value::Object(draft_config).to_string())
        .bind(&now)
        .bind(&now)
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
            event_type: "agent.created",
            resource_type: "agent_definition",
            resource_id: &agent_id,
            outcome: "success",
            actor_id: Some(user.id),
            request_id: header(&headers, "x-request-id"),
            remote_addr: None,
            user_agent: header(&headers, "user-agent"),
            metadata: json!({"slug": slug, "name": name, "project_id": project_id}),
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
    match agent_rows(
        pool,
        &state,
        &tenant_id,
        &project_id,
        Some(&agent_id),
        false,
    )
    .await
    {
        Ok(rows) => match rows.first() {
            Some(record) => (StatusCode::CREATED, Json(agent_json(record))).into_response(),
            None => not_found(),
        },
        Err(error) => database_error(error),
    }
}

pub async fn get_agent(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(agent_id): Path<String>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    match agent_access(pool, &state, &agent_id, user.id).await {
        Ok(Some(record)) => Json(agent_json(&record)).into_response(),
        Ok(None) => not_found(),
        Err(error) => database_error(error),
    }
}

pub async fn update_agent(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(agent_id): Path<String>,
    body: Bytes,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let Some(record) = (match agent_access(pool, &state, &agent_id, user.id).await {
        Ok(record) => record,
        Err(error) => return database_error(error),
    }) else {
        return not_found();
    };
    if !can_edit(pool, &record.tenant_id, &record.project_id, user.id)
        .await
        .unwrap_or(false)
    {
        return legacy_error(
            "Project editor permission required.",
            StatusCode::FORBIDDEN,
            "PERMISSION_DENIED",
        );
    }
    if record.is_archived {
        return legacy_error(
            "Archived agents are read-only.",
            StatusCode::CONFLICT,
            "AGENT_ARCHIVED",
        );
    }
    let payload = match parse_object(&body) {
        Ok(payload) => payload,
        Err(response) => return response,
    };
    let mut fields: Vec<&str> = Vec::new();
    let mut name = record.name.clone();
    let mut slug = record.slug.clone();
    let mut description = record.description.clone();
    let mut draft = record.draft_config.as_object().cloned().unwrap_or_default();
    if payload.get("name").is_some() {
        name = match text_field(&payload, "name", 120, true) {
            Ok(value) => value,
            Err(response) => return response,
        };
        fields.push("name");
    }
    if payload.get("slug").is_some() {
        slug = match text_field(&payload, "slug", 80, true) {
            Ok(value) => value,
            Err(response) => return response,
        };
        if !valid_slug(&slug) {
            return validation_error("slug", "Enter a valid \"slug\".");
        }
        fields.push("slug");
    }
    if payload.get("description").is_some() {
        description = match text_field(&payload, "description", 4000, false) {
            Ok(value) => value,
            Err(response) => return response,
        };
        fields.push("description");
    }
    if payload.get("draft_config").is_some() {
        draft = match draft_config(&payload) {
            Ok(value) => value,
            Err(response) => return response,
        };
        fields.push("draft_config");
    }
    if slug != record.slug {
        let duplicate = sqlx::query_scalar::<_, i64>(
            "SELECT COUNT(*) FROM core_agentdefinition \
             WHERE CAST(project_id AS TEXT) = ? AND slug = ? AND CAST(id AS TEXT) <> ?",
        )
        .bind(&record.project_id)
        .bind(&slug)
        .bind(&agent_id)
        .fetch_one(pool)
        .await
        .unwrap_or(0);
        if duplicate > 0 {
            return validation_error(
                "slug",
                "Agent with this slug already exists in this project.",
            );
        }
    }
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    let postgres = is_postgres(&state);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return sqlx_error(error),
    };
    let update = if postgres {
        "UPDATE core_agentdefinition SET name = ?, slug = ?, description = ?, \
         draft_config = ?::jsonb, updated_at = ?::timestamptz WHERE CAST(id AS TEXT) = ?"
    } else {
        "UPDATE core_agentdefinition SET name = ?, slug = ?, description = ?, \
         draft_config = ?, updated_at = ? WHERE CAST(id AS TEXT) = ?"
    };
    if let Err(error) = sqlx::query(update)
        .bind(&name)
        .bind(&slug)
        .bind(&description)
        .bind(Value::Object(draft).to_string())
        .bind(&now)
        .bind(&agent_id)
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
            tenant_id: Some(&record.tenant_id),
            project_id: Some(&record.project_id),
            event_type: "agent.updated",
            resource_type: "agent_definition",
            resource_id: &agent_id,
            outcome: "success",
            actor_id: Some(user.id),
            request_id: header(&headers, "x-request-id"),
            remote_addr: None,
            user_agent: header(&headers, "user-agent"),
            metadata: json!({
                "fields": fields,
                "previous_current_version": record.current_version,
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
    match agent_rows(
        pool,
        &state,
        &record.tenant_id,
        &record.project_id,
        Some(&agent_id),
        false,
    )
    .await
    {
        Ok(rows) => match rows.first() {
            Some(record) => Json(agent_json(record)).into_response(),
            None => not_found(),
        },
        Err(error) => database_error(error),
    }
}

struct AgentRecord {
    id: String,
    tenant_id: String,
    project_id: String,
    name: String,
    slug: String,
    description: String,
    draft_config: Value,
    current_version: i64,
    is_archived: bool,
    created_at: Option<String>,
    updated_at: Option<String>,
}

fn agent_json(record: &AgentRecord) -> Value {
    json!({
        "id": record.id,
        "tenant_id": record.tenant_id,
        "project_id": record.project_id,
        "name": record.name,
        "slug": record.slug,
        "description": record.description,
        "draft_config": record.draft_config,
        "current_version": record.current_version,
        "is_archived": record.is_archived,
        "created_at": record.created_at.as_deref().map(normalize_timestamp),
        "updated_at": record.updated_at.as_deref().map(normalize_timestamp),
    })
}

async fn agent_rows(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: &str,
    project_id: &str,
    agent_id: Option<&str>,
    include_archived: bool,
) -> Result<Vec<AgentRecord>, String> {
    let postgres = is_postgres(state);
    let false_literal = if postgres { "FALSE" } else { "0" };
    let draft = json_text(postgres, "agent.draft_config");
    let created = timestamp(state, "agent", "created_at");
    let updated = timestamp(state, "agent", "updated_at");
    let archived_clause = if include_archived {
        String::new()
    } else {
        format!(" AND agent.is_archived = {false_literal}")
    };
    let base = format!(
        "SELECT CAST(agent.id AS TEXT) AS id, CAST(agent.tenant_id AS TEXT) AS tenant_id, \
         CAST(agent.project_id AS TEXT) AS project_id, agent.name, agent.slug, agent.description, \
         {draft} AS draft_text, agent.current_version, CAST(agent.is_archived AS INTEGER) AS archived_int, \
         {created} AS created, {updated} AS updated FROM core_agentdefinition agent \
         WHERE CAST(agent.tenant_id AS TEXT) = ? AND CAST(agent.project_id AS TEXT) = ?{archived_clause}"
    );
    let rows = if let Some(id) = agent_id {
        let sql = format!("{base} AND CAST(agent.id AS TEXT) = ?");
        sqlx::query(&sql)
            .bind(tenant_id)
            .bind(project_id)
            .bind(id)
            .fetch_all(pool)
            .await
    } else {
        let sql = format!("{base} ORDER BY agent.name");
        sqlx::query(&sql)
            .bind(tenant_id)
            .bind(project_id)
            .fetch_all(pool)
            .await
    }
    .map_err(|error| error.to_string())?;
    Ok(rows
        .iter()
        .map(|row| AgentRecord {
            id: row.try_get("id").unwrap_or_default(),
            tenant_id: row.try_get("tenant_id").unwrap_or_default(),
            project_id: row.try_get("project_id").unwrap_or_default(),
            name: row.try_get("name").unwrap_or_default(),
            slug: row.try_get("slug").unwrap_or_default(),
            description: row.try_get("description").unwrap_or_default(),
            draft_config: parse_json(
                row.try_get::<Option<String>, _>("draft_text")
                    .ok()
                    .flatten(),
            )
            .unwrap_or_else(|| json!({})),
            current_version: row.try_get("current_version").unwrap_or_default(),
            is_archived: row.try_get::<i64, _>("archived_int").unwrap_or_default() != 0,
            created_at: row.try_get("created").ok(),
            updated_at: row.try_get("updated").ok(),
        })
        .collect())
}

async fn agent_access(
    pool: &AnyPool,
    state: &AppState,
    agent_id: &str,
    user_id: i64,
) -> Result<Option<AgentRecord>, String> {
    let row = sqlx::query(
        "SELECT CAST(agent.tenant_id AS TEXT) AS tenant_id, CAST(agent.project_id AS TEXT) AS project_id \
         FROM core_agentdefinition agent JOIN core_tenantmembership membership \
           ON membership.tenant_id = agent.tenant_id \
         WHERE CAST(agent.id AS TEXT) = ? AND membership.user_id = ? LIMIT 1",
    )
    .bind(agent_id)
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())?;
    let Some(row) = row else {
        return Ok(None);
    };
    let tenant_id: String = row.try_get("tenant_id").unwrap_or_default();
    let project_id: String = row.try_get("project_id").unwrap_or_default();
    if !project_access(pool, &tenant_id, &project_id, user_id)
        .await
        .unwrap_or(false)
    {
        return Ok(None);
    }
    let rows = agent_rows(pool, state, &tenant_id, &project_id, Some(agent_id), true).await?;
    Ok(rows.into_iter().next())
}

async fn can_edit(
    pool: &AnyPool,
    tenant_id: &str,
    project_id: &str,
    user_id: i64,
) -> Result<bool, String> {
    if tenant_admin(pool, tenant_id, user_id).await? {
        return Ok(true);
    }
    let role = sqlx::query_scalar::<_, String>(
        "SELECT role FROM core_projectmembership WHERE CAST(project_id AS TEXT) = ? AND user_id = ?",
    )
    .bind(project_id)
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())?;
    Ok(matches!(role.as_deref(), Some("admin") | Some("editor")))
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
            tracing::error!(%message, "rust agent api could not resolve the session");
            Err(unavailable())
        }
    }
}

fn parse_object(body: &Bytes) -> Result<Value, Response> {
    match serde_json::from_slice::<Value>(body) {
        Ok(value) if value.is_object() => Ok(value),
        _ => Err(validation_error(
            "non_field_errors",
            "A valid JSON object is required.",
        )),
    }
}

fn text_field(
    payload: &Value,
    field: &str,
    max: usize,
    required: bool,
) -> Result<String, Response> {
    match payload.get(field) {
        None if required => Err(validation_error(field, "This field is required.")),
        None => Ok(String::new()),
        Some(Value::String(value)) if value.chars().count() <= max => {
            if required && value.trim().is_empty() {
                Err(validation_error(field, "This field may not be blank."))
            } else {
                Ok(value.clone())
            }
        }
        Some(Value::String(_)) => Err(validation_error(
            field,
            &format!("Ensure this field has no more than {max} characters."),
        )),
        Some(_) => Err(validation_error(field, "Not a valid string.")),
    }
}

fn draft_config(payload: &Value) -> Result<Map<String, Value>, Response> {
    match payload.get("draft_config") {
        None => Ok(Map::new()),
        Some(Value::Object(map)) => {
            if serde_json::to_string(&Value::Object(map.clone()))
                .map(|text| text.len() > 32768)
                .unwrap_or(true)
            {
                return Err(validation_error(
                    "draft_config",
                    "Draft configuration exceeds 32 KiB.",
                ));
            }
            if contains_secret(&Value::Object(map.clone())) {
                return Err(validation_error(
                    "draft_config",
                    "Credentials must use credential references, not draft configuration.",
                ));
            }
            Ok(map.clone())
        }
        Some(_) => Err(validation_error(
            "draft_config",
            "Draft configuration must be a JSON object.",
        )),
    }
}

fn contains_secret(value: &Value) -> bool {
    const SECRET_NAMES: [&str; 5] = [
        "api_key",
        "secret",
        "password",
        "access_token",
        "client_secret",
    ];
    match value {
        Value::Object(map) => map.iter().any(|(key, child)| {
            SECRET_NAMES.contains(&key.to_lowercase().as_str()) || contains_secret(child)
        }),
        Value::Array(items) => items.iter().any(contains_secret),
        _ => false,
    }
}

fn valid_slug(value: &str) -> bool {
    let length = value.len();
    !value.is_empty()
        && length <= 80
        && value.chars().all(|character| {
            character.is_ascii_alphanumeric() || character == '_' || character == '-'
        })
}

fn timestamp(state: &AppState, alias: &str, field: &str) -> String {
    if is_postgres(state) {
        format!("to_char({alias}.{field} AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')")
    } else {
        format!("strftime('%Y-%m-%dT%H:%M:%fZ', {alias}.{field})")
    }
}

fn json_text(postgres: bool, expression: &str) -> String {
    if postgres {
        format!("{expression}::text")
    } else {
        expression.to_string()
    }
}

fn parse_json(value: Option<String>) -> Option<Value> {
    value.and_then(|text| serde_json::from_str::<Value>(&text).ok())
}

fn validation_error(field: &str, message: &str) -> Response {
    let details = json!({field: [message]});
    (
        StatusCode::BAD_REQUEST,
        Json(json!({"error": {"code": "INVALID_REQUEST", "message": "Request could not be processed.", "details": details}})),
    )
        .into_response()
}

fn legacy_error(message: &str, status: StatusCode, code: &str) -> Response {
    (
        status,
        Json(json!({"error": {"code": code, "message": message, "details": {}}})),
    )
        .into_response()
}

fn header<'a>(headers: &'a HeaderMap, name: &str) -> &'a str {
    headers
        .get(name)
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
}

fn not_found() -> Response {
    (StatusCode::NOT_FOUND, Json(json!({"detail": "Not found."}))).into_response()
}

fn unavailable() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({"detail": "The service is temporarily unavailable."})),
    )
        .into_response()
}

fn sqlx_error(error: sqlx::Error) -> Response {
    tracing::error!(%error, "rust agent api transaction failed");
    unavailable()
}

fn database_error(message: String) -> Response {
    tracing::error!(%message, "rust agent api database operation failed");
    unavailable()
}
