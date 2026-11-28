use std::time::Duration;

use serde_json::{json, Value};
use sqlx::{AnyPool, Row};

use crate::proxy::AppState;
use crate::tenancy::is_postgres;

struct ClaimedTask {
    id: String,
    task_type: String,
    payload: Value,
    attempts: i64,
    max_attempts: i64,
    lease_token: String,
    tenant_id: String,
    project_id: String,
    created_by_id: i64,
}

pub fn spawn(state: AppState) {
    if !state.config.worker_enabled {
        tracing::info!("rust task worker disabled");
        return;
    }
    let poll = state.config.worker_poll_seconds;
    tokio::spawn(async move {
        let worker_id = std::env::var("WORKER_ID")
            .unwrap_or_else(|_| format!("rust-worker-{}", std::process::id()));
        tracing::info!(%worker_id, poll_seconds = poll, "rust task worker started");
        let dispatch_interval = Duration::from_secs(30);
        let backup_interval = Duration::from_secs(60);
        let mut last_dispatch = std::time::Instant::now() - dispatch_interval;
        let mut last_backup_dispatch = std::time::Instant::now() - backup_interval;
        loop {
            if last_backup_dispatch.elapsed() >= backup_interval {
                last_backup_dispatch = std::time::Instant::now();
                if let Some(pool) = state.identity_pool.as_ref() {
                    let now = chrono::Utc::now().format("%Y-%m-%d %H:%M:%S").to_string();
                    match crate::backup_queue::dispatch_hourly_backups(pool, &now).await {
                        Ok(queued) if queued > 0 => {
                            tracing::info!(queued, "queued hourly etcd backups")
                        }
                        Ok(_) => {}
                        Err(error) => tracing::error!(%error, "hourly etcd backup dispatch failed"),
                    }
                }
            }
            if last_dispatch.elapsed() >= dispatch_interval {
                last_dispatch = std::time::Instant::now();
                match crate::scheduled_api::dispatch_due_jobs(&state, 3).await {
                    Ok(ran) if ran > 0 => tracing::info!(ran, "dispatched scheduled turns"),
                    Ok(_) => {}
                    Err(error) => {
                        tracing::error!(%error, "scheduled dispatch failed");
                    }
                }
            }
            match run_once(&state, &worker_id).await {
                Ok(true) => {}
                Ok(false) => tokio::time::sleep(Duration::from_secs(poll)).await,
                Err(error) => {
                    tracing::error!(%error, "rust task worker loop failed");
                    tokio::time::sleep(Duration::from_secs(poll)).await;
                }
            }
        }
    });
}

pub async fn run_once(state: &AppState, worker_id: &str) -> Result<bool, String> {
    let pool = state
        .identity_pool
        .as_ref()
        .ok_or_else(|| "Database is unavailable.".to_string())?;
    let Some(task) = claim(pool, state, worker_id).await? else {
        return Ok(false);
    };
    tracing::info!(task_id = %task.id, task_type = %task.task_type, "claimed task");
    let execution = execute(state, &task, worker_id).await;
    match execution {
        Ok(TaskOutcome::Success(result)) => {
            finish_success(pool, state, &task, &result).await?;
            Ok(true)
        }
        Ok(TaskOutcome::Suspended) => Ok(true),
        Err(error) => {
            tracing::warn!(task_id = %task.id, %error, "task failed");
            finish_failure(pool, state, &task, &error).await?;
            Ok(true)
        }
    }
}

enum TaskOutcome {
    Success(Value),
    Suspended,
}

async fn execute(
    state: &AppState,
    task: &ClaimedTask,
    worker_id: &str,
) -> Result<TaskOutcome, String> {
    match task.task_type.as_str() {
        "agent.run" => {
            let result = crate::agent_runner::execute(
                state,
                &task.id,
                &task.tenant_id,
                &task.project_id,
                task.created_by_id,
                worker_id,
                &task.lease_token,
            )
            .await?;
            Ok(TaskOutcome::Success(result))
        }
        "knowledge.ingest" => {
            let document_id = task
                .payload
                .get("knowledge_document_id")
                .and_then(Value::as_str)
                .ok_or_else(|| "Knowledge ingest task has no document id.".to_string())?;
            renew_lease(
                state.identity_pool.as_ref().unwrap(),
                state,
                &task.id,
                worker_id,
                &task.lease_token,
            )
            .await?;
            let result = crate::rag::ingest_document(
                state,
                &task.tenant_id,
                &task.project_id,
                task.created_by_id,
                document_id,
            )
            .await?;
            Ok(TaskOutcome::Success(json!({
                "document_id": document_id,
                "chunks": result.chunks,
                "embedding_model": result.embedding_model,
                "collection": result.collection,
            })))
        }
        "workflow.run" | "team.run" => {
            let result = crate::workflow_runtime::execute(
                state,
                &task.id,
                &task.tenant_id,
                &task.project_id,
                task.created_by_id,
                worker_id,
                &task.lease_token,
            )
            .await;
            match result {
                Ok(output) => {
                    let pool = state.identity_pool.as_ref().unwrap();
                    crate::workflow_runtime::finalize_run(pool, state, &task.id, &output).await?;
                    if task.task_type == "team.run" {
                        crate::workflow_runtime::finish_team_run(
                            pool,
                            state,
                            &task.id,
                            output.get("output").unwrap_or(&Value::Null),
                        )
                        .await?;
                    }
                    Ok(TaskOutcome::Success(output))
                }
                Err(crate::workflow_runtime::WorkflowError::Suspend) => Ok(TaskOutcome::Suspended),
                Err(crate::workflow_runtime::WorkflowError::Failed(error)) => Err(error),
            }
        }
        "demo.echo" => Ok(TaskOutcome::Success(json!({"echo": task.payload}))),
        "demo.sleep" => {
            let seconds = task
                .payload
                .get("seconds")
                .and_then(Value::as_i64)
                .unwrap_or(1)
                .clamp(0, 3);
            tokio::time::sleep(Duration::from_secs(seconds as u64)).await;
            Ok(TaskOutcome::Success(json!({"slept_seconds": seconds})))
        }
        other => Err(format!("Unsupported task type: {other}")),
    }
}

async fn claim(
    pool: &AnyPool,
    state: &AppState,
    worker_id: &str,
) -> Result<Option<ClaimedTask>, String> {
    let postgres = is_postgres(state);
    for _ in 0..5 {
        let now = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false);
        let sql = if postgres {
            "SELECT CAST(id AS TEXT) AS id, task_type, payload, status, attempts, max_attempts, \
             CAST(tenant_id AS TEXT) AS tenant_id, CAST(project_id AS TEXT) AS project_id, created_by_id \
             FROM core_task WHERE (status = 'queued' AND available_at <= ?::timestamptz) \
               OR ((status = 'running' OR status = 'cancel_requested') AND lease_expires_at < ?::timestamptz) \
             ORDER BY available_at, created_at LIMIT 1"
        } else {
            "SELECT CAST(id AS TEXT) AS id, task_type, payload, status, attempts, max_attempts, \
             CAST(tenant_id AS TEXT) AS tenant_id, CAST(project_id AS TEXT) AS project_id, created_by_id \
             FROM core_task WHERE (status = 'queued' AND available_at <= ?) \
               OR ((status = 'running' OR status = 'cancel_requested') AND lease_expires_at < ?) \
             ORDER BY available_at, created_at LIMIT 1"
        };
        let candidate = sqlx::query(sql)
            .bind(&now)
            .bind(&now)
            .fetch_optional(pool)
            .await
            .map_err(|error| error.to_string())?;
        let Some(row) = candidate else {
            return Ok(None);
        };
        let id: String = row.try_get("id").unwrap_or_default();
        let status: String = row.try_get("status").unwrap_or_default();
        let attempts: i64 = row.try_get::<i64, _>("attempts").unwrap_or(0);
        let max_attempts: i64 = row.try_get::<i64, _>("max_attempts").unwrap_or(3);
        if status == "cancel_requested" {
            mark_cancelled(pool, state, &id).await?;
            continue;
        }
        if status == "running" && attempts >= max_attempts {
            mark_lease_expired(pool, state, &id).await?;
            continue;
        }
        let lease_token = uuid::Uuid::new_v4().to_string();
        let expires = (chrono::Utc::now() + chrono::Duration::seconds(150))
            .to_rfc3339_opts(chrono::SecondsFormat::Micros, false);
        let update = if postgres {
            "UPDATE core_task SET status = 'running', attempts = attempts + 1, lease_owner = ?, \
             lease_token = ?, lease_expires_at = ?::timestamptz, heartbeat_at = ?::timestamptz, \
             started_at = COALESCE(started_at, ?::timestamptz), updated_at = ?::timestamptz \
             WHERE CAST(id AS TEXT) = ? AND ((status = 'queued' AND available_at <= ?::timestamptz) \
               OR ((status = 'running' OR status = 'cancel_requested') AND lease_expires_at < ?::timestamptz))"
        } else {
            "UPDATE core_task SET status = 'running', attempts = attempts + 1, lease_owner = ?, \
             lease_token = ?, lease_expires_at = ?, heartbeat_at = ?, \
             started_at = COALESCE(started_at, ?), updated_at = ? \
             WHERE CAST(id AS TEXT) = ? AND ((status = 'queued' AND available_at <= ?) \
               OR ((status = 'running' OR status = 'cancel_requested') AND lease_expires_at < ?))"
        };
        let updated = sqlx::query(update)
            .bind(worker_id)
            .bind(&lease_token)
            .bind(&expires)
            .bind(&now)
            .bind(&now)
            .bind(&now)
            .bind(&id)
            .bind(&now)
            .bind(&now)
            .execute(pool)
            .await
            .map_err(|error| error.to_string())?;
        if updated.rows_affected() != 1 {
            continue;
        }
        return Ok(Some(ClaimedTask {
            id,
            task_type: row.try_get("task_type").unwrap_or_default(),
            payload: parse_json(row.try_get::<Option<String>, _>("payload").ok().flatten()),
            attempts: attempts + 1,
            max_attempts,
            lease_token,
            tenant_id: row.try_get("tenant_id").unwrap_or_default(),
            project_id: row.try_get("project_id").unwrap_or_default(),
            created_by_id: row.try_get::<i64, _>("created_by_id").unwrap_or(1),
        }));
    }
    Ok(None)
}

fn parse_json(value: Option<String>) -> Value {
    value
        .and_then(|text| serde_json::from_str::<Value>(&text).ok())
        .unwrap_or_else(|| json!({}))
}

pub(crate) async fn renew_lease(
    pool: &AnyPool,
    state: &AppState,
    task_id: &str,
    worker_id: &str,
    lease_token: &str,
) -> Result<(), String> {
    let postgres = is_postgres(state);
    let now = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false);
    let expires = (chrono::Utc::now() + chrono::Duration::seconds(150))
        .to_rfc3339_opts(chrono::SecondsFormat::Micros, false);
    let sql = if postgres {
        "UPDATE core_task SET heartbeat_at = ?::timestamptz, lease_expires_at = ?::timestamptz, \
         updated_at = ?::timestamptz WHERE CAST(id AS TEXT) = ? AND lease_token = ? \
         AND lease_owner = ? AND status = 'running'"
    } else {
        "UPDATE core_task SET heartbeat_at = ?, lease_expires_at = ?, updated_at = ? \
         WHERE CAST(id AS TEXT) = ? AND lease_token = ? AND lease_owner = ? AND status = 'running'"
    };
    let updated = sqlx::query(sql)
        .bind(&now)
        .bind(&expires)
        .bind(&now)
        .bind(task_id)
        .bind(lease_token)
        .bind(worker_id)
        .execute(pool)
        .await
        .map_err(|error| error.to_string())?;
    if updated.rows_affected() != 1 {
        return Err("Task lease was lost or cancellation was requested.".into());
    }
    Ok(())
}

async fn finish_success(
    pool: &AnyPool,
    state: &AppState,
    task: &ClaimedTask,
    result: &Value,
) -> Result<(), String> {
    let postgres = is_postgres(state);
    let now = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false);
    let sql = if postgres {
        "UPDATE core_task SET status = CASE WHEN status = 'cancel_requested' THEN 'cancelled' ELSE 'succeeded' END, \
         result = ?::jsonb, error = NULL, finished_at = ?::timestamptz, lease_owner = NULL, \
         lease_token = NULL, lease_expires_at = NULL, updated_at = ?::timestamptz \
         WHERE CAST(id AS TEXT) = ? AND lease_token = ?"
    } else {
        "UPDATE core_task SET status = CASE WHEN status = 'cancel_requested' THEN 'cancelled' ELSE 'succeeded' END, \
         result = ?, error = NULL, finished_at = ?, lease_owner = NULL, \
         lease_token = NULL, lease_expires_at = NULL, updated_at = ? \
         WHERE CAST(id AS TEXT) = ? AND lease_token = ?"
    };
    let updated = sqlx::query(sql)
        .bind(result.to_string())
        .bind(&now)
        .bind(&now)
        .bind(&task.id)
        .bind(&task.lease_token)
        .execute(pool)
        .await
        .map_err(|error| error.to_string())?;
    if updated.rows_affected() != 1 {
        return Ok(());
    }
    finish_runs(pool, state, task, &now, &Value::Null).await
}

async fn finish_failure(
    pool: &AnyPool,
    state: &AppState,
    task: &ClaimedTask,
    error: &str,
) -> Result<(), String> {
    let postgres = is_postgres(state);
    let now = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false);
    let retryable = task.attempts < task.max_attempts
        && !error.contains("NO_AUTOMATIC_RETRY")
        && !error.contains("TENANT_TOKEN_BUDGET_EXCEEDED")
        && !error.contains("WORKFLOW_DEADLINE_EXCEEDED")
        && !error.starts_with("PERMANENT");
    let error_payload = json!({
        "code": if error.contains("TENANT_TOKEN_BUDGET_EXCEEDED") { "TENANT_TOKEN_BUDGET_EXCEEDED" } else { "TASK_FAILED" },
        "message": error,
        "retryable": false,
    });
    if retryable {
        let available = (chrono::Utc::now() + chrono::Duration::seconds(5 * task.attempts))
            .to_rfc3339_opts(chrono::SecondsFormat::Micros, false);
        let sql = if postgres {
            "UPDATE core_task SET status = 'queued', error = ?::jsonb, available_at = ?::timestamptz, \
             lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, updated_at = ?::timestamptz \
             WHERE CAST(id AS TEXT) = ? AND lease_token = ?"
        } else {
            "UPDATE core_task SET status = 'queued', error = ?, available_at = ?, \
             lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, updated_at = ? \
             WHERE CAST(id AS TEXT) = ? AND lease_token = ?"
        };
        sqlx::query(sql)
            .bind(error_payload.to_string())
            .bind(&available)
            .bind(&now)
            .bind(&task.id)
            .bind(&task.lease_token)
            .execute(pool)
            .await
            .map_err(|error| error.to_string())?;
        return Ok(());
    }
    let sql = if postgres {
        "UPDATE core_task SET status = 'failed', error = ?::jsonb, finished_at = ?::timestamptz, \
         lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, updated_at = ?::timestamptz \
         WHERE CAST(id AS TEXT) = ? AND lease_token = ?"
    } else {
        "UPDATE core_task SET status = 'failed', error = ?, finished_at = ?, \
         lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, updated_at = ? \
         WHERE CAST(id AS TEXT) = ? AND lease_token = ?"
    };
    let updated = sqlx::query(sql)
        .bind(error_payload.to_string())
        .bind(&now)
        .bind(&now)
        .bind(&task.id)
        .bind(&task.lease_token)
        .execute(pool)
        .await
        .map_err(|error| error.to_string())?;
    if updated.rows_affected() == 1 {
        finish_runs(pool, state, task, &now, &error_payload).await?;
    }
    Ok(())
}

async fn finish_runs(
    pool: &AnyPool,
    state: &AppState,
    task: &ClaimedTask,
    now: &str,
    error: &Value,
) -> Result<(), String> {
    let postgres = is_postgres(state);
    if task.task_type == "agent.run" {
        let sql = if postgres {
            "UPDATE core_agentrun SET finished_at = ?::timestamptz WHERE CAST(task_id AS TEXT) = ?"
        } else {
            "UPDATE core_agentrun SET finished_at = ? WHERE CAST(task_id AS TEXT) = ?"
        };
        sqlx::query(sql)
            .bind(now)
            .bind(&task.id)
            .execute(pool)
            .await
            .map_err(|error| error.to_string())?;
    }
    if task.task_type == "workflow.run" || task.task_type == "team.run" {
        if !error.is_null() {
            crate::workflow_runtime::fail_run(
                pool,
                state,
                &task.id,
                error
                    .get("message")
                    .and_then(Value::as_str)
                    .unwrap_or("Workflow run failed."),
            )
            .await?;
        }
        if task.task_type == "team.run" {
            let sql = if postgres {
                "UPDATE core_agentteamrun SET finished_at = ?::timestamptz WHERE CAST(workflow_run_id AS TEXT) = (SELECT CAST(id AS TEXT) FROM core_workflowrun WHERE CAST(task_id AS TEXT) = ?)"
            } else {
                "UPDATE core_agentteamrun SET finished_at = ? WHERE CAST(workflow_run_id AS TEXT) = (SELECT CAST(id AS TEXT) FROM core_workflowrun WHERE CAST(task_id AS TEXT) = ?)"
            };
            sqlx::query(sql)
                .bind(now)
                .bind(&task.id)
                .execute(pool)
                .await
                .map_err(|error| error.to_string())?;
        }
    }
    Ok(())
}

async fn mark_cancelled(pool: &AnyPool, state: &AppState, task_id: &str) -> Result<(), String> {
    let postgres = is_postgres(state);
    let now = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false);
    let sql = if postgres {
        "UPDATE core_task SET status = 'cancelled', finished_at = ?::timestamptz, lease_owner = NULL, \
         lease_token = NULL, lease_expires_at = NULL, updated_at = ?::timestamptz WHERE CAST(id AS TEXT) = ?"
    } else {
        "UPDATE core_task SET status = 'cancelled', finished_at = ?, lease_owner = NULL, \
         lease_token = NULL, lease_expires_at = NULL, updated_at = ? WHERE CAST(id AS TEXT) = ?"
    };
    sqlx::query(sql)
        .bind(&now)
        .bind(&now)
        .bind(task_id)
        .execute(pool)
        .await
        .map_err(|error| error.to_string())?;
    for table in ["core_agentrun", "core_workflowrun"] {
        let sql = if postgres {
            format!(
                "UPDATE {table} SET finished_at = ?::timestamptz WHERE CAST(task_id AS TEXT) = ?"
            )
        } else {
            format!("UPDATE {table} SET finished_at = ? WHERE CAST(task_id AS TEXT) = ?")
        };
        sqlx::query(&sql)
            .bind(&now)
            .bind(task_id)
            .execute(pool)
            .await
            .map_err(|error| error.to_string())?;
    }
    Ok(())
}

async fn mark_lease_expired(pool: &AnyPool, state: &AppState, task_id: &str) -> Result<(), String> {
    let postgres = is_postgres(state);
    let now = chrono::Utc::now().to_rfc3339_opts(chrono::SecondsFormat::Micros, false);
    let payload = json!({"code": "LEASE_EXPIRED", "message": "The task lease expired after the maximum number of attempts.", "retryable": false}).to_string();
    let sql = if postgres {
        "UPDATE core_task SET status = 'failed', error = ?::jsonb, finished_at = ?::timestamptz, \
         lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, updated_at = ?::timestamptz WHERE CAST(id AS TEXT) = ?"
    } else {
        "UPDATE core_task SET status = 'failed', error = ?, finished_at = ?, \
         lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, updated_at = ? WHERE CAST(id AS TEXT) = ?"
    };
    sqlx::query(sql)
        .bind(payload)
        .bind(&now)
        .bind(&now)
        .bind(task_id)
        .execute(pool)
        .await
        .map_err(|error| error.to_string())?;
    Ok(())
}
