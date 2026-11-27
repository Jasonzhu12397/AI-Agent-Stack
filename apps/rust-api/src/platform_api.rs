#![allow(clippy::result_large_err)]

use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;
use sqlx::Row;

use crate::context::{authenticate, AuthError};
use crate::proxy::AppState;

pub async fn roadmap(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let Some(pool) = state.identity_pool.as_ref() else {
        return unavailable();
    };
    let Some(secret_key) = state.config.django_secret_key.as_deref() else {
        return unavailable();
    };
    if let Err(response) = match authenticate(pool, secret_key, &headers).await {
        Ok(_) => Ok(()),
        Err(AuthError::Unauthenticated(detail)) => {
            Err((StatusCode::UNAUTHORIZED, Json(json!({"detail": detail}))).into_response())
        }
        Err(AuthError::Unavailable(message)) => {
            tracing::error!(%message, "rust roadmap could not resolve the session");
            Err(unavailable())
        }
    } {
        return response;
    }
    let sql = "SELECT \
         (SELECT COUNT(*) FROM core_agentversion) AS published_agents, \
         (SELECT COUNT(*) FROM core_agentrun run JOIN core_task task \
            ON CAST(task.id AS TEXT) = CAST(run.task_id AS TEXT) \
            WHERE task.status = 'succeeded') AS succeeded_runs, \
         (SELECT COUNT(*) FROM core_workflowversion) AS published_workflows, \
         (SELECT COUNT(*) FROM core_workflowsteprun WHERE status = 'succeeded') AS succeeded_steps, \
         (SELECT COUNT(*) FROM core_agentnode) AS enrolled_nodes, \
         (SELECT COUNT(*) FROM core_agentnode WHERE status = 'online') AS online_nodes, \
         (SELECT COUNT(*) FROM core_agentcommand WHERE status IN ('succeeded', 'failed')) AS settled_commands, \
         (SELECT COUNT(*) FROM core_agentcommand WHERE approved_at IS NOT NULL) AS approved_commands, \
         (SELECT COUNT(DISTINCT workflow_run_id) FROM core_workflowsteprun) AS workflow_runs, \
         (SELECT COUNT(DISTINCT command_type) FROM core_agentcommand) AS command_kinds, \
         (SELECT COUNT(*) FROM core_task) AS tasks";
    let row = match sqlx::query(sql).fetch_one(pool).await {
        Ok(row) => row,
        Err(error) => return database_error(error.to_string()),
    };
    let tasks = count(&row, "tasks");
    let published_agents = count(&row, "published_agents");
    let succeeded_runs = count(&row, "succeeded_runs");
    let published_workflows = count(&row, "published_workflows");
    let succeeded_steps = count(&row, "succeeded_steps");
    let enrolled_nodes = count(&row, "enrolled_nodes");
    let online_nodes = count(&row, "online_nodes");
    let settled_commands = count(&row, "settled_commands");
    let approved_commands = count(&row, "approved_commands");
    let workflow_runs = count(&row, "workflow_runs");
    let command_kinds = count(&row, "command_kinds");
    Json(json!({"items": [
        {
            "code": "M0",
            "title": "Platform foundation",
            "detail": "Django API, Vue Studio, PostgreSQL, Worker and Compose",
            "status": "verified",
            "evidence": {"tasks": tasks},
        },
        {
            "code": "M1",
            "title": "Agent runtime",
            "detail": "Published Agent versions and asynchronous LLM runs",
            "status": milestone(&[published_agents, succeeded_runs]),
            "evidence": {"published_versions": published_agents, "succeeded_runs": succeeded_runs},
        },
        {
            "code": "M2",
            "title": "Workflow engine",
            "detail": "Versioned DAG, LLM/Condition nodes and persisted steps",
            "status": milestone(&[published_workflows, succeeded_steps]),
            "evidence": {
                "published_versions": published_workflows,
                "succeeded_steps": succeeded_steps,
                "runs": workflow_runs,
            },
        },
        {
            "code": "M3",
            "title": "Multi-agent control",
            "detail": "Cloud Control, Target and EPH agents: enrollment, command leases and approval gate",
            "status": milestone(&[enrolled_nodes, settled_commands]),
            "evidence": {
                "enrolled_nodes": enrolled_nodes,
                "online_nodes": online_nodes,
                "settled_commands": settled_commands,
                "approved_commands": approved_commands,
                "command_types": command_kinds,
            },
        },
    ]}))
    .into_response()
}

fn count(row: &sqlx::any::AnyRow, name: &str) -> i64 {
    row.try_get::<i64, _>(name).unwrap_or(0)
}

fn milestone(counts: &[i64]) -> &'static str {
    let present = counts.iter().filter(|value| **value > 0).count();
    if present == counts.len() {
        "verified"
    } else if present == 0 {
        "in_progress"
    } else {
        "partial"
    }
}

fn unavailable() -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({"detail": "The service is temporarily unavailable."})),
    )
        .into_response()
}

fn database_error(error: String) -> Response {
    tracing::error!(%error, "rust platform api database operation failed");
    unavailable()
}
