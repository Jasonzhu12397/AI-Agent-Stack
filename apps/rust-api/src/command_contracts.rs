//! Payload contracts for Day-2 cluster commands.
//!
//! The platform rejects a malformed mutation before it is queued, and the
//! Target Agent re-checks the same rules before it executes. Keeping the
//! contract here means the API and the Agent cannot disagree.

use serde_json::{json, Map, Value};

#[derive(Debug)]
pub struct ContractError {
    pub code: &'static str,
    pub detail: String,
}

fn fail(code: &'static str, detail: impl Into<String>) -> ContractError {
    ContractError {
        code,
        detail: detail.into(),
    }
}

fn object(payload: &Value) -> Result<&Map<String, Value>, ContractError> {
    payload
        .as_object()
        .ok_or_else(|| fail("PAYLOAD_INVALID", "payload must be a JSON object"))
}

fn unknown_keys(object: &Map<String, Value>, allowed: &[&str]) -> Result<(), ContractError> {
    let unknown: Vec<&str> = object
        .keys()
        .map(String::as_str)
        .filter(|key| !allowed.contains(key))
        .collect();
    if unknown.is_empty() {
        Ok(())
    } else {
        Err(fail(
            "PAYLOAD_INVALID",
            format!("unknown payload keys: {}", unknown.join(", ")),
        ))
    }
}

fn dns_name(value: Option<&Value>, field: &str, allow_dot: bool) -> Result<String, ContractError> {
    let text = value.and_then(Value::as_str).map(str::trim).unwrap_or("");
    let valid_chars = text.chars().all(|c| {
        c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-' || (allow_dot && c == '.')
    });
    if text.is_empty()
        || text.len() > 63
        || !valid_chars
        || !text
            .chars()
            .next()
            .is_some_and(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
        || !text
            .chars()
            .last()
            .is_some_and(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
    {
        return Err(fail(
            "PAYLOAD_INVALID",
            format!("{field} must be a Kubernetes DNS name."),
        ));
    }
    Ok(text.to_string())
}

fn execution_mode(value: Option<&Value>, required: bool) -> Result<String, ContractError> {
    let mode = value.and_then(Value::as_str).unwrap_or("");
    if mode.is_empty() && !required {
        return Ok("simulation".into());
    }
    if !matches!(mode, "simulation" | "execute") {
        return Err(fail(
            "PAYLOAD_INVALID",
            "execution_mode must be simulation or execute.",
        ));
    }
    Ok(mode.to_string())
}

fn reason(value: Option<&Value>) -> Result<String, ContractError> {
    let text = value.and_then(Value::as_str).unwrap_or("");
    if text.len() > 300 {
        return Err(fail("PAYLOAD_INVALID", "reason is too long."));
    }
    Ok(text.to_string())
}

fn version_shape(value: &str) -> bool {
    let rest = value.strip_prefix('v').unwrap_or(value);
    let parts: Vec<&str> = rest.split('.').collect();
    parts.len() >= 2
        && parts.len() <= 3
        && parts
            .iter()
            .all(|part| !part.is_empty() && part.chars().all(|c| c.is_ascii_digit()))
}

fn minor(version: &str) -> Option<u32> {
    version.split('.').nth(1)?.parse().ok()
}

fn snapshot_directory_problem(directory: &str) -> Option<String> {
    if directory.is_empty() {
        return None;
    }
    if !directory.starts_with('/') {
        return Some("snapshot_directory must be absolute".into());
    }
    if directory.split('/').any(|part| part == "..") {
        return Some("snapshot_directory must not contain ..".into());
    }
    if !directory.starts_with("/var/lib/etcd/") {
        return Some("snapshot_directory must be under /var/lib/etcd".into());
    }
    None
}

fn validate_kubernetes_upgrade(
    payload: &Map<String, Value>,
    cluster_name: &str,
) -> Result<Value, ContractError> {
    unknown_keys(
        payload,
        &[
            "cluster_name",
            "current_version",
            "target_version",
            "execution_mode",
            "snapshot_directory",
        ],
    )?;
    for key in [
        "cluster_name",
        "current_version",
        "target_version",
        "execution_mode",
    ] {
        if !payload.contains_key(key) {
            return Err(fail(
                "PAYLOAD_INVALID",
                format!("payload must contain {key}"),
            ));
        }
    }
    let cluster = dns_name(payload.get("cluster_name"), "cluster_name", true)?;
    if !cluster_name.is_empty() && cluster != cluster_name {
        return Err(fail(
            "CLUSTER_MISMATCH",
            "cluster_name must match the Target Agent's cluster",
        ));
    }
    let current = payload
        .get("current_version")
        .and_then(Value::as_str)
        .unwrap_or("");
    let target = payload
        .get("target_version")
        .and_then(Value::as_str)
        .unwrap_or("");
    if !version_shape(current) {
        return Err(fail(
            "CURRENT_VERSION_INVALID",
            "current_version must look like v1.32.2",
        ));
    }
    if !version_shape(target) {
        return Err(fail(
            "TARGET_VERSION_INVALID",
            "target_version must look like v1.33.0",
        ));
    }
    let target_minor = format!("v1.{}", minor(target).unwrap_or(0));
    if !["v1.33", "v1.34", "v1.35", "v1.36"].contains(&target_minor.as_str()) {
        return Err(fail(
            "VERSION_UNSUPPORTED",
            format!("target minor {target_minor} is not supported"),
        ));
    }
    if minor(target).unwrap_or(0) != minor(current).unwrap_or(0) + 1 {
        return Err(fail(
            "VERSION_STEP_INVALID",
            "only an upgrade to the next minor version is supported",
        ));
    }
    let mode = execution_mode(payload.get("execution_mode"), true)?;
    let directory = payload
        .get("snapshot_directory")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim()
        .to_string();
    if let Some(problem) = snapshot_directory_problem(&directory) {
        return Err(fail("SNAPSHOT_DIRECTORY_INVALID", problem));
    }
    let mut normalized = json!({
        "cluster_name": cluster,
        "current_version": current,
        "target_version": target,
        "execution_mode": mode,
    });
    if !directory.is_empty() {
        normalized["snapshot_directory"] = json!(directory.trim_end_matches('/'));
    }
    Ok(normalized)
}

fn validate_kubernetes_rollback(
    payload: &Map<String, Value>,
    cluster_name: &str,
) -> Result<Value, ContractError> {
    unknown_keys(
        payload,
        &[
            "cluster_name",
            "snapshot_path",
            "execution_mode",
            "confirm_cluster_name",
        ],
    )?;
    let cluster = dns_name(payload.get("cluster_name"), "cluster_name", true)?;
    if !cluster_name.is_empty() && cluster != cluster_name {
        return Err(fail(
            "ROLLBACK_CLUSTER_NAME_MISMATCH",
            format!("this agent hosts {cluster_name}, not {cluster}"),
        ));
    }
    let snapshot = payload
        .get("snapshot_path")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if !snapshot.starts_with('/')
        || !snapshot.ends_with(".db")
        || snapshot.contains("..")
        || !snapshot
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '/' | '-'))
    {
        return Err(fail(
            "ROLLBACK_SNAPSHOT_PATH_INVALID",
            "snapshot_path must be an absolute .db path without traversal",
        ));
    }
    let mode = execution_mode(payload.get("execution_mode"), true)?;
    let confirm = payload
        .get("confirm_cluster_name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if mode == "execute" && confirm != cluster {
        return Err(fail(
            "ROLLBACK_CONFIRMATION_MISMATCH",
            "execute requires confirm_cluster_name to equal cluster_name",
        ));
    }
    Ok(json!({
        "cluster_name": cluster,
        "snapshot_path": snapshot,
        "execution_mode": mode,
        "confirm_cluster_name": confirm,
    }))
}

fn validate_cluster_scale(
    payload: &Map<String, Value>,
    cluster_name: &str,
) -> Result<Value, ContractError> {
    let target = dns_name(
        payload.get("target_cluster_name"),
        "target_cluster_name",
        false,
    )?;
    if !cluster_name.is_empty() && target != cluster_name {
        return Err(fail(
            "CLUSTER_SCALE_CLUSTER_MISMATCH",
            "The requested cluster is not the cluster this Agent is enrolled for.",
        ));
    }
    let machine_deployment = match payload.get("machine_deployment").and_then(Value::as_str) {
        Some(value) if !value.is_empty() => Some(dns_name(
            payload.get("machine_deployment"),
            "machine_deployment",
            false,
        )?),
        _ => None,
    };
    let namespace = if payload
        .get("target_namespace")
        .and_then(Value::as_str)
        .unwrap_or("")
        .is_empty()
    {
        "default".to_string()
    } else {
        dns_name(payload.get("target_namespace"), "target_namespace", false)?
    };
    let desired = payload
        .get("desired_workers")
        .and_then(Value::as_i64)
        .ok_or_else(|| {
            fail(
                "CLUSTER_SCALE_REQUEST_INVALID",
                "desired_workers must be a positive integer.",
            )
        })?;
    if !(1..=2500).contains(&desired) {
        return Err(fail(
            "CLUSTER_SCALE_REQUEST_INVALID",
            "desired_workers must be between 1 and 2500.",
        ));
    }
    let mode = execution_mode(payload.get("execution_mode"), false)?;
    let reason = reason(payload.get("reason"))?;
    Ok(json!({
        "target_cluster_name": target,
        "machine_deployment": machine_deployment,
        "target_namespace": namespace,
        "desired_workers": desired,
        "execution_mode": mode,
        "reason": reason,
    }))
}

fn validate_cluster_repair(
    payload: &Map<String, Value>,
    cluster_name: &str,
) -> Result<Value, ContractError> {
    let target = dns_name(
        payload.get("target_cluster_name"),
        "target_cluster_name",
        false,
    )?;
    if !cluster_name.is_empty() && target != cluster_name {
        return Err(fail(
            "CLUSTER_REPAIR_CLUSTER_NOT_ENROLLED",
            "The request targets a cluster this Agent is not enrolled for.",
        ));
    }
    let namespace = if payload
        .get("target_namespace")
        .and_then(Value::as_str)
        .unwrap_or("")
        .is_empty()
    {
        "default".to_string()
    } else {
        dns_name(payload.get("target_namespace"), "target_namespace", false)?
    };
    let machine_deployment = match payload.get("machine_deployment").and_then(Value::as_str) {
        Some(value) if !value.is_empty() => Some(dns_name(
            payload.get("machine_deployment"),
            "machine_deployment",
            false,
        )?),
        _ => None,
    };
    let mode = execution_mode(payload.get("execution_mode"), true)?;
    let reason = reason(payload.get("reason"))?;
    let machine_names = match payload.get("machine_names") {
        None | Some(Value::Null) => Value::Null,
        Some(Value::Array(items)) => {
            if items.is_empty() {
                return Err(fail(
                    "CLUSTER_REPAIR_REQUEST_INVALID",
                    "machine_names must be a non-empty list.",
                ));
            }
            let mut names = Vec::new();
            for item in items {
                names.push(dns_name(Some(item), "machine_names", false)?);
            }
            json!(names)
        }
        Some(_) => {
            return Err(fail(
                "CLUSTER_REPAIR_REQUEST_INVALID",
                "machine_names must be a non-empty list.",
            ))
        }
    };
    Ok(json!({
        "target_cluster_name": target,
        "target_namespace": namespace,
        "machine_deployment": machine_deployment,
        "execution_mode": mode,
        "reason": reason,
        "machine_names": machine_names,
    }))
}

fn network_version_shape(value: &str) -> bool {
    let trimmed = value.strip_prefix('v').unwrap_or(value);
    let (core, suffix) = match trimmed.split_once('-') {
        Some((core, suffix)) if !suffix.is_empty() => (core, true),
        Some(_) => return false,
        None => (trimmed, true),
    };
    let mut parts = core.split('.');
    let count = parts.clone().count();
    count == 3
        && parts.all(|part| !part.is_empty() && part.chars().all(|c| c.is_ascii_digit()))
        && suffix
}

fn network_common(
    payload: &Map<String, Value>,
    cluster_name: &str,
    mode_required: bool,
) -> Result<(String, String, String, String, String), ContractError> {
    let target = dns_name(
        payload.get("target_cluster_name"),
        "target_cluster_name",
        false,
    )?;
    if !cluster_name.is_empty() && target != cluster_name {
        return Err(fail(
            "NETWORK_UPGRADE_CLUSTER_NOT_ENROLLED",
            "The request targets a cluster this Agent is not enrolled for.",
        ));
    }
    let plugin = payload.get("plugin").and_then(Value::as_str).unwrap_or("");
    if !["calico", "multus", "ovs", "sriov", "dpdk"].contains(&plugin) {
        return Err(fail(
            "NETWORK_UPGRADE_REQUEST_INVALID",
            "plugin must be calico, multus, ovs, sriov or dpdk.",
        ));
    }
    let namespace = if payload
        .get("target_namespace")
        .and_then(Value::as_str)
        .unwrap_or("")
        .is_empty()
    {
        "default".to_string()
    } else {
        dns_name(payload.get("target_namespace"), "target_namespace", false)?
    };
    let reason = reason(payload.get("reason"))?;
    let mode = execution_mode(payload.get("execution_mode"), mode_required)?;
    Ok((target, namespace, plugin.to_string(), mode, reason))
}

fn validate_network_upgrade(
    payload: &Map<String, Value>,
    cluster_name: &str,
) -> Result<Value, ContractError> {
    let (target, namespace, plugin, mode, reason) = network_common(payload, cluster_name, true)?;
    let version = payload
        .get("target_version")
        .and_then(Value::as_str)
        .unwrap_or("");
    let shape_ok = network_version_shape(version);
    if !shape_ok {
        return Err(fail(
            "NETWORK_UPGRADE_REQUEST_INVALID",
            "target_version must look like 1.14.2.",
        ));
    }
    let documents = payload
        .get("documents")
        .and_then(Value::as_str)
        .unwrap_or("");
    if documents.trim().is_empty() || documents.len() > 60000 {
        return Err(fail(
            "NETWORK_UPGRADE_REQUEST_INVALID",
            "documents must contain at least one YAML object and be at most 60000 characters.",
        ));
    }
    Ok(json!({
        "target_cluster_name": target,
        "target_namespace": namespace,
        "plugin": plugin,
        "execution_mode": mode,
        "reason": reason,
        "target_version": version,
        "documents": documents,
    }))
}

fn validate_network_revisions(
    payload: &Map<String, Value>,
    cluster_name: &str,
) -> Result<Value, ContractError> {
    let (target, namespace, plugin, _mode, _reason) = network_common(payload, cluster_name, false)?;
    Ok(json!({
        "target_cluster_name": target,
        "target_namespace": namespace,
        "plugin": plugin,
    }))
}

fn validate_network_rollback(
    payload: &Map<String, Value>,
    cluster_name: &str,
) -> Result<Value, ContractError> {
    let (target, namespace, plugin, mode, reason) = network_common(payload, cluster_name, true)?;
    let revision = payload.get("revision").and_then(Value::as_str);
    if let Some(revision) = revision {
        let shape_ok = revision.len() >= 15
            && revision
                .chars()
                .all(|c| c.is_ascii_digit() || matches!(c, 'T' | 'Z' | '-'));
        if !shape_ok {
            return Err(fail(
                "NETWORK_UPGRADE_REQUEST_INVALID",
                "revision must be a captured id such as 20261126T000000Z.",
            ));
        }
    }
    Ok(json!({
        "target_cluster_name": target,
        "target_namespace": namespace,
        "plugin": plugin,
        "execution_mode": mode,
        "reason": reason,
        "revision": revision,
    }))
}

/// Validate and normalize the payload for a command that carries a contract.
/// Unknown commands pass through unchanged.
pub fn validate(
    command_type: &str,
    payload: &Value,
    cluster_name: &str,
) -> Result<Value, ContractError> {
    let map = object(payload)?;
    match command_type {
        "cluster.kubernetes.version.upgrade" => validate_kubernetes_upgrade(map, cluster_name),
        "cluster.kubernetes.version.rollback" => validate_kubernetes_rollback(map, cluster_name),
        "cluster.scale" => validate_cluster_scale(map, cluster_name),
        "cluster.worker.repair" => validate_cluster_repair(map, cluster_name),
        "target.network.upgrade" => validate_network_upgrade(map, cluster_name),
        "target.network.rollback" => validate_network_rollback(map, cluster_name),
        "target.network.revisions" => validate_network_revisions(map, cluster_name),
        _ => Ok(payload.clone()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cluster_scale_normalizes_and_rejects_mismatch() {
        let payload = json!({
            "target_cluster_name": "prod-a",
            "desired_workers": 3
        });
        let normalized = validate("cluster.scale", &payload, "prod-a").unwrap();
        assert_eq!(normalized["target_namespace"], "default");
        assert_eq!(normalized["execution_mode"], "simulation");
        assert!(normalized["machine_deployment"].is_null());

        let mismatch = validate("cluster.scale", &payload, "prod-b").unwrap_err();
        assert_eq!(mismatch.code, "CLUSTER_SCALE_CLUSTER_MISMATCH");

        let zero = json!({"target_cluster_name": "prod-a", "desired_workers": 0});
        assert_eq!(
            validate("cluster.scale", &zero, "prod-a").unwrap_err().code,
            "CLUSTER_SCALE_REQUEST_INVALID"
        );
    }

    #[test]
    fn kubernetes_upgrade_requires_the_next_minor_and_etcd_snapshot() {
        let valid = json!({
            "cluster_name": "prod-a",
            "current_version": "v1.34.2",
            "target_version": "v1.35.0",
            "execution_mode": "execute",
            "snapshot_directory": "/var/lib/etcd/backups/"
        });
        let normalized = validate("cluster.kubernetes.version.upgrade", &valid, "prod-a").unwrap();
        assert_eq!(normalized["snapshot_directory"], "/var/lib/etcd/backups");

        let skipped = json!({
            "cluster_name": "prod-a",
            "current_version": "v1.34.2",
            "target_version": "v1.36.0",
            "execution_mode": "simulation"
        });
        assert_eq!(
            validate("cluster.kubernetes.version.upgrade", &skipped, "prod-a")
                .unwrap_err()
                .code,
            "VERSION_STEP_INVALID"
        );

        let bad_snapshot = json!({
            "cluster_name": "prod-a",
            "current_version": "v1.34.2",
            "target_version": "v1.35.0",
            "execution_mode": "execute",
            "snapshot_directory": "/tmp/backups"
        });
        assert_eq!(
            validate(
                "cluster.kubernetes.version.upgrade",
                &bad_snapshot,
                "prod-a"
            )
            .unwrap_err()
            .code,
            "SNAPSHOT_DIRECTORY_INVALID"
        );
    }

    #[test]
    fn kubernetes_rollback_requires_confirmation_in_execute_mode() {
        let base = json!({
            "cluster_name": "prod-a",
            "snapshot_path": "/var/lib/etcd/backups/pre.db",
            "execution_mode": "execute"
        });
        assert_eq!(
            validate("cluster.kubernetes.version.rollback", &base, "prod-a")
                .unwrap_err()
                .code,
            "ROLLBACK_CONFIRMATION_MISMATCH"
        );
        let confirmed = json!({
            "cluster_name": "prod-a",
            "snapshot_path": "/var/lib/etcd/backups/pre.db",
            "execution_mode": "execute",
            "confirm_cluster_name": "prod-a"
        });
        assert!(validate("cluster.kubernetes.version.rollback", &confirmed, "prod-a").is_ok());
    }

    #[test]
    fn network_commands_validate_plugin_documents_and_revision() {
        let upgrade = json!({
            "target_cluster_name": "prod-a",
            "plugin": "calico",
            "execution_mode": "simulation",
            "target_version": "1.14.2",
            "documents": "kind: DaemonSet"
        });
        assert!(validate("target.network.upgrade", &upgrade, "prod-a").is_ok());

        let unknown_plugin = json!({
            "target_cluster_name": "prod-a",
            "plugin": "flannel",
            "execution_mode": "simulation",
            "target_version": "1.14.2",
            "documents": "kind: DaemonSet"
        });
        assert_eq!(
            validate("target.network.upgrade", &unknown_plugin, "prod-a")
                .unwrap_err()
                .code,
            "NETWORK_UPGRADE_REQUEST_INVALID"
        );

        let rollback = json!({
            "target_cluster_name": "prod-a",
            "plugin": "sriov",
            "execution_mode": "execute",
            "revision": "20261126T000000Z"
        });
        assert!(validate("target.network.rollback", &rollback, "prod-a").is_ok());
        let bad_revision = json!({
            "target_cluster_name": "prod-a",
            "plugin": "sriov",
            "execution_mode": "execute",
            "revision": "../etc"
        });
        assert!(validate("target.network.rollback", &bad_revision, "prod-a").is_err());
    }

    #[test]
    fn worker_repair_accepts_optional_machine_names() {
        let payload = json!({
            "target_cluster_name": "prod-a",
            "execution_mode": "execute",
            "machine_names": ["worker-1", "worker-2"]
        });
        let normalized = validate("cluster.worker.repair", &payload, "prod-a").unwrap();
        assert_eq!(normalized["machine_names"][1], "worker-2");
        assert_eq!(normalized["target_namespace"], "default");
    }
}
