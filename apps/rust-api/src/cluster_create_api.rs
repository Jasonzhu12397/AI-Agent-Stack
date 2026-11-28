#![allow(clippy::result_large_err)]
//! Native cluster deployment creation (preflight queue only).

use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Value};
use sqlx::Row;

use crate::billing_api::require_user;
use crate::proxy::AppState;

fn fail(status: StatusCode, code: &str, message: &str) -> Response {
    (
        status,
        Json(json!({"error": {"code": code, "message": message, "details": {}}})),
    )
        .into_response()
}

fn database_error(message: String) -> Response {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({"error": {"code": "DATABASE_UNAVAILABLE", "message": message}})),
    )
        .into_response()
}

fn now_sql(state: &AppState) -> &'static str {
    if state
        .config
        .database_url
        .as_deref()
        .unwrap_or("")
        .starts_with("postgres")
    {
        "NOW()"
    } else {
        "datetime('now')"
    }
}

pub async fn create_deployment(
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
        return database_error("Identity pool is not configured.".into());
    };
    match crate::tasks::project_editor(pool, &project_id, user.id).await {
        Ok(true) => {}
        Ok(false) => {
            return fail(
                StatusCode::FORBIDDEN,
                "PROJECT_EDITOR_REQUIRED",
                "Project editor permission required.",
            )
        }
        Err(error) => return database_error(error),
    }
    if !payload.is_object() {
        return fail(
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
            "Request body must be a JSON object.",
        );
    }
    let eph_node_id = payload
        .get("eph_node_id")
        .and_then(Value::as_str)
        .and_then(|value| uuid::Uuid::parse_str(value).ok());
    let Some(eph_node_id) = eph_node_id else {
        return fail(
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
            "A valid eph_node_id is required.",
        );
    };
    let now = now_sql(&state);
    let freshness = if now == "NOW()" {
        "NOW() - INTERVAL '60 seconds'"
    } else {
        "datetime('now', '-60 seconds')"
    };
    let node_sql = "SELECT CAST(node.id AS TEXT) AS id, node.status \
         FROM core_agentnode node WHERE CAST(node.id AS TEXT) = ? \
           AND CAST(node.tenant_id AS TEXT) = ? AND CAST(node.project_id AS TEXT) = ? \
           AND node.node_type = 'eph_node' LIMIT 1";
    let node = match sqlx::query(node_sql)
        .bind(eph_node_id.to_string())
        .bind(&tenant_id)
        .bind(&project_id)
        .fetch_optional(pool)
        .await
    {
        Ok(Some(row)) => row,
        Ok(None) => {
            return fail(
                StatusCode::NOT_FOUND,
                "NOT_FOUND",
                "The selected EPH node was not found.",
            )
        }
        Err(error) => return database_error(error.to_string()),
    };
    let node_status: String = node.try_get("status").unwrap_or_default();
    let fresh_sql = format!(
        "SELECT CASE WHEN last_seen_at IS NOT NULL AND last_seen_at >= {freshness} \
         THEN 1 ELSE 0 END AS fresh FROM core_agentnode WHERE CAST(id AS TEXT) = ?"
    );
    let fresh: i64 = sqlx::query_scalar(&fresh_sql)
        .bind(eph_node_id.to_string())
        .fetch_optional(pool)
        .await
        .ok()
        .flatten()
        .unwrap_or(0);
    if node_status != "online" || fresh == 0 {
        return fail(
            StatusCode::CONFLICT,
            "EPH_NODE_STALE",
            "The selected EPH agent must have a fresh heartbeat.",
        );
    }
    let Some(raw_spec) = payload
        .get("cluster_spec")
        .filter(|value| value.is_object())
    else {
        return fail(
            StatusCode::BAD_REQUEST,
            "INVALID_CLUSTER_SPEC",
            "cluster_spec must be an object.",
        );
    };
    let cluster_name = raw_spec
        .get("cluster_name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if cluster_name.is_empty()
        || cluster_name.len() > 63
        || !cluster_name
            .chars()
            .all(|ch| ch.is_ascii_lowercase() || ch.is_ascii_digit() || ch == '-')
    {
        return fail(
            StatusCode::BAD_REQUEST,
            "INVALID_CLUSTER_SPEC",
            "cluster_name must be a lowercase DNS label up to 63 characters.",
        );
    }
    let kubernetes_version = raw_spec
        .get("kubernetes_version")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    if kubernetes_version.is_empty() || kubernetes_version.len() > 40 {
        return fail(
            StatusCode::BAD_REQUEST,
            "INVALID_CLUSTER_SPEC",
            "kubernetes_version is required.",
        );
    }
    let idempotency_key = headers
        .get("idempotency-key")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .trim()
        .to_string();
    if idempotency_key.is_empty() || idempotency_key.len() > 128 {
        return fail(
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
            "A valid Idempotency-Key header is required.",
        );
    }
    let text = |field: &str| {
        if now == "NOW()" {
            format!("CAST(deployment.{field} AS TEXT)")
        } else {
            format!("deployment.{field}")
        }
    };
    let existing_sql = format!(
        "SELECT CAST(deployment.id AS TEXT) AS id, deployment.cluster_name, \
         deployment.kubernetes_version, CAST(deployment.eph_node_id AS TEXT) AS eph_node_id, \
         deployment.status, {} AS spec_text FROM core_clusterdeployment deployment \
         WHERE CAST(deployment.tenant_id AS TEXT) = ? AND deployment.idempotency_key = ? LIMIT 1",
        text("spec")
    );
    if let Ok(Some(row)) = sqlx::query(&existing_sql)
        .bind(&tenant_id)
        .bind(&idempotency_key)
        .fetch_optional(pool)
        .await
    {
        let existing_project: String = row.try_get("id").unwrap_or_default();
        let existing_node: String = row.try_get("eph_node_id").unwrap_or_default();
        let existing_spec: Option<String> = row.try_get("spec_text").ok();
        let same = existing_node == eph_node_id.to_string()
            && existing_spec
                .and_then(|text| serde_json::from_str::<Value>(&text).ok())
                .map(|value| value == *raw_spec)
                .unwrap_or(false);
        if !same {
            return fail(
                StatusCode::CONFLICT,
                "IDEMPOTENCY_MISMATCH",
                "Idempotency-Key was used for a different cluster plan.",
            );
        }
        return Json(json!({
            "id": existing_project,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "cluster_name": row.try_get::<String, _>("cluster_name").unwrap_or_default(),
            "kubernetes_version": row.try_get::<String, _>("kubernetes_version").unwrap_or_default(),
            "eph_node_id": existing_node,
            "status": row.try_get::<String, _>("status").unwrap_or_default(),
            "spec": raw_spec,
            "plan_result": {},
            "preflight_result": {},
            "cloud_control": null,
            "error": null,
            "created": false,
        }))
        .into_response();
    }
    let deployment_id = uuid::Uuid::new_v4().simple().to_string();
    let insert = format!(
        "INSERT INTO core_clusterdeployment (id, tenant_id, project_id, eph_node_id, target_node_id, \
         requested_by_id, cluster_name, kubernetes_version, spec, plan_result, preflight_result, status, \
         idempotency_key, error, created_at, updated_at) \
         VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, '{{}}', '{{}}', 'preflight_queued', ?, '', {now}, {now})"
    );
    if let Err(error) = sqlx::query(&insert)
        .bind(&deployment_id)
        .bind(&tenant_id)
        .bind(&project_id)
        .bind(eph_node_id.to_string())
        .bind(user.id)
        .bind(cluster_name)
        .bind(&kubernetes_version)
        .bind(serde_json::to_string(raw_spec).unwrap_or_default())
        .bind(&idempotency_key)
        .execute(pool)
        .await
    {
        let message = error.to_string();
        if message.to_ascii_lowercase().contains("unique") {
            return fail(
                StatusCode::CONFLICT,
                "IDEMPOTENCY_CONFLICT",
                "A deployment with this idempotency key already exists.",
            );
        }
        return database_error(message);
    }
    let _ = crate::audit::record_event(
        pool,
        state.config.database_url.as_deref().unwrap_or(""),
        crate::audit::AuditEvent {
            tenant_id: Some(&tenant_id),
            project_id: Some(&project_id),
            event_type: "cluster.deployment.created",
            resource_type: "cluster_deployment",
            resource_id: &deployment_id,
            outcome: "success",
            actor_id: Some(user.id),
            request_id: "",
            remote_addr: None,
            user_agent: "",
            metadata: json!({"cluster_name": cluster_name, "kubernetes_version": kubernetes_version}),
        },
    )
    .await;
    (
        StatusCode::CREATED,
        Json(json!({
            "id": deployment_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "cluster_name": cluster_name,
            "kubernetes_version": kubernetes_version,
            "eph_node_id": eph_node_id.to_string(),
            "status": "preflight_queued",
            "spec": raw_spec,
            "plan_result": {},
            "preflight_result": {},
            "cloud_control": null,
            "error": null,
            "created": true,
        })),
    )
        .into_response()
}
