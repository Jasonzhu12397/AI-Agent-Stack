//! Connector catalogue bootstrap.
//!
//! The catalogue is data: each entry names the environment variable holding
//! its HTTPS origin, and an entry without an endpoint is registered but
//! disabled so the console can show which variable is missing. Tenant
//! onboarding and the operator commands share this one implementation, so a
//! new workspace sees exactly the same connector registry as an old one.

use std::path::PathBuf;

use serde_json::{json, Value};
use sqlx::{AnyPool, Row};

use crate::proxy::AppState;

const DEFAULT_CATALOG: &str = "deploy/agent-plugins/catalog.json";

fn catalog_file(configured: Option<String>) -> Result<PathBuf, String> {
    if let Some(value) = configured.filter(|value| !value.trim().is_empty()) {
        return Ok(PathBuf::from(value));
    }
    if let Ok(value) = std::env::var("PLUGIN_CATALOG_PATH") {
        if !value.trim().is_empty() {
            return Ok(PathBuf::from(value));
        }
    }
    let base = std::env::current_dir().map_err(|error| error.to_string())?;
    let mut candidate = base.clone();
    loop {
        let path = candidate.join(DEFAULT_CATALOG);
        if path.exists() {
            return Ok(path);
        }
        if !candidate.pop() {
            return Ok(base.join(DEFAULT_CATALOG));
        }
    }
}

pub fn load_catalog_entries(configured: Option<String>) -> Result<Vec<Value>, String> {
    let path = catalog_file(configured)?;
    let text = std::fs::read_to_string(&path)
        .map_err(|_| format!("catalog file not found: {}", path.display()))?;
    let parsed: Value = serde_json::from_str(&text).map_err(|error| error.to_string())?;
    Ok(parsed
        .get("plugins")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default())
}

fn capability(entry: &Value) -> Value {
    let name = entry
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let description = entry
        .get("description")
        .and_then(Value::as_str)
        .unwrap_or(&name)
        .to_string();
    if entry.get("input_schema").map(Value::is_object) == Some(true) {
        return json!({
            "name": name,
            "description": description,
            "input_schema": entry["input_schema"],
            "output_schema": entry.get("output_schema").cloned().unwrap_or_else(|| json!({
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": true,
            })),
            "query_params": entry.get("query_params").cloned().unwrap_or_else(|| json!({})),
            "risk_level": entry.get("risk_level").cloned().unwrap_or_else(|| json!("low")),
            "timeout_seconds": entry.get("timeout_seconds").cloned().unwrap_or_else(|| json!(30)),
        });
    }
    json!({
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": false,
        },
        "output_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": false,
        },
        "risk_level": "low",
        "timeout_seconds": 30,
    })
}

fn catalog_metadata(entry: &Value, availability: &str) -> Value {
    json!({
        "kind": entry.get("kind").cloned().unwrap_or_else(|| json!("connector")),
        "category": entry.get("category").cloned().unwrap_or_else(|| json!("")),
        "vendor": entry.get("vendor").cloned().unwrap_or_else(|| json!("")),
        "availability": availability,
        "usage_count": entry.get("usage_count").cloned().unwrap_or_else(|| json!(0)),
        "featured": entry.get("featured").cloned().unwrap_or(json!(false)),
    })
}

fn manifest(endpoint: &str, capabilities: &[Value]) -> Value {
    let mut operations = serde_json::Map::new();
    for item in capabilities {
        let name = item
            .get("name")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        operations.insert(
            name.clone(),
            json!({
                "method": "GET",
                "path": format!("/v1/{name}"),
                "query_params": item.get("query_params").cloned().unwrap_or_else(|| json!({})),
            }),
        );
    }
    json!({
        "http": {"base_url": endpoint, "operations": operations},
        "capabilities": capabilities,
    })
}

async fn postgres(state: &AppState) -> bool {
    state
        .config
        .database_url
        .clone()
        .unwrap_or_default()
        .starts_with("postgres")
}

async fn now_literal(state: &AppState) -> String {
    if postgres(state).await {
        "NOW()".into()
    } else {
        format!("'{}'", chrono::Utc::now().format("%Y-%m-%d %H:%M:%S"))
    }
}

async fn tenant_owner(pool: &AnyPool, tenant_id: &str) -> Option<i64> {
    sqlx::query_scalar::<_, i64>(
        "SELECT user_id FROM core_tenantmembership WHERE CAST(tenant_id AS TEXT) = ? \
         ORDER BY created_at ASC, id ASC LIMIT 1",
    )
    .bind(tenant_id)
    .fetch_optional(pool)
    .await
    .ok()
    .flatten()
}

/// Upsert every catalogue entry for one tenant: `(registered, enabled)`.
pub async fn sync_tenant_catalog(
    state: &AppState,
    catalog: Option<String>,
    tenant_slug: Option<String>,
) -> Result<(usize, usize), String> {
    let pool = state
        .identity_pool
        .as_ref()
        .ok_or_else(|| "database unavailable".to_string())?;
    let entries = load_catalog_entries(catalog)?;
    let mut tenants: Vec<String> = Vec::new();
    let rows =
        sqlx::query("SELECT CAST(id AS TEXT) AS id, slug FROM core_tenant ORDER BY created_at ASC")
            .fetch_all(pool)
            .await
            .map_err(|error| error.to_string())?;
    for row in rows {
        let slug: String = row.try_get("slug").unwrap_or_default();
        if tenant_slug.as_deref().map(|wanted| wanted != slug) == Some(true) {
            continue;
        }
        tenants.push(row.try_get::<String, _>("id").unwrap_or_default());
    }
    let now = now_literal(state).await;
    let mut created = 0usize;
    let mut enabled = 0usize;
    for tenant_id in tenants {
        let Some(owner) = tenant_owner(pool, &tenant_id).await else {
            continue;
        };
        for entry in &entries {
            let builtin = entry.get("builtin").and_then(Value::as_bool) == Some(true);
            let endpoint = entry
                .get("endpoint_env")
                .and_then(Value::as_str)
                .map(|name| std::env::var(name).unwrap_or_default())
                .unwrap_or_default()
                .trim()
                .to_string();
            let capabilities: Vec<Value> = entry
                .get("capabilities")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default()
                .iter()
                .map(capability)
                .collect();
            let plugin_key = entry
                .get("plugin_key")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            let name = entry
                .get("name")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            let base_description = entry
                .get("description")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            let description = if builtin || !endpoint.is_empty() {
                base_description
            } else {
                format!(
                    "{base_description} Configure {} to enable it.",
                    entry
                        .get("endpoint_env")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                )
            };
            let version = entry
                .get("version")
                .and_then(Value::as_str)
                .unwrap_or("1.0.0")
                .to_string();
            let protocol = entry
                .get("protocol")
                .and_then(Value::as_str)
                .unwrap_or("http_json")
                .to_string();
            let auth_required = entry
                .get("auth_required")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            let availability = if builtin {
                "builtin"
            } else if !endpoint.is_empty() {
                "ready"
            } else {
                "unprovisioned"
            };
            let mut manifest_value = catalog_metadata(entry, availability);
            if !endpoint.is_empty() && !builtin {
                let http_manifest = manifest(&endpoint, &capabilities);
                if let (Some(target), Some(source)) =
                    (manifest_value.as_object_mut(), http_manifest.as_object())
                {
                    for (key, value) in source {
                        target.insert(key.clone(), value.clone());
                    }
                    target.insert("catalog".into(), catalog_metadata(entry, availability));
                }
            }
            let manifest_text =
                serde_json::to_string(&manifest_value).unwrap_or_else(|_| "{}".into());
            let capabilities_text =
                serde_json::to_string(&capabilities).unwrap_or_else(|_| "[]".into());
            let enabled_flag = builtin || !endpoint.is_empty();
            let existing = sqlx::query(
                "SELECT CAST(id AS TEXT) AS id FROM core_agentplugindefinition \
                 WHERE CAST(tenant_id AS TEXT) = ? AND plugin_key = ? ORDER BY created_at DESC LIMIT 1",
            )
            .bind(&tenant_id)
            .bind(&plugin_key)
            .fetch_optional(pool)
            .await
            .map_err(|error| error.to_string())?;
            match existing {
                Some(row) => {
                    let id: String = row.try_get("id").unwrap_or_default();
                    let sql = format!(
                        "UPDATE core_agentplugindefinition SET created_by_id = ?, name = ?, \
                         description = ?, version = ?, protocol = ?, endpoint_url = ?, \
                         auth_required = ?, capabilities = ?, manifest = ?, enabled = ?, \
                         updated_at = {now} WHERE CAST(id AS TEXT) = ?"
                    );
                    sqlx::query(&sql)
                        .bind(owner)
                        .bind(&name)
                        .bind(&description)
                        .bind(&version)
                        .bind(&protocol)
                        .bind(&endpoint)
                        .bind(if auth_required { 1 } else { 0 })
                        .bind(&capabilities_text)
                        .bind(&manifest_text)
                        .bind(if enabled_flag { 1 } else { 0 })
                        .bind(&id)
                        .execute(pool)
                        .await
                        .map_err(|error| error.to_string())?;
                }
                None => {
                    let id = uuid::Uuid::new_v4().simple().to_string();
                    let sql = format!(
                        "INSERT INTO core_agentplugindefinition (id, tenant_id, created_by_id, \
                         plugin_key, name, description, version, protocol, endpoint_url, \
                         auth_required, credential_ref, capabilities, manifest, input_schema, \
                         output_schema, timeout_seconds, max_concurrency, enabled, created_at, \
                         updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, 30, 1, ?, \
                         {now}, {now})"
                    );
                    sqlx::query(&sql)
                        .bind(&id)
                        .bind(&tenant_id)
                        .bind(owner)
                        .bind(&plugin_key)
                        .bind(&name)
                        .bind(&description)
                        .bind(&version)
                        .bind(&protocol)
                        .bind(&endpoint)
                        .bind(if auth_required { 1 } else { 0 })
                        .bind(&capabilities_text)
                        .bind(&manifest_text)
                        .bind("{\"type\":\"object\",\"properties\":{},\"required\":[],\"additionalProperties\":true}")
                        .bind("{\"type\":\"object\",\"properties\":{},\"required\":[],\"additionalProperties\":true}")
                        .bind(if enabled_flag { 1 } else { 0 })
                        .execute(pool)
                        .await
                        .map_err(|error| error.to_string())?;
                }
            }
            created += 1;
            if enabled_flag {
                enabled += 1;
            }
        }
    }
    Ok((created, enabled))
}

/// Install enabled built-in connectors into every project of each tenant.
pub async fn install_builtin_connectors(
    state: &AppState,
    tenant_slug: Option<String>,
) -> Result<usize, String> {
    let pool = state
        .identity_pool
        .as_ref()
        .ok_or_else(|| "database unavailable".to_string())?;
    let mut tenants: Vec<String> = Vec::new();
    let rows =
        sqlx::query("SELECT CAST(id AS TEXT) AS id, slug FROM core_tenant ORDER BY created_at ASC")
            .fetch_all(pool)
            .await
            .map_err(|error| error.to_string())?;
    for row in rows {
        let slug: String = row.try_get("slug").unwrap_or_default();
        if tenant_slug.as_deref().map(|wanted| wanted != slug) == Some(true) {
            continue;
        }
        tenants.push(row.try_get::<String, _>("id").unwrap_or_default());
    }
    let now = now_literal(state).await;
    let mut installed = 0usize;
    for tenant_id in tenants {
        let Some(owner) = tenant_owner(pool, &tenant_id).await else {
            continue;
        };
        let definitions = sqlx::query(
            "SELECT CAST(plugin.id AS TEXT) AS id, plugin.version, \
             CAST(plugin.capabilities AS TEXT) AS capabilities_text \
             FROM core_agentplugindefinition plugin WHERE CAST(plugin.tenant_id AS TEXT) = ? \
             AND plugin.enabled = 1 AND plugin.protocol = 'open_data'",
        )
        .bind(&tenant_id)
        .fetch_all(pool)
        .await
        .map_err(|error| error.to_string())?;
        let projects = sqlx::query(
            "SELECT CAST(id AS TEXT) AS id FROM core_project WHERE CAST(tenant_id AS TEXT) = ?",
        )
        .bind(&tenant_id)
        .fetch_all(pool)
        .await
        .map_err(|error| error.to_string())?;
        for project in projects {
            let project_id: String = project.try_get("id").unwrap_or_default();
            for definition in &definitions {
                let plugin_id: String = definition.try_get("id").unwrap_or_default();
                let version: String = definition.try_get("version").unwrap_or_default();
                let capabilities_text: Option<String> =
                    definition.try_get("capabilities_text").ok().flatten();
                let capabilities: Value = capabilities_text
                    .as_deref()
                    .and_then(|text| serde_json::from_str(text).ok())
                    .unwrap_or_else(|| json!([]));
                let granted: Vec<String> = capabilities
                    .as_array()
                    .cloned()
                    .unwrap_or_default()
                    .iter()
                    .filter_map(|item| item.get("name").and_then(Value::as_str).map(str::to_string))
                    .collect();
                let existing = sqlx::query(
                    "SELECT CAST(id AS TEXT) AS id, status FROM core_agentplugininstallation \
                     WHERE CAST(project_id AS TEXT) = ? AND CAST(plugin_id AS TEXT) = ? LIMIT 1",
                )
                .bind(&project_id)
                .bind(&plugin_id)
                .fetch_optional(pool)
                .await
                .map_err(|error| error.to_string())?;
                match existing {
                    Some(row) => {
                        let id: String = row.try_get("id").unwrap_or_default();
                        let status: String = row.try_get("status").unwrap_or_default();
                        if status != "installed" {
                            let sql = format!(
                                "UPDATE core_agentplugininstallation SET status = 'installed', \
                                 updated_at = {now} WHERE CAST(id AS TEXT) = ?"
                            );
                            sqlx::query(&sql)
                                .bind(&id)
                                .execute(pool)
                                .await
                                .map_err(|error| error.to_string())?;
                        }
                    }
                    None => {
                        let id = uuid::Uuid::new_v4().simple().to_string();
                        let sql = format!(
                            "INSERT INTO core_agentplugininstallation (id, tenant_id, project_id, \
                             plugin_id, plugin_version, status, granted_capabilities, credential_ref, \
                             installed_by_id, revision, created_at, updated_at) VALUES (?, ?, ?, ?, ?, \
                             'installed', ?, '', ?, 0, {now}, {now})"
                        );
                        sqlx::query(&sql)
                            .bind(&id)
                            .bind(&tenant_id)
                            .bind(&project_id)
                            .bind(&plugin_id)
                            .bind(&version)
                            .bind(serde_json::to_string(&granted).unwrap_or_else(|_| "[]".into()))
                            .bind(owner)
                            .execute(pool)
                            .await
                            .map_err(|error| error.to_string())?;
                    }
                }
                installed += 1;
            }
        }
    }
    Ok(installed)
}
