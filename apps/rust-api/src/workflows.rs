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
use crate::tenancy::{is_postgres, normalize_timestamp, project_access, tenant_role};

pub(crate) struct WorkflowRecord {
    pub(crate) id: String,
    pub(crate) tenant_id: String,
    pub(crate) project_id: String,
    pub(crate) name: String,
    pub(crate) slug: String,
    pub(crate) description: String,
    pub(crate) draft_definition: Value,
    pub(crate) current_version: i64,
    pub(crate) is_archived: bool,
    pub(crate) created_at: Option<String>,
    pub(crate) updated_at: Option<String>,
}

impl WorkflowRecord {
    fn to_json(&self) -> Value {
        json!({
            "id": self.id,
            "tenant_id": self.tenant_id,
            "project_id": self.project_id,
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
            "draft_definition": self.draft_definition,
            "current_version": self.current_version,
            "is_archived": self.is_archived,
            "created_at": self.created_at.as_deref().map(normalize_timestamp),
            "updated_at": self.updated_at.as_deref().map(normalize_timestamp),
        })
    }
}

/// Native replacement for workflow draft list/create/detail/update.
pub async fn list_workflows(
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
    if !match project_access(pool, &tenant_id, &project_id, user.id).await {
        Ok(access) => access,
        Err(error) => return database_error_message(error),
    } {
        return not_found();
    }
    match workflow_rows(pool, &state, Some(&tenant_id), Some(&project_id), None).await {
        Ok(records) => Json(
            records
                .iter()
                .map(WorkflowRecord::to_json)
                .collect::<Vec<Value>>(),
        )
        .into_response(),
        Err(error) => database_error_message(error),
    }
}

pub async fn create_workflow(
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
    let role = match tenant_role(pool, &tenant_id, user.id).await {
        Ok(Some(role)) => role,
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    };
    if !match project_access(pool, &tenant_id, &project_id, user.id).await {
        Ok(access) => access,
        Err(error) => return database_error_message(error),
    } {
        return not_found();
    }
    if !editor(role.as_str(), pool, &project_id, user.id).await {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"detail": "Project editor permission required."})),
        )
            .into_response();
    }
    let payload = match parse_body(&body) {
        Ok(payload) => payload,
        Err(response) => return response,
    };
    let name = match required_string(&payload, "name", 120) {
        Ok(name) => name,
        Err(response) => return response,
    };
    let slug = match required_slug(&payload, "slug", 80) {
        Ok(slug) => slug,
        Err(response) => return response,
    };
    let description = payload
        .get("description")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string();
    let draft = match draft_definition(&payload) {
        Ok(draft) => draft,
        Err(response) => return response,
    };
    let existing = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_workflowdefinition \
         WHERE CAST(project_id AS TEXT) = ? AND slug = ?",
    )
    .bind(&project_id)
    .bind(&slug)
    .fetch_one(pool)
    .await;
    if matches!(existing, Ok(value) if value > 0) {
        return validation_error("slug", "workflow with this slug already exists.");
    }
    let workflow_id = uuid::Uuid::new_v4().to_string();
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    let postgres = is_postgres(&state);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return sqlx_error(error),
    };
    let insert = if postgres {
        "INSERT INTO core_workflowdefinition (\
         id, tenant_id, project_id, created_by_id, name, slug, description, \
         draft_definition, current_version, is_archived, created_at, updated_at) \
         VALUES (?::uuid, ?::uuid, ?::uuid, ?, ?, ?, ?, ?::jsonb, 0, FALSE, \
         ?::timestamptz, ?::timestamptz)"
    } else {
        "INSERT INTO core_workflowdefinition (\
         id, tenant_id, project_id, created_by_id, name, slug, description, \
         draft_definition, current_version, is_archived, created_at, updated_at) \
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)"
    };
    if let Err(error) = sqlx::query(insert)
        .bind(&workflow_id)
        .bind(&tenant_id)
        .bind(&project_id)
        .bind(user.id)
        .bind(&name)
        .bind(&slug)
        .bind(&description)
        .bind(draft.to_string())
        .bind(&now)
        .bind(&now)
        .execute(&mut *transaction)
        .await
    {
        return sqlx_error(error);
    }
    if let Err(error) = record_event_in_transaction(
        &mut transaction,
        postgres,
        &AuditEvent {
            tenant_id: Some(&tenant_id),
            project_id: Some(&project_id),
            event_type: "workflow.created",
            resource_type: "workflow_definition",
            resource_id: &workflow_id,
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
        return database_error_message(error);
    }
    if let Err(error) = transaction.commit().await {
        return sqlx_error(error);
    }
    match fetch_workflow(pool, &state, &workflow_id).await {
        Ok(Some(record)) => (StatusCode::CREATED, Json(record.to_json())).into_response(),
        Ok(None) => not_found(),
        Err(error) => database_error_message(error),
    }
}

pub async fn get_workflow(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(workflow_id): Path<String>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    match workflow_access(pool, &state, &workflow_id, user.id).await {
        Ok(Some(record)) => Json(record.to_json()).into_response(),
        Ok(None) => not_found(),
        Err(error) => database_error_message(error),
    }
}

pub async fn update_workflow(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(workflow_id): Path<String>,
    body: Bytes,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let record = match workflow_access(pool, &state, &workflow_id, user.id).await {
        Ok(Some(record)) => record,
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    };
    let role = match tenant_role(pool, &record.tenant_id, user.id).await {
        Ok(Some(role)) => role,
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    };
    if !editor(role.as_str(), pool, &record.project_id, user.id).await {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"detail": "Project editor permission required."})),
        )
            .into_response();
    }
    if record.is_archived {
        return (
            StatusCode::CONFLICT,
            Json(json!({"detail": "Archived workflows are read-only."})),
        )
            .into_response();
    }
    let payload = match parse_body(&body) {
        Ok(payload) => payload,
        Err(response) => return response,
    };
    let mut changes = Map::new();
    if let Some(value) = payload.get("name") {
        let name = match string_value(value, "name", 120) {
            Ok(name) => name,
            Err(response) => return response,
        };
        changes.insert("name".to_string(), json!(name));
    }
    if let Some(value) = payload.get("slug") {
        let slug = match slug_value(value, "slug", 80) {
            Ok(slug) => slug,
            Err(response) => return response,
        };
        let existing = sqlx::query_scalar::<_, i64>(
            "SELECT COUNT(*) FROM core_workflowdefinition \
             WHERE CAST(project_id AS TEXT) = ? AND slug = ? AND CAST(id AS TEXT) <> ?",
        )
        .bind(&record.project_id)
        .bind(&slug)
        .bind(&record.id)
        .fetch_one(pool)
        .await;
        if matches!(existing, Ok(value) if value > 0) {
            return validation_error("slug", "workflow with this slug already exists.");
        }
        changes.insert("slug".to_string(), json!(slug));
    }
    if let Some(value) = payload.get("description") {
        let Some(description) = value.as_str() else {
            return validation_error("description", "Not a valid string.");
        };
        changes.insert("description".to_string(), json!(description));
    }
    if let Some(value) = payload.get("draft_definition") {
        match draft_value(value) {
            Ok(draft) => {
                changes.insert("draft_definition".to_string(), draft);
            }
            Err(response) => return response,
        }
    }
    if changes.is_empty() {
        return Json(record.to_json()).into_response();
    }
    let postgres = is_postgres(&state);
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return sqlx_error(error),
    };
    let set_parts: Vec<String> = changes
        .keys()
        .map(|field| {
            if *field == "draft_definition" {
                if postgres {
                    "draft_definition = ?::jsonb".to_string()
                } else {
                    "draft_definition = ?".to_string()
                }
            } else {
                format!("{field} = ?")
            }
        })
        .collect();
    let sql = format!(
        "UPDATE core_workflowdefinition SET {}, updated_at = {} WHERE CAST(id AS TEXT) = ?",
        set_parts.join(", "),
        if postgres { "?::timestamptz" } else { "?" }
    );
    let mut query = sqlx::query(&sql);
    for (field, value) in &changes {
        if field == "draft_definition" {
            query = query.bind(value.to_string());
        } else {
            query = query.bind(value.as_str().unwrap_or("").to_string());
        }
    }
    let result = query
        .bind(&now)
        .bind(&workflow_id)
        .execute(&mut *transaction)
        .await;
    if let Err(error) = result {
        return sqlx_error(error);
    }
    let fields: Vec<&String> = changes.keys().collect();
    if let Err(error) = record_event_in_transaction(
        &mut transaction,
        postgres,
        &AuditEvent {
            tenant_id: Some(&record.tenant_id),
            project_id: Some(&record.project_id),
            event_type: "workflow.updated",
            resource_type: "workflow_definition",
            resource_id: &workflow_id,
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
        return database_error_message(error);
    }
    if let Err(error) = transaction.commit().await {
        return sqlx_error(error);
    }
    match fetch_workflow(pool, &state, &workflow_id).await {
        Ok(Some(record)) => Json(record.to_json()).into_response(),
        Ok(None) => not_found(),
        Err(error) => database_error_message(error),
    }
}

async fn workflow_rows(
    pool: &AnyPool,
    state: &AppState,
    tenant_id: Option<&str>,
    project_id: Option<&str>,
    workflow_id: Option<&str>,
) -> Result<Vec<WorkflowRecord>, String> {
    let postgres = is_postgres(state);
    let json_cast = if postgres { "::text" } else { "" };
    let columns = format!(
        "SELECT CAST(id AS TEXT) AS id, CAST(tenant_id AS TEXT) AS tenant_id, \
         CAST(project_id AS TEXT) AS project_id, name, slug, description, \
         draft_definition{json_cast} AS draft_text, current_version, is_archived, \
         {created}, {updated} FROM core_workflowdefinition",
        created = crate::tenancy::timestamp_expr(postgres, "core_workflowdefinition"),
        updated =
            crate::tenancy::timestamp_field_expr(postgres, "core_workflowdefinition", "updated_at"),
    );
    let rows = if let Some(workflow_id) = workflow_id {
        let sql = format!("{columns} WHERE CAST(id AS TEXT) = ?");
        sqlx::query(&sql)
            .bind(workflow_id)
            .fetch_all(pool)
            .await
            .map_err(|error| error.to_string())?
    } else {
        let archived = if postgres { "FALSE" } else { "0" };
        let sql = format!(
            "{columns} WHERE CAST(tenant_id AS TEXT) = ? AND CAST(project_id AS TEXT) = ? \
             AND is_archived = {archived} ORDER BY name"
        );
        sqlx::query(&sql)
            .bind(tenant_id.unwrap_or_default())
            .bind(project_id.unwrap_or_default())
            .fetch_all(pool)
            .await
            .map_err(|error| error.to_string())?
    };
    rows.iter().map(WorkflowRecord::from_row).collect()
}

async fn fetch_workflow(
    pool: &AnyPool,
    state: &AppState,
    workflow_id: &str,
) -> Result<Option<WorkflowRecord>, String> {
    Ok(workflow_rows(pool, state, None, None, Some(workflow_id))
        .await?
        .into_iter()
        .next())
}

impl WorkflowRecord {
    fn from_row(row: &sqlx::any::AnyRow) -> Result<Self, String> {
        Ok(Self {
            id: row.try_get("id").map_err(|error| error.to_string())?,
            tenant_id: row
                .try_get("tenant_id")
                .map_err(|error| error.to_string())?,
            project_id: row
                .try_get("project_id")
                .map_err(|error| error.to_string())?,
            name: row.try_get("name").map_err(|error| error.to_string())?,
            slug: row.try_get("slug").map_err(|error| error.to_string())?,
            description: row.try_get("description").unwrap_or_default(),
            draft_definition: row
                .try_get::<String, _>("draft_text")
                .ok()
                .and_then(|text| serde_json::from_str(&text).ok())
                .unwrap_or_else(|| json!({})),
            current_version: row.try_get("current_version").unwrap_or_default(),
            is_archived: row.try_get::<i64, _>("is_archived").unwrap_or_default() != 0,
            created_at: row.try_get("created").ok(),
            updated_at: row.try_get("updated").ok(),
        })
    }
}

pub(crate) async fn workflow_access(
    pool: &AnyPool,
    state: &AppState,
    workflow_id: &str,
    user_id: i64,
) -> Result<Option<WorkflowRecord>, String> {
    let Some(record) = fetch_workflow(pool, state, workflow_id).await? else {
        return Ok(None);
    };
    if tenant_role(pool, &record.tenant_id, user_id)
        .await?
        .is_none()
    {
        return Ok(None);
    }
    if project_access(pool, &record.tenant_id, &record.project_id, user_id).await? {
        Ok(Some(record))
    } else {
        Ok(None)
    }
}

async fn editor(role: &str, pool: &AnyPool, project_id: &str, user_id: i64) -> bool {
    if role == "owner" || role == "admin" {
        return true;
    }
    let project_role = sqlx::query_scalar::<_, String>(
        "SELECT role FROM core_projectmembership \
         WHERE CAST(project_id AS TEXT) = ? AND user_id = ?",
    )
    .bind(project_id)
    .bind(user_id)
    .fetch_optional(pool)
    .await;
    matches!(project_role, Ok(Some(role)) if role == "admin" || role == "editor")
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
            tracing::error!(%message, "rust workflow api could not resolve the session");
            Err(unavailable())
        }
    }
}

fn parse_body(body: &Bytes) -> Result<Value, Response> {
    match serde_json::from_slice::<Value>(body) {
        Ok(value) if value.is_object() => Ok(value),
        _ => Err((
            StatusCode::BAD_REQUEST,
            Json(json!({"detail": "A valid JSON body is required."})),
        )
            .into_response()),
    }
}

fn required_string(payload: &Value, field: &str, max: usize) -> Result<String, Response> {
    let Some(value) = payload.get(field) else {
        return Err(validation_error(field, "This field is required."));
    };
    string_value(value, field, max)
}

fn string_value(value: &Value, field: &str, max: usize) -> Result<String, Response> {
    match value.as_str() {
        Some(text) if !text.trim().is_empty() && text.chars().count() <= max => {
            Ok(text.to_string())
        }
        Some(text) if text.trim().is_empty() => {
            Err(validation_error(field, "This field may not be blank."))
        }
        Some(_) => Err(validation_error(
            field,
            &format!("Ensure this field has no more than {max} characters."),
        )),
        None => Err(validation_error(field, "Not a valid string.")),
    }
}

fn required_slug(payload: &Value, field: &str, max: usize) -> Result<String, Response> {
    let value = required_string(payload, field, max)?;
    slug_value(&json!(value), field, max)
}

fn slug_value(value: &Value, field: &str, max: usize) -> Result<String, Response> {
    let value = string_value(value, field, max)?;
    if value
        .chars()
        .all(|character| character.is_ascii_alphanumeric() || character == '-' || character == '_')
    {
        Ok(value)
    } else {
        Err(validation_error(field, "Enter a valid slug."))
    }
}

fn draft_definition(payload: &Value) -> Result<Value, Response> {
    match payload.get("draft_definition") {
        Some(value) => draft_value(value),
        None => Ok(json!({})),
    }
}

fn draft_value(value: &Value) -> Result<Value, Response> {
    if !value.is_object() {
        return Err(validation_error(
            "draft_definition",
            "Workflow definition must be a JSON object.",
        ));
    }
    if value.to_string().len() > 131072 {
        return Err(validation_error(
            "draft_definition",
            "Workflow definition exceeds 128 KiB.",
        ));
    }
    Ok(value.clone())
}

fn validation_error(field: &str, message: &str) -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(json!({ "error": { "code": "INVALID_REQUEST", "message": "Request could not be processed.", "details": { field: [message] } } })),
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
    tracing::error!(%error, "rust workflow database operation failed");
    unavailable()
}

fn database_error_message(message: String) -> Response {
    tracing::error!(%message, "rust workflow database operation failed");
    unavailable()
}
