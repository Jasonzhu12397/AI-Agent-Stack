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

const BMH_INSPECT_ACTION: &str = "metal3.host.inspect";
const BMH_INVENTORY_KEY: &str = "bmh_inventory";

fn bmh_record(payload: &Value) -> Value {
    let output = payload.get("output").and_then(Value::as_object);
    json!({
        "available": payload.get("available").and_then(Value::as_bool).unwrap_or(false),
        "reason": payload.get("reason").cloned().unwrap_or(Value::Null),
        "status": payload.get("status").cloned().unwrap_or(Value::Null),
        "error_code": payload.get("error_code").cloned().unwrap_or(Value::Null),
        "command_id": payload.get("command_id").cloned().unwrap_or(Value::Null),
        "host_count": 0,
        "matched": 0,
        "unmatched": [],
        "warnings": [],
        "observed_at": output.and_then(|out| out.get("observed_at")).cloned().unwrap_or(Value::Null),
        "synced_at": Utc::now().format("%Y-%m-%dT%H:%M:%S%.6f+00:00").to_string(),
    })
}

async fn store_snapshot(
    pool: &AnyPool,
    deployment_id: &str,
    key: &str,
    snapshot: &Value,
) -> Result<Value, String> {
    let stored: Option<String> = sqlx::query_scalar(
        "SELECT preflight_result FROM core_clusterdeployment WHERE CAST(id AS TEXT) = ? LIMIT 1",
    )
    .bind(deployment_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())?;
    let mut document = stored
        .as_deref()
        .and_then(|raw| serde_json::from_str::<Value>(raw).ok())
        .and_then(|value| value.as_object().cloned())
        .unwrap_or_default();
    let unchanged = document.get(key).is_some_and(|existing| {
        let mut existing = existing.clone();
        let mut incoming = snapshot.clone();
        if let (Some(a), Some(b)) = (existing.as_object_mut(), incoming.as_object_mut()) {
            a.remove("synced_at");
            b.remove("synced_at");
        }
        existing == incoming
    });
    if !unchanged {
        document.insert(key.into(), snapshot.clone());
        sqlx::query(
            "UPDATE core_clusterdeployment SET preflight_result = ?, updated_at = CURRENT_TIMESTAMP WHERE CAST(id AS TEXT) = ?",
        )
        .bind(Value::Object(document.clone()).to_string())
        .bind(deployment_id)
        .execute(pool)
        .await
        .map_err(|error| error.to_string())?;
    }
    Ok(Value::Object(document))
}

pub async fn sync_bmh_inventory(
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
    let (tenant, project) = match permission(pool, &deployment_id, user.id).await {
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
    let payload = match fetch_action(&deployment_id, BMH_INSPECT_ACTION).await {
        Ok(payload) => payload,
        Err(code) if code == "CLOUD_CONTROL_NOT_CONFIGURED" => {
            return fail(
                StatusCode::SERVICE_UNAVAILABLE,
                "Cloud Control is not configured.",
                "BMH_INVENTORY_UNAVAILABLE",
            );
        }
        Err(_) => {
            return fail(
                StatusCode::BAD_GATEWAY,
                "Cloud Control request failed.",
                "BMH_INVENTORY_UNAVAILABLE",
            )
        }
    };
    let mut record = bmh_record(&payload);
    if record["available"] != json!(true) {
        let document = store_snapshot(pool, &deployment_id, BMH_INVENTORY_KEY, &record)
            .await
            .unwrap_or(json!({}));
        return (StatusCode::OK, Json(json!({"bmh_inventory": record, "updated": Value::Null, "preflight_result": document}))).into_response();
    }
    if record["status"] != json!("succeeded") {
        record["reason"] = json!("BMH_INSPECT_NOT_SUCCEEDED");
        let _ = store_snapshot(pool, &deployment_id, BMH_INVENTORY_KEY, &record).await;
        return fail(
            StatusCode::CONFLICT,
            "The EPH inspection did not succeed.",
            "bmh_inspect_not_succeeded",
        );
    }
    let output = payload.get("output").cloned().unwrap_or_else(|| json!({}));
    let hosts = match crate::fleet_api::normalize_hosts(output.get("hosts").unwrap_or(&Value::Null))
    {
        Ok(hosts) => hosts,
        Err(code) => {
            record["reason"] = json!(code);
            let _ = store_snapshot(pool, &deployment_id, BMH_INVENTORY_KEY, &record).await;
            return fail(StatusCode::BAD_REQUEST, &format!("bmh: {code}"), &code);
        }
    };
    let machines = match crate::fleet_api::load_machines(pool, &tenant, &project).await {
        Ok(machines) => machines,
        Err(code) => return fail(StatusCode::INTERNAL_SERVER_ERROR, &code, "DATABASE_ERROR"),
    };
    let mut updated = Vec::new();
    let mut unmatched = Vec::new();
    for host in &hosts {
        let host_key = host
            .get("bmc_host")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let machine = machines
            .iter()
            .find(|m| {
                !host_key.is_empty() && crate::fleet_api::bmc_host(&m.bmc_endpoint) == host_key
            })
            .or_else(|| {
                machines.iter().find(|m| {
                    !m.bmh_name.is_empty()
                        && m.bmh_name
                            == host
                                .get("bmh_name")
                                .and_then(Value::as_str)
                                .unwrap_or_default()
                })
            })
            .or_else(|| {
                let uuid = host
                    .get("system_uuid")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                if uuid.is_empty() {
                    None
                } else {
                    machines.iter().find(|m| m.system_uuid == uuid)
                }
            });
        let Some(machine) = machine else {
            unmatched.push(host.get("bmh_name").cloned().unwrap_or(Value::Null));
            continue;
        };
        match crate::fleet_api::merge_host(pool, machine, host).await {
            Ok(adopted) => updated.push(json!({
                "machine_id": machine.id,
                "name": host.get("bmh_name").cloned().unwrap_or(Value::Null),
                "pci_addresses": host.get("pci_addresses").cloned().unwrap_or_else(|| json!([])),
                "pci_inventory_adopted": adopted,
            })),
            Err(code) => return fail(StatusCode::INTERNAL_SERVER_ERROR, &code, "DATABASE_ERROR"),
        }
    }
    unmatched.truncate(64);
    record["host_count"] = json!(hosts.len());
    record["matched"] = json!(updated.len());
    record["unmatched"] = json!(unmatched);
    record["warnings"] = json!(bounded_warnings(output.get("warnings")));
    match store_snapshot(pool, &deployment_id, BMH_INVENTORY_KEY, &record).await {
        Ok(document) => (
            StatusCode::OK,
            Json(
                json!({"bmh_inventory": record, "updated": updated, "preflight_result": document}),
            ),
        )
            .into_response(),
        Err(code) => fail(StatusCode::INTERNAL_SERVER_ERROR, &code, "DATABASE_ERROR"),
    }
}
