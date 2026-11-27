#![allow(clippy::result_large_err)]

use axum::extract::{Path, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::{json, Map, Value};
use sqlx::{AnyPool, Row};

use crate::proxy::AppState;

const MAX_HOSTS: usize = 256;
const MAX_NICS: usize = 64;

fn error(status: StatusCode, message: &str, code: &str) -> Response {
    (
        status,
        Json(json!({"error": {"code": code, "message": message, "details": {}}})),
    )
        .into_response()
}

fn text(value: Option<&Value>) -> String {
    value
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
}

fn mapping(value: Option<&Value>) -> Map<String, Value> {
    value
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

fn bmc_host(address: &str) -> String {
    let mut value = address.trim().to_lowercase();
    if value.is_empty() {
        return String::new();
    }
    value = value.split("://").last().unwrap_or(&value).to_string();
    let host = value.split('/').next().unwrap_or("").to_string();
    let host = host.rsplit('@').next().unwrap_or("").to_string();
    let host = host.split(':').next().unwrap_or("").to_string();
    host.trim_matches(|c| c == '[' || c == ']').to_string()
}

fn parse_hosts(document: &Value) -> Result<Vec<Value>, String> {
    let items = match document {
        Value::Array(items) => items.clone(),
        Value::Object(map) => map
            .get("items")
            .and_then(Value::as_array)
            .cloned()
            .ok_or("BMH_DOCUMENT_INVALID")?,
        Value::String(raw) => {
            let parsed: Value = serde_json::from_str(raw).map_err(|_| "BMH_DOCUMENT_INVALID")?;
            return parse_hosts(&parsed);
        }
        _ => return Err("BMH_DOCUMENT_INVALID".into()),
    };
    if items.len() > MAX_HOSTS {
        return Err("BMH_HOST_COUNT_EXCEEDED".into());
    }
    let mut hosts = Vec::with_capacity(items.len());
    let mut seen = std::collections::HashSet::new();
    for item in &items {
        let Value::Object(item) = item else {
            return Err("BMH_ITEM_INVALID".into());
        };
        let metadata = mapping(item.get("metadata"));
        let spec = mapping(item.get("spec"));
        let status = mapping(item.get("status"));
        let hardware = mapping(status.get("hardware"));
        let inventory = mapping(hardware.get("inventory"));
        let name = text(metadata.get("name")).trim().to_string();
        if name.is_empty() {
            return Err("BMH_NAME_REQUIRED".into());
        }
        let namespace = text(metadata.get("namespace")).trim().to_string();
        if !seen.insert(format!("{namespace}/{name}")) {
            return Err("BMH_HOST_DUPLICATE".into());
        }
        let raw_bmc = text(mapping(spec.get("bmc")).get("address"))
            .trim()
            .to_string();
        let address = if raw_bmc.is_empty() || raw_bmc.contains("://") {
            raw_bmc.clone()
        } else {
            format!("https://{raw_bmc}")
        };
        let nic_entries = hardware
            .get("nics")
            .and_then(Value::as_array)
            .filter(|rows| !rows.is_empty())
            .or_else(|| inventory.get("nics").and_then(Value::as_array));
        let mut nics = Vec::new();
        for row in nic_entries.into_iter().flatten().take(MAX_NICS) {
            let Value::Object(row) = row else {
                return Err("BMH_NIC_INVALID".into());
            };
            let nic_name = text(row.get("name")).trim().to_string();
            if nic_name.is_empty() {
                continue;
            }
            let mut nic = Map::new();
            nic.insert("name".into(), json!(nic_name));
            for field in ["mac", "model", "vendor"] {
                let value = text(row.get(field)).trim().to_string();
                if !value.is_empty() {
                    nic.insert(field.into(), json!(value));
                }
            }
            if let Some(speed) = row.get("speedGbps").and_then(Value::as_f64) {
                nic.insert("speed_gbps".into(), json!(speed));
            }
            if let Some(pxe) = row.get("pxe").and_then(Value::as_bool) {
                nic.insert("pxe".into(), json!(pxe));
            }
            nic.insert(
                "pci_address".into(),
                json!(text(row.get("pciAddress")).trim()),
            );
            nics.push(Value::Object(nic));
        }
        let pci_addresses: Vec<String> = nics
            .iter()
            .filter_map(|nic| nic.get("pci_address").and_then(Value::as_str))
            .filter(|value| !value.is_empty())
            .map(str::to_string)
            .collect::<std::collections::BTreeSet<_>>()
            .into_iter()
            .collect();
        let mut host = json!({
            "bmh_name": name,
            "namespace": namespace,
            "bmc_address": address,
            "bmc_host": bmc_host(&address),
            "boot_mac": text(spec.get("bootMACAddress")).trim().to_lowercase(),
            "system_uuid": if !text(hardware.get("systemUUID")).trim().is_empty() { text(hardware.get("systemUUID")).trim().to_string() } else { text(spec.get("uuid")).trim().to_string() },
            "serial_number": text(hardware.get("systemSerialNumber")).trim(),
            "manufacturer": if !text(hardware.get("manufacturer")).trim().is_empty() { text(hardware.get("manufacturer")).trim().to_string() } else { text(hardware.get("systemVendor")).trim().to_string() },
            "product_name": text(hardware.get("productName")).trim(),
            "power_state": text(hardware.get("powerState")).trim(),
            "operational_status": text(status.get("operationalStatus")).trim(),
            "error_type": text(status.get("errorType")).trim(),
            "nics": nics,
            "pci_addresses": pci_addresses,
        });
        let provisioning = mapping(status.get("provisioning"));
        host["provisioning_state"] = json!(text(provisioning.get("state")).trim());
        if let Some(memory) = hardware.get("ramMib").and_then(Value::as_i64) {
            host["memory_mib"] = json!(memory);
        }
        if let Some(arch) = hardware
            .get("cpu")
            .and_then(Value::as_object)
            .map(|cpu| text(cpu.get("arch")).trim().to_string())
            .filter(|value| !value.is_empty())
        {
            host["cpu_arch"] = json!(arch);
        }
        hosts.push(host);
    }
    Ok(hosts)
}

struct Machine {
    id: String,
    bmc_endpoint: String,
    bmh_name: String,
    system_uuid: String,
    vendor: String,
    product_name: String,
    inventory: Value,
}

pub async fn bmh_inventory(
    State(state): State<AppState>,
    Path((tenant_id, project_id)): Path<(String, String)>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Response {
    let user = match crate::billing_api::require_user(&state, &headers).await {
        Ok(user) => user,
        Err(response) => return response,
    };
    let Some(pool) = state.identity_pool.as_ref() else {
        return error(
            StatusCode::SERVICE_UNAVAILABLE,
            "Database unavailable.",
            "DATABASE_UNAVAILABLE",
        );
    };
    let is_admin = crate::billing_api::tenant_admin(pool, &tenant_id, user.id)
        .await
        .unwrap_or(false)
        || crate::tasks::project_editor(pool, &project_id, user.id)
            .await
            .unwrap_or(false);
    if !is_admin {
        return error(
            StatusCode::FORBIDDEN,
            "Tenant or project administrator role is required.",
            "PERMISSION_DENIED",
        );
    }
    let document = body.get("document").cloned().or_else(|| {
        body.get("items")
            .cloned()
            .map(|items| json!({"items": items}))
    });
    let Some(document) = document else {
        return error(
            StatusCode::BAD_REQUEST,
            "document is required.",
            "BMH_DOCUMENT_INVALID",
        );
    };
    let hosts = match parse_hosts(&document) {
        Ok(hosts) => hosts,
        Err(code) => return error(StatusCode::BAD_REQUEST, &format!("bmh: {code}"), &code),
    };
    let machines = match load_machines(pool, &tenant_id, &project_id).await {
        Ok(machines) => machines,
        Err(code) => return error(StatusCode::INTERNAL_SERVER_ERROR, &code, "DATABASE_ERROR"),
    };
    let mut updated = Vec::new();
    let mut unmatched = Vec::new();
    for host in &hosts {
        let host_key = text(host.get("bmc_host"));
        let machine = machines
            .iter()
            .find(|m| !host_key.is_empty() && bmc_host(&m.bmc_endpoint) == host_key)
            .or_else(|| {
                machines
                    .iter()
                    .find(|m| !m.bmh_name.is_empty() && m.bmh_name == text(host.get("bmh_name")))
            })
            .or_else(|| {
                let uuid = text(host.get("system_uuid"));
                if uuid.is_empty() {
                    None
                } else {
                    machines.iter().find(|m| m.system_uuid == uuid)
                }
            });
        let Some(machine) = machine else {
            unmatched.push(text(host.get("bmh_name")));
            continue;
        };
        match merge_host(pool, machine, host).await {
            Ok(adopted) => updated.push(json!({
                "machine_id": machine.id,
                "name": text(host.get("bmh_name")),
                "pci_addresses": host.get("pci_addresses").cloned().unwrap_or_else(|| json!([])),
                "pci_inventory_adopted": adopted,
            })),
            Err(code) => return error(StatusCode::INTERNAL_SERVER_ERROR, &code, "DATABASE_ERROR"),
        }
    }
    let mut warnings = Vec::new();
    if hosts.is_empty() {
        warnings.push("BMH_NO_HOSTS");
    }
    if hosts.iter().any(|h| {
        h.get("nics")
            .and_then(Value::as_array)
            .is_some_and(|n| !n.is_empty())
    }) && !hosts.iter().any(|h| {
        h.get("pci_addresses")
            .and_then(Value::as_array)
            .is_some_and(|p| !p.is_empty())
    }) {
        warnings.push("BMH_PCI_ADDRESS_UNAVAILABLE");
    }
    if hosts.iter().any(|h| text(h.get("boot_mac")).is_empty()) {
        warnings.push("BMH_BOOT_MAC_UNAVAILABLE");
    }
    (
        StatusCode::OK,
        Json(json!({"host_count": hosts.len(), "matched": updated.len(), "unmatched": unmatched, "warnings": warnings, "updated": updated})),
    ).into_response()
}

async fn load_machines(
    pool: &AnyPool,
    tenant_id: &str,
    project_id: &str,
) -> Result<Vec<Machine>, String> {
    let rows = sqlx::query(
        "SELECT CAST(id AS TEXT) AS id, COALESCE(bmc_endpoint, '') AS bmc_endpoint, COALESCE(bmh_name, '') AS bmh_name, \
         COALESCE(system_uuid, '') AS system_uuid, COALESCE(vendor, '') AS vendor, COALESCE(product_name, '') AS product_name, \
         hardware_inventory FROM core_agentmachine WHERE CAST(tenant_id AS TEXT) = ? AND CAST(project_id AS TEXT) = ?",
    )
    .bind(tenant_id)
    .bind(project_id)
    .fetch_all(pool)
    .await
    .map_err(|error| error.to_string())?;
    Ok(rows
        .into_iter()
        .map(|row| Machine {
            id: row.try_get::<String, _>("id").unwrap_or_default(),
            bmc_endpoint: row.try_get::<String, _>("bmc_endpoint").unwrap_or_default(),
            bmh_name: row.try_get::<String, _>("bmh_name").unwrap_or_default(),
            system_uuid: row.try_get::<String, _>("system_uuid").unwrap_or_default(),
            vendor: row.try_get::<String, _>("vendor").unwrap_or_default(),
            product_name: row.try_get::<String, _>("product_name").unwrap_or_default(),
            inventory: row
                .try_get::<Option<String>, _>("hardware_inventory")
                .ok()
                .flatten()
                .and_then(|raw| serde_json::from_str(&raw).ok())
                .unwrap_or_else(|| json!({})),
        })
        .collect())
}

async fn merge_host(pool: &AnyPool, machine: &Machine, host: &Value) -> Result<bool, String> {
    let mut inventory = machine.inventory.as_object().cloned().unwrap_or_default();
    let mut bmh = host.as_object().cloned().unwrap_or_default();
    bmh.insert(
        "inspected_at".into(),
        json!(chrono::Utc::now()
            .format("%Y-%m-%dT%H:%M:%S%.6f+00:00")
            .to_string()),
    );
    inventory.insert("bmh".into(), Value::Object(bmh));
    let existing_nics = inventory
        .get("nics")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let has_pci = existing_nics.iter().any(|nic| {
        nic.get("pci_address")
            .and_then(Value::as_str)
            .is_some_and(|v| !v.is_empty())
    });
    let bmh_owned = inventory.get("nics_source").and_then(Value::as_str) == Some("bmh_inspect");
    let pci_addresses = host
        .get("pci_addresses")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let adopt = !pci_addresses.is_empty() && (!has_pci || bmh_owned);
    let mut adopted_pci = false;
    let mut sets = vec!["hardware_inventory = ?".to_string()];
    if adopt {
        let nics: Vec<Value> = host.get("nics").and_then(Value::as_array).into_iter().flatten().filter_map(|nic| {
            let name = text(nic.get("name"));
            let pci = text(nic.get("pci_address"));
            if name.is_empty() || pci.is_empty() { return None; }
            Some(json!({"name": name, "pci_address": pci, "mac": text(nic.get("mac")), "sriov_capable": true}))
        }).collect();
        if nics != existing_nics {
            inventory.insert("nics".into(), json!(nics));
            inventory.insert("nics_source".into(), json!("bmh_inspect"));
            adopted_pci = true;
            sets.push("inventory_source = 'bmh_inspect'".into());
            sets.push("inventory_revision = COALESCE(inventory_revision, 0) + 1".into());
            sets.push("observed_at = CURRENT_TIMESTAMP".into());
        }
    }
    let mut params: Vec<String> = vec![Value::Object(inventory).to_string()];
    if machine.bmh_name.is_empty() && !text(host.get("bmh_name")).is_empty() {
        sets.push("bmh_name = ?".into());
        params.push(text(host.get("bmh_name")));
    }
    if machine.system_uuid.is_empty() && !text(host.get("system_uuid")).is_empty() {
        sets.push("system_uuid = ?".into());
        params.push(text(host.get("system_uuid")));
    }
    if machine.vendor.is_empty() && !text(host.get("manufacturer")).is_empty() {
        sets.push("vendor = ?".into());
        params.push(text(host.get("manufacturer")));
    }
    if machine.product_name.is_empty() && !text(host.get("product_name")).is_empty() {
        sets.push("product_name = ?".into());
        params.push(text(host.get("product_name")));
    }
    sets.push("updated_at = CURRENT_TIMESTAMP".into());
    let sql = format!(
        "UPDATE core_agentmachine SET {} WHERE CAST(id AS TEXT) = ?",
        sets.join(", ")
    );
    let mut query = sqlx::query(&sql);
    for value in params {
        query = query.bind(value);
    }
    query
        .bind(&machine.id)
        .execute(pool)
        .await
        .map_err(|error| error.to_string())?;
    Ok(adopted_pci)
}
