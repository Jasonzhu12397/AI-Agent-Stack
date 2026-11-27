#![allow(clippy::result_large_err)]

//! Native cluster deployment read surface.

use axum::{
    extract::{Path, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use serde_json::{json, Value};
use sqlx::Row;

use crate::proxy::AppState;

fn fail(status: StatusCode, code: &str, message: &str) -> Response {
    (status, Json(json!({"detail": message, "code": code}))).into_response()
}

fn column(state: &AppState, alias: &str, name: &str) -> String {
    let postgres = state
        .config
        .database_url
        .as_deref()
        .is_some_and(|url| url.starts_with("postgres"));
    if postgres {
        format!("to_char({alias}.{name} AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')")
    } else {
        format!("strftime('%Y-%m-%d %H:%M:%S', {alias}.{name})")
    }
}

fn json_text(state: &AppState) -> bool {
    state
        .config
        .database_url
        .as_deref()
        .is_some_and(|url| url.starts_with("postgres"))
}

fn parse_json(value: Option<String>) -> Value {
    value
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or(Value::Null)
}

fn network_spec_version(spec: &Value) -> i64 {
    let version = if let Some(nested) = spec.get("network_spec") {
        nested.get("network_spec_version")
    } else {
        spec.get("network_spec_version")
    };
    version
        .and_then(Value::as_i64)
        .filter(|value| *value >= 1)
        .unwrap_or(1)
}

fn network_spec_migration(spec: &Value) -> Value {
    if !spec.is_object() {
        return Value::Null;
    }
    let version = network_spec_version(spec);
    if version >= 2 {
        return Value::Null;
    }
    json!({
        "code": "NETWORK_SPEC_LEGACY",
        "version": version,
        "current_version": 2,
        "detail": "This deployment was planned before VLAN sub-interfaces were modelled. Its network spec still names the physical interface, which is not what a re-plan would produce. Re-plan the cluster from the current profile before re-running installation.",
    })
}

/// `GET /api/v1/tenants/{tenant}/projects/{project}/cluster-deployments/`
pub async fn list_deployments(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path((tenant_id, project_id)): Path<(String, String)>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return fail(
            StatusCode::SERVICE_UNAVAILABLE,
            "UNAVAILABLE",
            "Database unavailable.",
        );
    };
    if !matches!(
        crate::tenancy::project_access(pool, &tenant_id, &project_id, user.id).await,
        Ok(true)
    ) {
        return fail(StatusCode::NOT_FOUND, "NOT_FOUND", "Project not found.");
    }
    let created = column(&state, "deployment", "created_at");
    let updated = column(&state, "deployment", "updated_at");
    let started = column(&state, "deployment", "started_at");
    let finished = column(&state, "deployment", "finished_at");
    let text = |field: &str, alias: &str| {
        if json_text(&state) {
            format!("CAST({alias}.{field} AS TEXT)")
        } else {
            format!("{alias}.{field}")
        }
    };
    let sql = format!(
        "SELECT CAST(deployment.id AS TEXT) AS id, CAST(deployment.tenant_id AS TEXT) AS tenant_id, \
         CAST(deployment.project_id AS TEXT) AS project_id, deployment.cluster_name, \
         deployment.kubernetes_version, CAST(deployment.eph_node_id AS TEXT) AS eph_node_id, \
         CAST(deployment.target_node_id AS TEXT) AS target_node_id, deployment.status, \
         {} AS spec_text, {} AS plan_text, {} AS preflight_text, {} AS error_text, \
         {created} AS created, {updated} AS updated, {started} AS started, {finished} AS finished \
         FROM core_clusterdeployment deployment WHERE CAST(deployment.tenant_id AS TEXT) = ? \
         AND CAST(deployment.project_id AS TEXT) = ? ORDER BY deployment.created_at DESC LIMIT 50",
        text("spec", "deployment"),
        text("plan_result", "deployment"),
        text("preflight_result", "deployment"),
        text("error", "deployment"),
    );
    let rows = match sqlx::query(&sql)
        .bind(&tenant_id)
        .bind(&project_id)
        .fetch_all(pool)
        .await
    {
        Ok(rows) => rows,
        Err(error) => {
            return fail(
                StatusCode::INTERNAL_SERVER_ERROR,
                "DATABASE_ERROR",
                &error.to_string(),
            )
        }
    };
    let results: Vec<Value> = rows
        .iter()
        .map(|row| {
            let spec = parse_json(row.try_get::<Option<String>, _>("spec_text").ok().flatten());
            let plan = parse_json(row.try_get::<Option<String>, _>("plan_text").ok().flatten());
            json!({
                "id": row.try_get::<String, _>("id").unwrap_or_default(),
                "tenant_id": row.try_get::<String, _>("tenant_id").unwrap_or_default(),
                "project_id": row.try_get::<String, _>("project_id").unwrap_or_default(),
                "cluster_name": row.try_get::<String, _>("cluster_name").unwrap_or_default(),
                "kubernetes_version": row.try_get::<String, _>("kubernetes_version").unwrap_or_default(),
                "eph_node_id": row.try_get::<String, _>("eph_node_id").unwrap_or_default(),
                "target_node_id": row.try_get::<Option<String>, _>("target_node_id").ok().flatten(),
                "status": row.try_get::<String, _>("status").unwrap_or_default(),
                "network_spec_status": {
                    "version": network_spec_version(&spec),
                    "current_version": 2,
                    "migration": network_spec_migration(&spec),
                },
                "spec": spec,
                "plan_result": plan,
                "preflight_result": parse_json(row.try_get::<Option<String>, _>("preflight_text").ok().flatten()),
                "cloud_control": plan.get("cloud_control").cloned().unwrap_or(Value::Null),
                "error": parse_json(row.try_get::<Option<String>, _>("error_text").ok().flatten()),
                "created_at": row.try_get::<Option<String>, _>("created").ok().flatten(),
                "updated_at": row.try_get::<Option<String>, _>("updated").ok().flatten(),
                "started_at": row.try_get::<Option<String>, _>("started").ok().flatten(),
                "finished_at": row.try_get::<Option<String>, _>("finished").ok().flatten(),
            })
        })
        .collect();
    Json(json!({"count": results.len(), "results": results})).into_response()
}

/// `GET /api/v1/cluster-deployments/{deployment_id}/`
pub async fn get_deployment(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(deployment_id): Path<String>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return fail(
            StatusCode::SERVICE_UNAVAILABLE,
            "UNAVAILABLE",
            "Database unavailable.",
        );
    };
    let scope = sqlx::query(
        "SELECT CAST(tenant_id AS TEXT) AS tenant_id, CAST(project_id AS TEXT) AS project_id \
         FROM core_clusterdeployment WHERE CAST(id AS TEXT) = ? LIMIT 1",
    )
    .bind(&deployment_id)
    .fetch_optional(pool)
    .await;
    let (tenant_id, project_id) = match scope {
        Ok(Some(row)) => (
            row.try_get::<String, _>("tenant_id").unwrap_or_default(),
            row.try_get::<String, _>("project_id").unwrap_or_default(),
        ),
        Ok(None) => return fail(StatusCode::NOT_FOUND, "NOT_FOUND", "Not found."),
        Err(error) => {
            return fail(
                StatusCode::INTERNAL_SERVER_ERROR,
                "DATABASE_ERROR",
                &error.to_string(),
            )
        }
    };
    match crate::tenancy::project_access(pool, &tenant_id, &project_id, user.id).await {
        Ok(true) => {}
        Ok(false) => return fail(StatusCode::NOT_FOUND, "NOT_FOUND", "Not found."),
        Err(error) => return fail(StatusCode::INTERNAL_SERVER_ERROR, "DATABASE_ERROR", &error),
    }
    let created = column(&state, "deployment", "created_at");
    let updated = column(&state, "deployment", "updated_at");
    let started = column(&state, "deployment", "started_at");
    let finished = column(&state, "deployment", "finished_at");
    let text = |field: &str| {
        if json_text(&state) {
            format!("CAST(deployment.{field} AS TEXT)")
        } else {
            format!("deployment.{field}")
        }
    };
    let sql = format!(
        "SELECT CAST(deployment.id AS TEXT) AS id, CAST(deployment.tenant_id AS TEXT) AS tenant_id, \
         CAST(deployment.project_id AS TEXT) AS project_id, deployment.cluster_name, \
         deployment.kubernetes_version, CAST(deployment.eph_node_id AS TEXT) AS eph_node_id, \
         CAST(deployment.target_node_id AS TEXT) AS target_node_id, deployment.status, \
         {} AS spec_text, {} AS plan_text, {} AS preflight_text, {} AS error_text, \
         {created} AS created, {updated} AS updated, {started} AS started, {finished} AS finished \
         FROM core_clusterdeployment deployment WHERE CAST(deployment.id AS TEXT) = ? LIMIT 1",
        text("spec"),
        text("plan_result"),
        text("preflight_result"),
        text("error"),
    );
    let row = match sqlx::query(&sql)
        .bind(&deployment_id)
        .fetch_optional(pool)
        .await
    {
        Ok(Some(row)) => row,
        Ok(None) => return fail(StatusCode::NOT_FOUND, "NOT_FOUND", "Not found."),
        Err(error) => {
            return fail(
                StatusCode::INTERNAL_SERVER_ERROR,
                "DATABASE_ERROR",
                &error.to_string(),
            )
        }
    };
    let spec = parse_json(row.try_get::<Option<String>, _>("spec_text").ok().flatten());
    let plan = parse_json(row.try_get::<Option<String>, _>("plan_text").ok().flatten());
    let evidence = spec.get("network_evidence").and_then(Value::as_array);
    let evidence_status = match evidence {
        None => json!("missing"),
        Some(items) if items.is_empty() => json!("missing"),
        Some(items) => {
            let drift = network_evidence_drift(pool, &project_id, items).await;
            if drift.is_empty() {
                json!({"status": "current", "drift": []})
            } else {
                json!({"status": "drift", "drift": drift})
            }
        }
    };
    Json(json!({
        "id": row.try_get::<String, _>("id").unwrap_or_default(),
        "tenant_id": row.try_get::<String, _>("tenant_id").unwrap_or_default(),
        "project_id": row.try_get::<String, _>("project_id").unwrap_or_default(),
        "cluster_name": row.try_get::<String, _>("cluster_name").unwrap_or_default(),
        "kubernetes_version": row.try_get::<String, _>("kubernetes_version").unwrap_or_default(),
        "eph_node_id": row.try_get::<String, _>("eph_node_id").unwrap_or_default(),
        "target_node_id": row.try_get::<Option<String>, _>("target_node_id").ok().flatten(),
        "status": row.try_get::<String, _>("status").unwrap_or_default(),
        "network_spec_status": {
            "version": network_spec_version(&spec),
            "current_version": 2,
            "migration": network_spec_migration(&spec),
        },
        "spec": spec,
        "plan_result": plan,
        "preflight_result": parse_json(row.try_get::<Option<String>, _>("preflight_text").ok().flatten()),
        "cloud_control": plan.get("cloud_control").cloned().unwrap_or(Value::Null),
        "error": parse_json(row.try_get::<Option<String>, _>("error_text").ok().flatten()),
        "created_at": row.try_get::<Option<String>, _>("created").ok().flatten(),
        "updated_at": row.try_get::<Option<String>, _>("updated").ok().flatten(),
        "started_at": row.try_get::<Option<String>, _>("started").ok().flatten(),
        "finished_at": row.try_get::<Option<String>, _>("finished").ok().flatten(),
        "network_evidence_status": evidence_status,
    }))
    .into_response()
}

async fn network_evidence_drift(
    pool: &sqlx::AnyPool,
    project_id: &str,
    evidence: &[Value],
) -> Vec<Value> {
    let mut drift = Vec::new();
    for item in evidence {
        let Some(object) = item.as_object() else {
            drift.push(json!({"machine_id": Value::Null, "code": "NETWORK_EVIDENCE_INVALID"}));
            continue;
        };
        let machine_id = object.get("machine_id").and_then(Value::as_str);
        let Some(machine_id) = machine_id else {
            drift.push(json!({"machine_id": Value::Null, "code": "NETWORK_EVIDENCE_INVALID"}));
            continue;
        };
        let machine = sqlx::query(
            "SELECT inventory_revision, strftime('%Y-%m-%d %H:%M:%f', observed_at) AS observed \
             FROM core_agentmachine WHERE CAST(project_id AS TEXT) = ? AND CAST(id AS TEXT) = ? LIMIT 1",
        )
        .bind(project_id)
        .bind(machine_id)
        .fetch_optional(pool)
        .await
        .ok()
        .flatten();
        let Some(machine) = machine else {
            drift.push(
                json!({"machine_id": machine_id, "code": "NETWORK_EVIDENCE_MACHINE_MISSING"}),
            );
            continue;
        };
        let current_revision = machine.try_get::<i64, _>("inventory_revision").unwrap_or(0);
        let expected_revision = object.get("inventory_revision").and_then(Value::as_i64);
        let current_observed = machine
            .try_get::<Option<String>, _>("observed")
            .ok()
            .flatten()
            .map(|value| normalize_observed(&value));
        let expected_observed = object
            .get("observed_at")
            .and_then(Value::as_str)
            .map(normalize_observed);
        if expected_revision != Some(current_revision) || expected_observed != current_observed {
            drift.push(json!({
                "machine_id": machine_id,
                "code": "NETWORK_EVIDENCE_STALE",
                "expected_revision": expected_revision,
                "current_revision": current_revision,
                "expected_observed_at": object.get("observed_at").cloned().unwrap_or(Value::Null),
                "current_observed_at": current_observed,
            }));
        }
    }
    drift
}

fn normalize_observed(value: &str) -> String {
    let mut text = value.trim().replace(' ', "T");
    if let Some(stripped) = text.strip_suffix("+00:00") {
        text = stripped.to_string();
    }
    if let Some(stripped) = text.strip_suffix('Z') {
        text = stripped.to_string();
    }
    while text.ends_with('0') && text.contains('.') {
        text.pop();
    }
    text.trim_end_matches('.').to_string()
}

/// `POST /api/v1/cluster-deployments/{deployment_id}/network/evidence/verify/`
pub async fn verify_network_evidence(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(deployment_id): Path<String>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return fail(
            StatusCode::SERVICE_UNAVAILABLE,
            "UNAVAILABLE",
            "Database unavailable.",
        );
    };
    let row = sqlx::query(
        "SELECT CAST(tenant_id AS TEXT) AS tenant_id, CAST(project_id AS TEXT) AS project_id, \
         CASE WHEN ? THEN CAST(spec AS TEXT) ELSE spec END AS spec_text \
         FROM core_clusterdeployment WHERE CAST(id AS TEXT) = ? LIMIT 1",
    )
    .bind(json_text(&state))
    .bind(&deployment_id)
    .fetch_optional(pool)
    .await;
    let row = match row {
        Ok(Some(row)) => row,
        Ok(None) => return fail(StatusCode::NOT_FOUND, "NOT_FOUND", "Not found."),
        Err(error) => {
            return fail(
                StatusCode::INTERNAL_SERVER_ERROR,
                "DATABASE_ERROR",
                &error.to_string(),
            )
        }
    };
    let tenant_id: String = row.try_get("tenant_id").unwrap_or_default();
    let project_id: String = row.try_get("project_id").unwrap_or_default();
    if !crate::billing_api::tenant_member(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
    {
        return fail(
            StatusCode::FORBIDDEN,
            "FORBIDDEN",
            "Project membership required.",
        );
    }
    if !matches!(
        crate::tenancy::project_access(pool, &tenant_id, &project_id, user.id).await,
        Ok(true)
    ) {
        return fail(
            StatusCode::FORBIDDEN,
            "FORBIDDEN",
            "Project membership required.",
        );
    }
    let spec = parse_json(row.try_get::<Option<String>, _>("spec_text").ok().flatten());
    let evidence = spec.get("network_evidence").and_then(Value::as_array);
    let Some(items) = evidence.filter(|items| !items.is_empty()) else {
        return (
            StatusCode::CONFLICT,
            Json(json!({"status": "missing", "detail": "This deployment has no bound network evidence."})),
        )
            .into_response();
    };
    let drift = network_evidence_drift(pool, &project_id, items).await;
    if drift.is_empty() {
        Json(json!({"status": "current", "evidence": items})).into_response()
    } else {
        (
            StatusCode::CONFLICT,
            Json(json!({
                "status": "drift",
                "detail": "Node network inventory changed after the readiness check. Re-run network collection and create a new deployment.",
                "drift": drift,
            })),
        )
            .into_response()
    }
}
