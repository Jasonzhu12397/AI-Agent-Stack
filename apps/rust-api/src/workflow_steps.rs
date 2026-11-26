#![allow(clippy::result_large_err)]

use axum::body::Bytes;
use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use chrono::{DateTime, SecondsFormat, Utc};
use serde_json::{json, Value};
use sqlx::{AnyPool, Row};

use crate::audit::{record_event_in_transaction, AuditEvent};
use crate::context::{authenticate, AuthError};
use crate::proxy::AppState;
use crate::tenancy::{is_postgres, project_access, tenant_role};

pub async fn get_run(
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
    let workflow_id = match run_workflow_id(pool, &run_id).await {
        Ok(Some(id)) => id,
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    };
    if !match crate::workflows::workflow_access(pool, &state, &workflow_id, user.id).await {
        Ok(Some(record)) => project_access(pool, &record.tenant_id, &record.project_id, user.id)
            .await
            .unwrap_or(false),
        _ => false,
    } {
        return not_found();
    }
    match crate::workflow_reads::fetch_run(pool, &state, &run_id).await {
        Ok(Some(run)) => Json(run).into_response(),
        Ok(None) => not_found(),
        Err(error) => database_error_message(error),
    }
}

pub async fn approve(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((run_id, node_id)): Path<(String, String)>,
    body: Bytes,
) -> Response {
    decide(state, headers, run_id, node_id, body, true).await
}

pub async fn reject(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((run_id, node_id)): Path<(String, String)>,
    body: Bytes,
) -> Response {
    decide(state, headers, run_id, node_id, body, false).await
}

async fn decide(
    state: AppState,
    headers: HeaderMap,
    run_id: String,
    node_id: String,
    body: Bytes,
    approve: bool,
) -> Response {
    let user = match require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let reason = match parse_reason(&body) {
        Ok(reason) => reason,
        Err(response) => return response,
    };
    let workflow = match run_workflow(pool, &state, &run_id).await {
        Ok(Some(record)) => record,
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    };
    if !project_access(pool, &workflow.tenant_id, &workflow.project_id, user.id)
        .await
        .unwrap_or(false)
    {
        return not_found();
    }
    let role = match tenant_role(pool, &workflow.tenant_id, user.id).await {
        Ok(Some(role)) => role,
        Ok(None) => return not_found(),
        Err(error) => return database_error_message(error),
    };
    if !decider(role.as_str(), pool, &workflow.project_id, user.id).await {
        return (
            StatusCode::FORBIDDEN,
            Json(json!({"detail": "Project administrator permission required."})),
        )
            .into_response();
    }
    let deadline = match run_deadline(pool, &state, &run_id).await {
        Ok(value) => value,
        Err(error) => return database_error_message(error),
    };
    if let Some(deadline) = deadline {
        if deadline <= Utc::now() {
            return (
                StatusCode::CONFLICT,
                Json(json!({"detail": "Workflow run deadline exceeded before the decision."})),
            )
                .into_response();
        }
    }
    let postgres = is_postgres(&state);
    let mut transaction = match pool.begin().await {
        Ok(transaction) => transaction,
        Err(error) => {
            tracing::error!(%error, "rust workflow approval transaction failed");
            return unavailable();
        }
    };
    let task_id = match sqlx::query_scalar::<_, String>(
        "SELECT CAST(task_id AS TEXT) FROM core_workflowrun WHERE CAST(id AS TEXT) = ?",
    )
    .bind(&run_id)
    .fetch_optional(&mut *transaction)
    .await
    {
        Ok(Some(id)) => id,
        Ok(None) => {
            let _ = transaction.rollback().await;
            return not_found();
        }
        Err(error) => {
            let _ = transaction.rollback().await;
            return database_error_message(error.to_string());
        }
    };
    let task_sql = if postgres {
        "SELECT status FROM core_task WHERE CAST(id AS TEXT) = ? FOR UPDATE"
    } else {
        "SELECT status FROM core_task WHERE CAST(id AS TEXT) = ?"
    };
    let task = match sqlx::query(task_sql)
        .bind(&task_id)
        .fetch_optional(&mut *transaction)
        .await
    {
        Ok(Some(row)) => row,
        Ok(None) => {
            let _ = transaction.rollback().await;
            return not_found();
        }
        Err(error) => {
            let _ = transaction.rollback().await;
            return database_error_message(error.to_string());
        }
    };
    let task_status: String = task.try_get("status").unwrap_or_default();
    let step = match sqlx::query(
        "SELECT CAST(id AS TEXT) AS id, status FROM core_workflowsteprun \
         WHERE CAST(workflow_run_id AS TEXT) = ? AND node_id = ? AND node_type = 'approval'",
    )
    .bind(&run_id)
    .bind(&node_id)
    .fetch_optional(&mut *transaction)
    .await
    {
        Ok(Some(row)) => row,
        Ok(None) => {
            let _ = transaction.rollback().await;
            return not_found();
        }
        Err(error) => {
            let _ = transaction.rollback().await;
            return database_error_message(error.to_string());
        }
    };
    let step_status: String = step.try_get("status").unwrap_or_default();
    if step_status != "awaiting_approval" {
        let _ = transaction.rollback().await;
        return (
            StatusCode::CONFLICT,
            Json(json!({"detail": "Workflow step is not awaiting approval."})),
        )
            .into_response();
    }
    if task_status != "waiting_approval" {
        let _ = transaction.rollback().await;
        return (
            StatusCode::CONFLICT,
            Json(json!({"detail": "Workflow task is no longer waiting for approval."})),
        )
            .into_response();
    }
    let deadline_expr = if postgres {
        "to_char(deadline_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
    } else {
        "strftime('%Y-%m-%dT%H:%M:%fZ', deadline_at)"
    };
    let deadline_sql = format!(
        "SELECT {deadline_expr} AS deadline FROM core_workflowrun WHERE CAST(id AS TEXT) = ?"
    );
    let deadline_text = match sqlx::query_scalar::<_, Option<String>>(&deadline_sql)
        .bind(&run_id)
        .fetch_optional(&mut *transaction)
        .await
    {
        Ok(Some(value)) => value,
        Ok(None) => {
            let _ = transaction.rollback().await;
            return not_found();
        }
        Err(error) => {
            let _ = transaction.rollback().await;
            return database_error_message(error.to_string());
        }
    };
    let deadline = match deadline_text {
        Some(value) => match DateTime::parse_from_rfc3339(&value) {
            Ok(value) => Some(value.with_timezone(&Utc)),
            Err(error) => {
                let _ = transaction.rollback().await;
                return database_error_message(error.to_string());
            }
        },
        None => None,
    };
    if deadline.is_some_and(|deadline| deadline <= Utc::now()) {
        let _ = transaction.rollback().await;
        return (
            StatusCode::CONFLICT,
            Json(json!({"detail": "Workflow run deadline exceeded before the decision."})),
        )
            .into_response();
    }

    let now = Utc::now();
    let now_text = now.to_rfc3339_opts(SecondsFormat::Micros, false);
    let decision = if approve { "approved" } else { "rejected" };
    let decision_payload = json!({
        "decision": decision,
        "decided_by_id": user.id,
        "decided_at": now_text,
        "reason": reason.trim(),
    });
    let step_id: String = step.try_get("id").unwrap_or_default();
    let step_status_value = if approve { "succeeded" } else { "failed" };
    let step_error = if approve {
        Value::Null
    } else {
        json!({
            "code": "WORKFLOW_APPROVAL_REJECTED",
            "message": "A project administrator rejected this workflow step.",
        })
    };
    let step_update = if postgres {
        "UPDATE core_workflowsteprun SET status = ?, output_data = ?::jsonb, error = ?::jsonb, \
         finished_at = ?::timestamptz WHERE CAST(id AS TEXT) = ?"
    } else {
        "UPDATE core_workflowsteprun SET status = ?, output_data = ?, error = ?, \
         finished_at = ? WHERE CAST(id AS TEXT) = ?"
    };
    if let Err(error) = sqlx::query(step_update)
        .bind(step_status_value)
        .bind(decision_payload.to_string())
        .bind(if step_error.is_null() {
            None
        } else {
            Some(step_error.to_string())
        })
        .bind(&now_text)
        .bind(&step_id)
        .execute(&mut *transaction)
        .await
    {
        let _ = transaction.rollback().await;
        return database_error_message(error.to_string());
    }
    if approve {
        let task_update = if postgres {
            "UPDATE core_task SET status = 'queued', available_at = ?::timestamptz, attempts = 0, \
             error = NULL, lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, \
             updated_at = ?::timestamptz WHERE CAST(id AS TEXT) = ?"
        } else {
            "UPDATE core_task SET status = 'queued', available_at = ?, attempts = 0, \
             error = NULL, lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, \
             updated_at = ? WHERE CAST(id AS TEXT) = ?"
        };
        if let Err(error) = sqlx::query(task_update)
            .bind(&now_text)
            .bind(&now_text)
            .bind(&task_id)
            .execute(&mut *transaction)
            .await
        {
            let _ = transaction.rollback().await;
            return database_error_message(error.to_string());
        }
    } else {
        let task_error = json!({
            "code": "WORKFLOW_APPROVAL_REJECTED",
            "message": "A project administrator rejected this workflow step.",
            "retryable": false,
        });
        let task_update = if postgres {
            "UPDATE core_task SET status = 'failed', error = ?::jsonb, finished_at = ?::timestamptz, \
             lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, \
             updated_at = ?::timestamptz WHERE CAST(id AS TEXT) = ?"
        } else {
            "UPDATE core_task SET status = 'failed', error = ?, finished_at = ?, \
             lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, \
             updated_at = ? WHERE CAST(id AS TEXT) = ?"
        };
        if let Err(error) = sqlx::query(task_update)
            .bind(task_error.to_string())
            .bind(&now_text)
            .bind(&now_text)
            .bind(&task_id)
            .execute(&mut *transaction)
            .await
        {
            let _ = transaction.rollback().await;
            return database_error_message(error.to_string());
        }
        let run_update = if postgres {
            "UPDATE core_workflowrun SET finished_at = ?::timestamptz WHERE CAST(id AS TEXT) = ?"
        } else {
            "UPDATE core_workflowrun SET finished_at = ? WHERE CAST(id AS TEXT) = ?"
        };
        if let Err(error) = sqlx::query(run_update)
            .bind(&now_text)
            .bind(&run_id)
            .execute(&mut *transaction)
            .await
        {
            let _ = transaction.rollback().await;
            return database_error_message(error.to_string());
        }
    }
    if let Err(error) = record_event_in_transaction(
        &mut transaction,
        postgres,
        &AuditEvent {
            tenant_id: Some(&workflow.tenant_id),
            project_id: Some(&workflow.project_id),
            event_type: if approve {
                "workflow.approval.approved"
            } else {
                "workflow.approval.rejected"
            },
            resource_type: "workflow_step_run",
            resource_id: &step_id,
            outcome: "success",
            actor_id: Some(user.id),
            request_id: header(&headers, "x-request-id"),
            remote_addr: None,
            user_agent: header(&headers, "user-agent"),
            metadata: json!({
                "workflow_run_id": run_id,
                "workflow_id": workflow.id,
                "node_id": node_id,
                "reason": reason.trim(),
            }),
        },
    )
    .await
    {
        let _ = transaction.rollback().await;
        return database_error_message(error);
    }
    if let Err(error) = transaction.commit().await {
        return database_error_message(error.to_string());
    }
    Json(json!({
        "run_id": run_id,
        "node_id": node_id,
        "decision": decision,
        "task_status": if approve { "queued" } else { "failed" },
    }))
    .into_response()
}

async fn run_workflow_id(pool: &AnyPool, run_id: &str) -> Result<Option<String>, String> {
    sqlx::query_scalar::<_, String>(
        "SELECT CAST(v.workflow_id AS TEXT) FROM core_workflowrun r \
         JOIN core_workflowversion v ON v.id = r.workflow_version_id \
         WHERE CAST(r.id AS TEXT) = ?",
    )
    .bind(run_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())
}

async fn run_workflow(
    pool: &AnyPool,
    state: &AppState,
    run_id: &str,
) -> Result<Option<crate::workflows::WorkflowRecord>, String> {
    let Some(workflow_id) = run_workflow_id(pool, run_id).await? else {
        return Ok(None);
    };
    crate::workflows::fetch_workflow(pool, state, &workflow_id).await
}

async fn run_deadline(
    pool: &AnyPool,
    state: &AppState,
    run_id: &str,
) -> Result<Option<DateTime<Utc>>, String> {
    let postgres = is_postgres(state);
    let expr = if postgres {
        "to_char(deadline_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')".to_string()
    } else {
        "strftime('%Y-%m-%dT%H:%M:%fZ', deadline_at)".to_string()
    };
    let sql = format!("SELECT {expr} AS deadline FROM core_workflowrun WHERE CAST(id AS TEXT) = ?");
    let value = sqlx::query_scalar::<_, Option<String>>(&sql)
        .bind(run_id)
        .fetch_optional(pool)
        .await
        .map_err(|error| error.to_string())?
        .flatten();
    Ok(value.and_then(|text| {
        DateTime::parse_from_rfc3339(&text)
            .ok()
            .map(|date| date.with_timezone(&Utc))
    }))
}

async fn decider(role: &str, pool: &AnyPool, project_id: &str, user_id: i64) -> bool {
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
    matches!(project_role, Ok(Some(role)) if role == "admin")
}

fn parse_reason(body: &Bytes) -> Result<String, Response> {
    let value = if body.is_empty() {
        json!({})
    } else {
        match serde_json::from_slice::<Value>(body) {
            Ok(value) if value.is_object() => value,
            _ => {
                return Err((
                    StatusCode::BAD_REQUEST,
                    Json(json!({"detail": "Request body must be a JSON object."})),
                )
                    .into_response())
            }
        }
    };
    match value.get("reason") {
        None => Ok(String::new()),
        Some(Value::String(reason)) if reason.chars().count() <= 500 => Ok(reason.clone()),
        Some(_) => Err((
            StatusCode::BAD_REQUEST,
            Json(json!({"detail": "reason must be a string no longer than 500 characters."})),
        )
            .into_response()),
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
            tracing::error!(%message, "rust workflow step api could not resolve the session");
            Err(unavailable())
        }
    }
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

fn database_error_message(message: String) -> Response {
    tracing::error!(%message, "rust workflow step database operation failed");
    unavailable()
}
