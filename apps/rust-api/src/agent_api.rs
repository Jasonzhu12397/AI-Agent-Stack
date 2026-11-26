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

pub async fn list_agent_runs(
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
    let agent = match agent_access(pool, &state, &agent_id, user.id).await {
        Ok(Some(agent)) => agent,
        Ok(None) => return not_found(),
        Err(error) => return database_error(error),
    };
    let created = timestamp(&state, "run", "created_at");
    let finished = timestamp(&state, "run", "finished_at");
    let sql = format!(
        "SELECT CAST(run.id AS TEXT) AS id, CAST(run.task_id AS TEXT) AS task_id, \
         CAST(run.agent_version_id AS TEXT) AS agent_version_id, task.status AS task_status, \
         run.input_text, run.output_text, run.input_tokens, run.output_tokens, \
         run.provider_name, run.model_name, run.latency_ms, {created} AS created, \
         {finished} AS finished FROM core_agentrun run \
         JOIN core_task task ON CAST(task.id AS TEXT) = CAST(run.task_id AS TEXT) \
         JOIN core_agentversion version ON CAST(version.id AS TEXT) = CAST(run.agent_version_id AS TEXT) \
         JOIN core_agentdefinition definition ON CAST(definition.id AS TEXT) = CAST(version.agent_id AS TEXT) \
         WHERE CAST(definition.id AS TEXT) = ? ORDER BY run.created_at DESC LIMIT 100"
    );
    let rows = match sqlx::query(&sql).bind(&agent.id).fetch_all(pool).await {
        Ok(rows) => rows,
        Err(error) => return database_error(error.to_string()),
    };
    Json(
        rows.iter()
            .map(|row| {
                let created_at: Option<String> = row.try_get("created").ok();
                let finished_at: Option<String> = row.try_get("finished").ok();
                json!({
                    "id": row.try_get::<String, _>("id").unwrap_or_default(),
                    "task_id": row.try_get::<String, _>("task_id").unwrap_or_default(),
                    "agent_version_id": row.try_get::<String, _>("agent_version_id").unwrap_or_default(),
                    "task_status": row.try_get::<String, _>("task_status").unwrap_or_default(),
                    "input_text": row.try_get::<String, _>("input_text").unwrap_or_default(),
                    "output_text": row.try_get::<String, _>("output_text").unwrap_or_default(),
                    "input_tokens": row.try_get::<i64, _>("input_tokens").unwrap_or_default(),
                    "output_tokens": row.try_get::<i64, _>("output_tokens").unwrap_or_default(),
                    "provider_name": row.try_get::<String, _>("provider_name").unwrap_or_default(),
                    "model_name": row.try_get::<String, _>("model_name").unwrap_or_default(),
                    "latency_ms": row.try_get::<i64, _>("latency_ms").unwrap_or_default(),
                    "created_at": created_at.as_deref().map(normalize_timestamp),
                    "finished_at": finished_at.as_deref().map(normalize_timestamp),
                })
            })
            .collect::<Vec<_>>(),
    )
    .into_response()
}

pub async fn create_agent_run(
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
    let agent = match agent_access(pool, &state, &agent_id, user.id).await {
        Ok(Some(agent)) => agent,
        Ok(None) => return not_found(),
        Err(error) => return database_error(error),
    };
    if agent.is_archived || agent.current_version <= 0 {
        return legacy_error(
            "Agent must have a published version before it can run.",
            StatusCode::CONFLICT,
            "AGENT_NOT_PUBLISHED",
        );
    }
    let idempotency_key = headers
        .get("idempotency-key")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .trim()
        .to_string();
    if idempotency_key.is_empty() || idempotency_key.chars().count() > 128 {
        return legacy_error(
            "A valid Idempotency-Key header is required.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        );
    }
    let payload = match parse_object(&body) {
        Ok(payload) => payload,
        Err(response) => return response,
    };
    let input = payload.get("input").and_then(Value::as_str).unwrap_or("");
    if input.trim().is_empty() || input.chars().count() > 20_000 {
        return legacy_error(
            "input must be a non-empty string no longer than 20,000 characters.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        );
    }
    let version = sqlx::query(
        "SELECT CAST(version.id AS TEXT) AS id FROM core_agentversion version \
         JOIN core_agentdefinition definition ON CAST(definition.id AS TEXT) = CAST(version.agent_id AS TEXT) \
         WHERE CAST(definition.id AS TEXT) = ? AND version.version = ?",
    )
    .bind(&agent.id)
    .bind(agent.current_version)
    .fetch_optional(pool)
    .await;
    let version_id = match version {
        Ok(Some(row)) => row.try_get::<String, _>("id").unwrap_or_default(),
        Ok(None) => return not_found(),
        Err(error) => return database_error(error.to_string()),
    };
    let task_id = uuid::Uuid::new_v4().to_string();
    let run_id = uuid::Uuid::new_v4().to_string();
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    let payload_json = json!({"agent_version_id": version_id, "input_text": input}).to_string();
    let postgres = is_postgres(&state);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return sqlx_error(error),
    };
    let existing = sqlx::query(
        "SELECT CAST(id AS TEXT) AS id, CAST(project_id AS TEXT) AS project_id, task_type, payload \
         FROM core_task WHERE CAST(tenant_id AS TEXT) = ? AND idempotency_key = ?",
    )
    .bind(&agent.tenant_id)
    .bind(&idempotency_key)
    .fetch_optional(&mut *transaction)
    .await;
    let (row_task_id, created) = match existing {
        Ok(Some(row)) => {
            let project: String = row.try_get("project_id").unwrap_or_default();
            let task_type: String = row.try_get("task_type").unwrap_or_default();
            let stored: String = row.try_get("payload").unwrap_or_default();
            if project != agent.project_id || task_type != "agent.run" || stored != payload_json {
                let _ = transaction.rollback().await;
                return legacy_error(
                    "Idempotency-Key was already used with a different request.",
                    StatusCode::CONFLICT,
                    "IDEMPOTENCY_CONFLICT",
                );
            }
            (row.try_get::<String, _>("id").unwrap_or_default(), false)
        }
        Ok(None) => {
            let insert = if postgres {
                "INSERT INTO core_task (id, tenant_id, project_id, created_by_id, task_type, payload, \
                 result, error, status, idempotency_key, attempts, max_attempts, available_at, \
                 lease_owner, lease_token, created_at, updated_at) VALUES \
                 (?::uuid, ?::uuid, ?::uuid, ?, ?, ?::jsonb, NULL, NULL, 'queued', ?, 0, 3, ?::timestamptz, \
                 NULL, NULL, ?::timestamptz, ?::timestamptz)"
            } else {
                "INSERT INTO core_task (id, tenant_id, project_id, created_by_id, task_type, payload, \
                 result, error, status, idempotency_key, attempts, max_attempts, available_at, \
                 lease_owner, lease_token, created_at, updated_at) VALUES \
                 (?, ?, ?, ?, ?, ?, NULL, NULL, 'queued', ?, 0, 3, ?, NULL, NULL, ?, ?)"
            };
            if let Err(error) = sqlx::query(insert)
                .bind(&task_id)
                .bind(&agent.tenant_id)
                .bind(&agent.project_id)
                .bind(user.id)
                .bind("agent.run")
                .bind(&payload_json)
                .bind(&idempotency_key)
                .bind(&now)
                .bind(&now)
                .bind(&now)
                .execute(&mut *transaction)
                .await
            {
                let _ = transaction.rollback().await;
                return database_error(error.to_string());
            }
            (task_id.clone(), true)
        }
        Err(error) => {
            let _ = transaction.rollback().await;
            return database_error(error.to_string());
        }
    };
    let insert_run = if postgres {
        "INSERT INTO core_agentrun (id, task_id, agent_version_id, input_text, output_text, \
         input_tokens, output_tokens, provider_name, model_name, latency_ms, execution_state, \
         created_at, finished_at) VALUES (?::uuid, ?::uuid, ?::uuid, ?, '', 0, 0, '', '', 0, \
         '{}'::jsonb, ?::timestamptz, NULL)"
    } else {
        "INSERT INTO core_agentrun (id, task_id, agent_version_id, input_text, output_text, \
         input_tokens, output_tokens, provider_name, model_name, latency_ms, execution_state, \
         created_at, finished_at) VALUES (?, ?, ?, ?, '', 0, 0, '', '', 0, '{}', ?, NULL)"
    };
    let run_insert = sqlx::query(insert_run)
        .bind(&run_id)
        .bind(&row_task_id)
        .bind(&version_id)
        .bind(input)
        .bind(&now)
        .execute(&mut *transaction)
        .await;
    if let Err(error) = run_insert {
        let existing_run = sqlx::query(
            "SELECT CAST(id AS TEXT) AS id FROM core_agentrun WHERE CAST(task_id AS TEXT) = ?",
        )
        .bind(&row_task_id)
        .fetch_optional(&mut *transaction)
        .await;
        if let Ok(Some(row)) = existing_run {
            let existing_id: String = row.try_get("id").unwrap_or_default();
            let _ = transaction.commit().await;
            let fetched = agent_run_row(pool, &state, &existing_id).await;
            return match fetched {
                Ok(Some(run)) => (
                    if created {
                        StatusCode::ACCEPTED
                    } else {
                        StatusCode::OK
                    },
                    Json(run),
                )
                    .into_response(),
                Ok(None) => not_found(),
                Err(error) => database_error(error),
            };
        }
        let _ = transaction.rollback().await;
        return database_error(error.to_string());
    }
    if let Err(error) = record_event_in_transaction(
        &mut transaction,
        postgres,
        &AuditEvent {
            tenant_id: Some(&agent.tenant_id),
            project_id: Some(&agent.project_id),
            event_type: "agent.run.created",
            resource_type: "agent_run",
            resource_id: &run_id,
            outcome: "success",
            actor_id: Some(user.id),
            request_id: header(&headers, "x-request-id"),
            remote_addr: None,
            user_agent: header(&headers, "user-agent"),
            metadata: json!({
                "agent_id": agent.id,
                "task_id": row_task_id,
                "agent_version_id": version_id,
                "deduplicated": !created,
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
    match agent_run_row(pool, &state, &run_id).await {
        Ok(Some(run)) => (
            if created {
                StatusCode::ACCEPTED
            } else {
                StatusCode::OK
            },
            Json(run),
        )
            .into_response(),
        Ok(None) => not_found(),
        Err(error) => database_error(error),
    }
}

pub async fn get_agent_run(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(run_id): Path<String>,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let tenant_id = sqlx::query_scalar::<_, String>(
        "SELECT CAST(definition.tenant_id AS TEXT) FROM core_agentrun run \
         JOIN core_agentversion version ON CAST(version.id AS TEXT) = CAST(run.agent_version_id AS TEXT) \
         JOIN core_agentdefinition definition ON CAST(definition.id AS TEXT) = CAST(version.agent_id AS TEXT) \
         WHERE CAST(run.id AS TEXT) = ?",
    )
    .bind(&run_id)
    .fetch_optional(pool)
    .await;
    let tenant_id = match tenant_id {
        Ok(Some(tenant_id)) => tenant_id,
        Ok(None) => return not_found(),
        Err(error) => return database_error(error.to_string()),
    };
    let member = sqlx::query_scalar::<_, i64>(
        "SELECT COUNT(*) FROM core_tenantmembership \
         WHERE CAST(tenant_id AS TEXT) = ? AND user_id = ?",
    )
    .bind(&tenant_id)
    .bind(user.id)
    .fetch_one(pool)
    .await
    .unwrap_or(0);
    if member == 0 || !can_view_run(pool, &run_id, user.id).await.unwrap_or(false) {
        return not_found();
    }
    match agent_run_row(pool, &state, &run_id).await {
        Ok(Some(run)) => Json(run).into_response(),
        Ok(None) => not_found(),
        Err(error) => database_error(error),
    }
}

async fn can_view_run(pool: &AnyPool, run_id: &str, user_id: i64) -> Result<bool, String> {
    let row = sqlx::query(
        "SELECT CAST(definition.tenant_id AS TEXT) AS tenant_id, \
         CAST(definition.project_id AS TEXT) AS project_id, \
         (SELECT COUNT(*) FROM core_tenantmembership admin \
          WHERE CAST(admin.tenant_id AS TEXT) = CAST(definition.tenant_id AS TEXT) \
            AND admin.user_id = ? AND admin.role IN ('owner', 'admin')) AS is_admin \
         FROM core_agentrun run \
         JOIN core_agentversion version ON CAST(version.id AS TEXT) = CAST(run.agent_version_id AS TEXT) \
         JOIN core_agentdefinition definition ON CAST(definition.id AS TEXT) = CAST(version.agent_id AS TEXT) \
         WHERE CAST(run.id AS TEXT) = ?",
    )
    .bind(user_id)
    .bind(run_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())?;
    let Some(row) = row else {
        return Ok(false);
    };
    if row.try_get::<i64, _>("is_admin").unwrap_or(0) > 0 {
        return Ok(true);
    }
    let project_id: String = row.try_get("project_id").unwrap_or_default();
    project_access(
        pool,
        &row.try_get::<String, _>("tenant_id").unwrap_or_default(),
        &project_id,
        user_id,
    )
    .await
    .map_err(|error| error.to_string())
}

async fn agent_run_row(
    pool: &AnyPool,
    state: &AppState,
    run_id: &str,
) -> Result<Option<Value>, String> {
    let created = timestamp(state, "run", "created_at");
    let finished = timestamp(state, "run", "finished_at");
    let sql = format!(
        "SELECT CAST(run.id AS TEXT) AS id, CAST(run.task_id AS TEXT) AS task_id, \
         CAST(run.agent_version_id AS TEXT) AS agent_version_id, task.status AS task_status, \
         run.input_text, run.output_text, run.input_tokens, run.output_tokens, \
         run.provider_name, run.model_name, run.latency_ms, {created} AS created, \
         {finished} AS finished FROM core_agentrun run \
         JOIN core_task task ON CAST(task.id AS TEXT) = CAST(run.task_id AS TEXT) \
         WHERE CAST(run.id AS TEXT) = ?"
    );
    let row = sqlx::query(&sql)
        .bind(run_id)
        .fetch_optional(pool)
        .await
        .map_err(|error| error.to_string())?;
    let Some(row) = row else {
        return Ok(None);
    };
    let created_at: Option<String> = row.try_get("created").ok();
    let finished_at: Option<String> = row.try_get("finished").ok();
    Ok(Some(json!({
        "id": row.try_get::<String, _>("id").unwrap_or_default(),
        "task_id": row.try_get::<String, _>("task_id").unwrap_or_default(),
        "agent_version_id": row.try_get::<String, _>("agent_version_id").unwrap_or_default(),
        "task_status": row.try_get::<String, _>("task_status").unwrap_or_default(),
        "input_text": row.try_get::<String, _>("input_text").unwrap_or_default(),
        "output_text": row.try_get::<String, _>("output_text").unwrap_or_default(),
        "input_tokens": row.try_get::<i64, _>("input_tokens").unwrap_or_default(),
        "output_tokens": row.try_get::<i64, _>("output_tokens").unwrap_or_default(),
        "provider_name": row.try_get::<String, _>("provider_name").unwrap_or_default(),
        "model_name": row.try_get::<String, _>("model_name").unwrap_or_default(),
        "latency_ms": row.try_get::<i64, _>("latency_ms").unwrap_or_default(),
        "created_at": created_at.as_deref().map(normalize_timestamp),
        "finished_at": finished_at.as_deref().map(normalize_timestamp),
    })))
}

pub async fn publish_agent(
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
    let agent = match agent_access(pool, &state, &agent_id, user.id).await {
        Ok(Some(agent)) => agent,
        Ok(None) => return not_found(),
        Err(error) => return database_error(error),
    };
    if !can_edit(pool, &agent.tenant_id, &agent.project_id, user.id)
        .await
        .unwrap_or(false)
    {
        return legacy_error(
            "Project editor permission required.",
            StatusCode::FORBIDDEN,
            "PERMISSION_DENIED",
        );
    }
    if agent.is_archived {
        return legacy_error(
            "Archived agents cannot be published.",
            StatusCode::CONFLICT,
            "AGENT_ARCHIVED",
        );
    }
    let config = match agent.draft_config.as_object() {
        Some(config) => config.clone(),
        None => Map::new(),
    };
    if config.get("kind").and_then(Value::as_str) == Some("marketplace_agent") {
        return legacy_error(
            "Marketplace agents are bound from installed plugins and cannot be republished through the agent editor.",
            StatusCode::CONFLICT,
            "MARKETPLACE_AGENT_IMMUTABLE",
        );
    }
    let model_config_id = config
        .get("model_config_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    let system_prompt = config
        .get("system_prompt")
        .and_then(Value::as_str)
        .unwrap_or("");
    let mut missing = Vec::new();
    if model_config_id.is_empty() {
        missing.push("model_config_id");
    }
    if system_prompt.trim().is_empty() {
        missing.push("system_prompt");
    }
    if !missing.is_empty() {
        return (
            StatusCode::BAD_REQUEST,
            Json(json!({"error": {
                "code": "INVALID_REQUEST",
                "message": "Missing required agent configuration.",
                "details": {"fields": missing},
            }})),
        )
            .into_response();
    }
    if uuid::Uuid::parse_str(model_config_id).is_err() {
        return legacy_error(
            "model_config_id must be a UUID.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        );
    }
    if system_prompt.chars().count() > 12_000 {
        return legacy_error(
            "system_prompt cannot exceed 12,000 characters.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        );
    }
    if config.contains_key("tool_allowlist") {
        return legacy_error(
            "tool_allowlist is retired; use capability_allowlist.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        );
    }
    let true_literal = if is_postgres(&state) { "TRUE" } else { "1" };
    let capabilities = match parse_object_field(&config, "capability_allowlist") {
        Ok(values) => values,
        Err(response) => return response,
    };
    let knowledge_ids_value = match parse_object_field(&config, "knowledge_base_ids") {
        Ok(values) => values,
        Err(response) => return response,
    };
    if knowledge_ids_value.len() > 8 {
        return legacy_error(
            "knowledge_base_ids may contain at most 8 knowledge bases.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        );
    }
    let mut knowledge_ids = Vec::new();
    for value in &knowledge_ids_value {
        let Some(id) = value.as_str() else {
            return legacy_error(
                "knowledge_base_ids entries must be UUIDs.",
                StatusCode::BAD_REQUEST,
                "INVALID_REQUEST",
            );
        };
        if uuid::Uuid::parse_str(id).is_err() {
            return legacy_error(
                "knowledge_base_ids entries must be UUIDs.",
                StatusCode::BAD_REQUEST,
                "INVALID_REQUEST",
            );
        }
        if knowledge_ids.iter().any(|existing: &String| existing == id) {
            return legacy_error(
                "knowledge_base_ids cannot contain duplicate IDs.",
                StatusCode::BAD_REQUEST,
                "INVALID_REQUEST",
            );
        }
        knowledge_ids.push(id.to_string());
    }
    let false_literal = if is_postgres(&state) { "FALSE" } else { "0" };
    for id in &knowledge_ids {
        let query = format!(
            "SELECT COUNT(*) FROM core_knowledgebase kb \
             JOIN core_modelconfig model ON CAST(model.id AS TEXT) = CAST(kb.embedding_model_id AS TEXT) \
             JOIN core_modelprovider provider ON CAST(provider.id AS TEXT) = CAST(model.provider_id AS TEXT) \
             WHERE CAST(kb.id AS TEXT) = ? AND CAST(kb.tenant_id AS TEXT) = ? \
               AND CAST(kb.project_id AS TEXT) = ? AND kb.is_archived = {false_literal} \
               AND model.enabled = {true_literal} AND provider.enabled = {true_literal}"
        );
        let available = sqlx::query_scalar::<_, i64>(&query)
            .bind(id)
            .bind(&agent.tenant_id)
            .bind(&agent.project_id)
            .fetch_one(pool)
            .await
            .unwrap_or(0);
        if available == 0 {
            return legacy_error(
                "Every knowledge base must be active and belong to this project.",
                StatusCode::BAD_REQUEST,
                "INVALID_REQUEST",
            );
        }
    }
    let capabilities_sql = if is_postgres(&state) {
        "CAST(config.capabilities AS TEXT)"
    } else {
        "config.capabilities"
    };
    let model = sqlx::query(&format!(
        "SELECT CAST(config.id AS TEXT) AS id, {capabilities_sql} AS capabilities, \
         config.max_output_tokens FROM core_modelconfig config \
         JOIN core_modelprovider provider ON CAST(provider.id AS TEXT) = CAST(config.provider_id AS TEXT) \
         WHERE CAST(config.id AS TEXT) = ? AND config.enabled = {true_literal} \
           AND provider.enabled = {true_literal} \
           AND (CAST(provider.tenant_id AS TEXT) = ? OR provider.tenant_id IS NULL) \
           AND (provider.auth_mode <> 'user_account' OR provider.created_by_id = ?)"
    ))
    .bind(model_config_id)
    .bind(&agent.tenant_id)
    .bind(user.id)
    .fetch_optional(pool)
    .await;
    let model = match model {
        Ok(Some(model)) => model,
        Ok(None) => return not_found(),
        Err(error) => return database_error(error.to_string()),
    };
    let model_capabilities = parse_json(
        model
            .try_get::<Option<String>, _>("capabilities")
            .ok()
            .flatten(),
    )
    .and_then(|value| value.as_array().cloned())
    .unwrap_or_default();
    let has_chat = model_capabilities
        .iter()
        .any(|value| value.as_str() == Some("chat"));
    if !has_chat {
        return legacy_error(
            "Selected model does not declare chat capability.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        );
    }
    let max_output_tokens: i64 = model.try_get("max_output_tokens").unwrap_or(0);
    let mut capability_names = Vec::new();
    if !capabilities.is_empty() {
        if capabilities.len() > 32 {
            return legacy_error(
                "capability_allowlist may contain at most 32 capabilities.",
                StatusCode::BAD_REQUEST,
                "INVALID_REQUEST",
            );
        }
        let has_tools = model_capabilities
            .iter()
            .any(|value| value.as_str() == Some("tools"));
        if !has_tools {
            return legacy_error(
                "Selected model does not declare tools capability.",
                StatusCode::BAD_REQUEST,
                "INVALID_REQUEST",
            );
        }
        for value in &capabilities {
            let Some(name) = value.as_str().filter(|name| !name.is_empty()) else {
                return legacy_error(
                    "capability_allowlist entries must be capability names.",
                    StatusCode::BAD_REQUEST,
                    "INVALID_REQUEST",
                );
            };
            if capability_names
                .iter()
                .any(|existing: &String| existing == name)
            {
                return legacy_error(
                    "capability_allowlist cannot contain duplicate names.",
                    StatusCode::BAD_REQUEST,
                    "INVALID_REQUEST",
                );
            }
            match resolve_capability_risk(pool, &agent.tenant_id, &agent.project_id, name).await {
                Ok(Some(risk)) => {
                    if matches!(risk.as_str(), "high" | "critical" | "unclassified") {
                        return legacy_error(
                            &format!(
                                "{name} requires per-call approval, which published agents do not support yet."
                            ),
                            StatusCode::BAD_REQUEST,
                            "INVALID_REQUEST",
                        );
                    }
                    capability_names.push(name.to_string());
                }
                Ok(None) => {
                    return (
                        StatusCode::BAD_REQUEST,
                        Json(json!({"error": {
                            "code": "CAPABILITY_NOT_INSTALLED",
                            "message": format!("{name}: The capability is not installed in this project."),
                            "details": {},
                        }})),
                    )
                        .into_response();
                }
                Err(error) => return database_error(error),
            }
        }
    }
    let max_iterations = int_setting(&config, "max_iterations", 4);
    if !(1..=8).contains(&max_iterations) {
        return legacy_error(
            "max_iterations must be between 1 and 8.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        );
    }
    let timeout_seconds = int_setting(&config, "timeout_seconds", 60);
    if !(1..=120).contains(&timeout_seconds) {
        return legacy_error(
            "timeout_seconds must be between 1 and 120.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        );
    }
    let max_tokens = int_setting(&config, "max_tokens", 2048);
    if max_tokens < 1 || max_tokens > max_output_tokens {
        return legacy_error(
            "max_tokens exceeds the selected model output limit.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        );
    }
    let version_number = sqlx::query_scalar::<_, i64>(
        "SELECT COALESCE(MAX(version), 0) FROM core_agentversion \
         WHERE CAST(agent_id AS TEXT) = ?",
    )
    .bind(&agent.id)
    .fetch_one(pool)
    .await
    .unwrap_or(0)
        + 1;
    let snapshot = json!({
        "model_config_id": model_config_id,
        "system_prompt": system_prompt,
        "capability_allowlist": capability_names,
        "knowledge_base_ids": knowledge_ids,
        "max_iterations": max_iterations,
        "timeout_seconds": timeout_seconds,
        "max_tokens": max_tokens,
    });
    let version_id = uuid::Uuid::new_v4().to_string();
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    let postgres = is_postgres(&state);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => return sqlx_error(error),
    };
    let insert = if postgres {
        "INSERT INTO core_agentversion (id, agent_id, version, config_snapshot, created_by_id, \
         published_at) VALUES (?::uuid, ?::uuid, ?, ?::jsonb, ?, ?::timestamptz)"
    } else {
        "INSERT INTO core_agentversion (id, agent_id, version, config_snapshot, created_by_id, \
         published_at) VALUES (?, ?, ?, ?, ?, ?)"
    };
    if let Err(error) = sqlx::query(insert)
        .bind(&version_id)
        .bind(&agent.id)
        .bind(version_number)
        .bind(snapshot.to_string())
        .bind(user.id)
        .bind(&now)
        .execute(&mut *transaction)
        .await
    {
        let _ = transaction.rollback().await;
        return database_error(error.to_string());
    }
    if let Err(error) = sqlx::query(
        "UPDATE core_agentdefinition SET current_version = ?, updated_at = ? \
         WHERE CAST(id AS TEXT) = ?",
    )
    .bind(version_number)
    .bind(&now)
    .bind(&agent.id)
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
            tenant_id: Some(&agent.tenant_id),
            project_id: Some(&agent.project_id),
            event_type: "agent.published",
            resource_type: "agent_version",
            resource_id: &version_id,
            outcome: "success",
            actor_id: Some(user.id),
            request_id: header(&headers, "x-request-id"),
            remote_addr: None,
            user_agent: header(&headers, "user-agent"),
            metadata: json!({
                "agent_id": agent.id,
                "version": version_number,
                "model_config_id": model_config_id,
                "tool_count": capability_names.len(),
                "knowledge_base_count": knowledge_ids.len(),
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
    (
        StatusCode::CREATED,
        Json(json!({
            "id": version_id,
            "agent_id": agent.id,
            "version": version_number,
            "config_snapshot": snapshot,
            "published_at": now,
        })),
    )
        .into_response()
}

fn parse_object_field(config: &Map<String, Value>, key: &str) -> Result<Vec<Value>, Response> {
    match config.get(key) {
        None => Ok(Vec::new()),
        Some(Value::Array(values)) => Ok(values.clone()),
        Some(_) => Err(legacy_error(
            "capability_allowlist and knowledge_base_ids must be arrays.",
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
        )),
    }
}

fn int_setting(config: &Map<String, Value>, key: &str, default: i64) -> i64 {
    match config.get(key) {
        Some(value) => value.as_i64().unwrap_or(-1),
        None => default,
    }
}

async fn resolve_capability_risk(
    pool: &AnyPool,
    tenant_id: &str,
    project_id: &str,
    name: &str,
) -> Result<Option<String>, String> {
    let capabilities_text = "plugin.capabilities";
    let rows = sqlx::query(&format!(
        "SELECT {capabilities_text} AS capabilities, installation.status, plugin.enabled \
         FROM core_agentplugininstallation installation \
         JOIN core_agentplugindefinition plugin \
           ON CAST(plugin.id AS TEXT) = CAST(installation.plugin_id AS TEXT) \
         WHERE CAST(installation.tenant_id AS TEXT) = ? \
           AND CAST(installation.project_id AS TEXT) = ? ORDER BY installation.created_at DESC"
    ))
    .bind(tenant_id)
    .bind(project_id)
    .fetch_all(pool)
    .await
    .map_err(|error| error.to_string())?;
    let mut matched_any = false;
    let mut saw_inactive = false;
    for row in rows {
        let entries = parse_json(
            row.try_get::<Option<String>, _>("capabilities")
                .ok()
                .flatten(),
        )
        .and_then(|value| value.as_array().cloned())
        .unwrap_or_default();
        let Some(definition) = entries
            .iter()
            .find(|entry| entry.get("name").and_then(Value::as_str) == Some(name))
        else {
            continue;
        };
        matched_any = true;
        let status: String = row.try_get("status").unwrap_or_default();
        let enabled: i64 = row.try_get("enabled").unwrap_or(0);
        if status == "installed" && enabled != 0 {
            let risk = definition
                .get("risk_level")
                .and_then(Value::as_str)
                .unwrap_or("low");
            return Ok(Some(risk.to_string()));
        }
        saw_inactive = true;
    }
    if matched_any && saw_inactive {
        return Ok(Some("unclassified".to_string()));
    }
    Ok(None)
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
