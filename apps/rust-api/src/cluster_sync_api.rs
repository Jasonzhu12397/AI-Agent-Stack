#![allow(clippy::result_large_err)]

use std::time::Duration;

use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use chrono::Utc;
use serde_json::{json, Value};
use sqlx::AnyPool;

use crate::proxy::AppState;

const EPH_PREFLIGHT_ACTION: &str = "eph.preflight";
const EPH_PREFLIGHT_KEY: &str = "eph_preflight";

fn fail(status: StatusCode, message: &str, code: &str) -> Response {
    (
        status,
        Json(json!({"error": {"code": code, "message": message, "details": {}}})),
    )
        .into_response()
}

fn cloud_control_client() -> Result<reqwest::Client, ()> {
    let url = std::env::var("CLOUD_CONTROL_API_URL").unwrap_or_default();
    let ca = std::env::var("CLOUD_CONTROL_CA_FILE").unwrap_or_default();
    let cert = std::env::var("CLOUD_CONTROL_CLIENT_CERT_FILE").unwrap_or_default();
    let key = std::env::var("CLOUD_CONTROL_CLIENT_KEY_FILE").unwrap_or_default();
    if !url.starts_with("https://") || ca.is_empty() || cert.is_empty() || key.is_empty() {
        return Err(());
    }
    let ca_pem = std::fs::read(&ca).map_err(|_| ())?;
    let mut identity_pem = std::fs::read(&cert).map_err(|_| ())?;
    identity_pem.extend_from_slice(&std::fs::read(&key).map_err(|_| ())?);
    reqwest::Client::builder()
        .timeout(Duration::from_secs(15))
        .redirect(reqwest::redirect::Policy::none())
        .add_root_certificate(reqwest::Certificate::from_pem(&ca_pem).map_err(|_| ())?)
        .identity(reqwest::Identity::from_pem(&identity_pem).map_err(|_| ())?)
        .build()
        .map_err(|_| ())
}

async fn fetch_action(deployment_id: &str, action: &str) -> Result<Value, String> {
    let client = cloud_control_client().map_err(|_| "CLOUD_CONTROL_NOT_CONFIGURED".to_string())?;
    let base = std::env::var("CLOUD_CONTROL_API_URL").unwrap_or_default();
    let response = client
        .get(format!(
            "{}/api/v1/control/deployments/{deployment_id}/actions/{action}/result",
            base.trim_end_matches('/')
        ))
        .send()
        .await
        .map_err(|_| "CLOUD_CONTROL_REQUEST_FAILED".to_string())?;
    if !response.status().is_success() {
        return Err("CLOUD_CONTROL_REQUEST_FAILED".into());
    }
    let body: Value = response
        .json()
        .await
        .map_err(|_| "CLOUD_CONTROL_REQUEST_FAILED".to_string())?;
    if !body.is_object() {
        return Err("CLOUD_CONTROL_REQUEST_FAILED".into());
    }
    Ok(body)
}

fn bounded_warnings(value: Option<&Value>) -> Vec<Value> {
    value
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .take(64)
                .map(|item| json!(item))
                .collect()
        })
        .unwrap_or_default()
}

fn preflight_snapshot(payload: &Value) -> Value {
    let output = payload.get("output").and_then(Value::as_object);
    json!({
        "available": payload.get("available").and_then(Value::as_bool).unwrap_or(false),
        "reason": payload.get("reason").cloned().unwrap_or(Value::Null),
        "status": payload.get("status").cloned().unwrap_or(Value::Null),
        "error_code": payload.get("error_code").cloned().unwrap_or(Value::Null),
        "eph_ready": output.and_then(|out| out.get("eph_ready")).cloned().unwrap_or(Value::Null),
        "warnings": bounded_warnings(output.and_then(|out| out.get("warnings"))),
        "observed_at": output.and_then(|out| out.get("observed_at")).cloned().unwrap_or(Value::Null),
        "command_id": payload.get("command_id").cloned().unwrap_or(Value::Null),
        "synced_at": Utc::now().format("%Y-%m-%dT%H:%M:%S%.6f+00:00").to_string(),
    })
}

async fn permission(
    pool: &AnyPool,
    deployment_id: &str,
    user_id: i64,
) -> Result<(String, String), String> {
    let row = sqlx::query(
        "SELECT CAST(tenant_id AS TEXT) AS tenant_id, CAST(project_id AS TEXT) AS project_id \
         FROM core_clusterdeployment WHERE CAST(id AS TEXT) = ? LIMIT 1",
    )
    .bind(deployment_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())?;
    let Some(row) = row else {
        return Err("NOT_FOUND".into());
    };
    use sqlx::Row;
    let tenant: String = row
        .try_get("tenant_id")
        .map_err(|error| error.to_string())?;
    let project: String = row
        .try_get("project_id")
        .map_err(|error| error.to_string())?;
    let tenant_member: Option<String> = sqlx::query_scalar(
        "SELECT role FROM core_tenantmembership WHERE CAST(tenant_id AS TEXT) = ? AND user_id = ? LIMIT 1",
    )
    .bind(&tenant)
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())?;
    if tenant_member.is_some() {
        return Ok((tenant, project));
    }
    let project_member: Option<String> = sqlx::query_scalar(
        "SELECT role FROM core_projectmembership WHERE CAST(project_id AS TEXT) = ? AND user_id = ? LIMIT 1",
    )
    .bind(&project)
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())?;
    if project_member.is_some() {
        return Ok((tenant, project));
    }
    Err("PERMISSION_DENIED".into())
}

pub async fn sync_eph_preflight(
    State(state): State<AppState>,
    Path(deployment_id): Path<String>,
    headers: HeaderMap,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return fail(
            StatusCode::SERVICE_UNAVAILABLE,
            "Database unavailable.",
            "DATABASE_UNAVAILABLE",
        );
    };
    let (_, _) = match permission(pool, &deployment_id, user.id).await {
        Ok(scope) => scope,
        Err(code) if code == "NOT_FOUND" => {
            return fail(StatusCode::NOT_FOUND, "Not found.", "NOT_FOUND")
        }
        Err(code) if code == "PERMISSION_DENIED" => {
            return fail(
                StatusCode::FORBIDDEN,
                "Project membership required.",
                "PERMISSION_DENIED",
            )
        }
        Err(code) => return fail(StatusCode::INTERNAL_SERVER_ERROR, &code, "DATABASE_ERROR"),
    };
    let payload = match fetch_action(&deployment_id, EPH_PREFLIGHT_ACTION).await {
        Ok(payload) => payload,
        Err(code) if code == "CLOUD_CONTROL_NOT_CONFIGURED" => {
            return fail(
                StatusCode::SERVICE_UNAVAILABLE,
                "Cloud Control is not configured.",
                "EPH_PREFLIGHT_UNAVAILABLE",
            );
        }
        Err(_) => {
            return fail(
                StatusCode::BAD_GATEWAY,
                "Cloud Control request failed.",
                "EPH_PREFLIGHT_UNAVAILABLE",
            )
        }
    };
    let snapshot = preflight_snapshot(&payload);
    let stored: Option<String> = sqlx::query_scalar(
        "SELECT preflight_result FROM core_clusterdeployment WHERE CAST(id AS TEXT) = ? LIMIT 1",
    )
    .bind(&deployment_id)
    .fetch_optional(pool)
    .await
    .unwrap_or(None);
    let mut document = stored
        .as_deref()
        .and_then(|raw| serde_json::from_str::<Value>(raw).ok())
        .and_then(|value| value.as_object().cloned())
        .unwrap_or_default();
    let unchanged = document.get(EPH_PREFLIGHT_KEY).is_some_and(|existing| {
        let mut existing = existing.clone();
        let mut incoming = snapshot.clone();
        if let (Some(a), Some(b)) = (existing.as_object_mut(), incoming.as_object_mut()) {
            a.remove("synced_at");
            b.remove("synced_at");
        }
        existing == incoming
    });
    if !unchanged {
        document.insert(EPH_PREFLIGHT_KEY.into(), snapshot.clone());
        if let Err(error) = sqlx::query(
            "UPDATE core_clusterdeployment SET preflight_result = ?, updated_at = CURRENT_TIMESTAMP WHERE CAST(id AS TEXT) = ?",
        )
        .bind(Value::Object(document.clone()).to_string())
        .bind(&deployment_id)
        .execute(pool)
        .await
        {
            return fail(StatusCode::INTERNAL_SERVER_ERROR, &error.to_string(), "DATABASE_ERROR");
        }
    }
    (
        StatusCode::OK,
        Json(json!({
            "eph_preflight": snapshot,
            "preflight_result": Value::Object(document),
        })),
    )
        .into_response()
}
