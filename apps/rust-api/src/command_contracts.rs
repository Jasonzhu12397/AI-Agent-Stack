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

fn bool_is(result: &Value, key: &str, expected: bool) -> bool {
    result.get(key).and_then(Value::as_bool) == Some(expected)
}

fn str_field<'a>(result: &'a Value, key: &str) -> Option<&'a str> {
    result.get(key).and_then(Value::as_str)
}

fn matches_field(result: &Value, payload: &Value, key: &str, payload_key: &str) -> bool {
    result.get(key) == payload.get(payload_key)
}

fn steps_are(result: &Value, names: &[&str], statuses: Option<(&str, &str)>) -> bool {
    let Some(steps) = result.get("upgrade_steps").or(result.get("steps")) else {
        return false;
    };
    let Value::Array(steps) = steps else {
        return false;
    };
    if steps.len() != names.len() {
        return false;
    }
    steps.iter().enumerate().all(|(index, step)| {
        step.get("sequence").and_then(Value::as_i64) == Some(index as i64 + 1)
            && step.get("name").and_then(Value::as_str) == Some(names[index])
            && statuses.is_none_or(|(allowed_a, allowed_b)| {
                matches!(
                    step.get("status").and_then(Value::as_str),
                    Some(status) if status == allowed_a || status == allowed_b
                )
            })
    })
}

fn validate_executed_upgrade(result: &Value) -> Option<&'static str> {
    if !matches!(str_field(result, "execution_mode"), Some("execute"))
        || !bool_is(result, "approval_required", true)
        || !bool_is(result, "upgrade_simulated", false)
        || !bool_is(result, "requires_manual_execution", false)
        || !bool_is(result, "resume_supported", false)
        || !bool_is(result, "side_effects", true)
    {
        return Some("Kubernetes upgrade result violates the execution safety contract.");
    }
    if !bool_is(result, "upgrade_performed", true) {
        if bool_is(result, "upgrade_performed", false) && bool_is(result, "already_at_target", true)
        {
            if result
                .get("performed_steps")
                .and_then(Value::as_array)
                .is_some_and(|steps| {
                    steps
                        .iter()
                        .filter_map(Value::as_str)
                        .any(|step| step.starts_with("control_plane") || step.starts_with("worker"))
                })
            {
                return Some("A retry that changed nothing cannot report upgraded nodes.");
            }
        } else {
            return Some("Kubernetes upgrade result violates the execution safety contract.");
        }
    }
    let names = ["preflight", "control_plane", "workers", "postflight"];
    let Some(steps) = result.get("upgrade_steps").and_then(Value::as_array) else {
        return Some("Kubernetes upgrade execution must report the four ordered stages.");
    };
    if steps.len() != names.len()
        || steps.iter().enumerate().any(|(index, step)| {
            step.get("sequence").and_then(Value::as_i64) != Some(index as i64 + 1)
                || step.get("name").and_then(Value::as_str) != Some(names[index])
                || !matches!(
                    step.get("status").and_then(Value::as_str),
                    Some("succeeded" | "skipped")
                )
        })
    {
        return Some("Kubernetes upgrade stages must all have run to completion.");
    }
    let Some(preflight) = result.get("preflight").filter(|value| value.is_object()) else {
        return Some("A completed Kubernetes upgrade must report its preflight evidence.");
    };
    if preflight
        .get("checks")
        .and_then(Value::as_array)
        .is_none_or(Vec::is_empty)
    {
        return Some("A completed Kubernetes upgrade must list the preflight checks it ran.");
    }
    if preflight.get("blocked").and_then(Value::as_bool) != Some(false) {
        return Some("A Kubernetes upgrade cannot complete while its preflight is blocked.");
    }
    None
}

fn validate_kubernetes_rollback_result(payload: &Value, result: &Value) -> Option<&'static str> {
    if !matches_field(result, payload, "cluster_name", "cluster_name")
        || !matches_field(result, payload, "snapshot_path", "snapshot_path")
        || !matches_field(result, payload, "execution_mode", "execution_mode")
    {
        return Some("Kubernetes rollback result does not match the approved command.");
    }
    let names = [
        "preflight",
        "etcd_restore",
        "data_swap",
        "kubelet_restart",
        "postflight",
    ];
    if !steps_are(result, &names, None) {
        return Some("A Kubernetes rollback must report the five ordered stages.");
    }
    let simulation = str_field(payload, "execution_mode") == Some("simulation");
    if simulation {
        if result
            .get("steps")
            .and_then(Value::as_array)
            .is_some_and(|steps| {
                steps
                    .iter()
                    .any(|step| step.get("status").and_then(Value::as_str) != Some("not_started"))
            })
        {
            return Some("A simulated rollback must not claim any stage ran.");
        }
        if !bool_is(result, "approval_required", true)
            || !bool_is(result, "rollback_simulated", true)
            || !bool_is(result, "rolled_back", false)
            || !result
                .get("performed_steps")
                .and_then(Value::as_array)
                .is_some_and(Vec::is_empty)
        {
            return Some("A simulated rollback must not claim the cluster was restored.");
        }
        return None;
    }
    if !bool_is(result, "approval_required", true)
        || !bool_is(result, "rollback_simulated", false)
        || !bool_is(result, "rolled_back", true)
    {
        return Some("An executed rollback must report that the cluster was restored.");
    }
    let performed = result.get("performed_steps").and_then(Value::as_array);
    if performed.is_none_or(|steps| {
        ["etcd_restore", "data_swap", "kubelet_restart", "postflight"]
            .iter()
            .any(|required| !steps.iter().any(|item| item.as_str() == Some(required)))
    }) {
        return Some("An executed rollback must report every stage it performed.");
    }
    if result
        .get("steps")
        .and_then(Value::as_array)
        .is_none_or(|steps| {
            steps.iter().any(|step| {
                !matches!(
                    step.get("status").and_then(Value::as_str),
                    Some("succeeded" | "skipped")
                )
            })
        })
    {
        return Some("Kubernetes rollback stages must all have run to completion.");
    }
    let Some(preflight) = result.get("preflight").filter(|value| value.is_object()) else {
        return Some("A completed rollback must report its preflight evidence.");
    };
    if preflight
        .get("checks")
        .and_then(Value::as_array)
        .is_none_or(Vec::is_empty)
    {
        return Some("A completed rollback must list the preflight checks it ran.");
    }
    if preflight.get("blocked").and_then(Value::as_bool) != Some(false) {
        return Some("A rollback cannot complete while its preflight is blocked.");
    }
    if !str_field(result, "previous_member_dir")
        .is_some_and(|path| path.starts_with("/var/lib/etcd/member.pre-rollback-"))
    {
        return Some("A rollback must keep the previous etcd data directory on disk.");
    }
    None
}

fn validate_network_result(
    command_type: &str,
    payload: &Value,
    result: &Value,
) -> Option<&'static str> {
    let rollback = command_type == "target.network.rollback";
    if !matches_field(result, payload, "cluster_name", "target_cluster_name")
        || !matches_field(result, payload, "plugin", "plugin")
        || !matches!(
            result.get("namespace").and_then(Value::as_str),
            Some(namespace)
                if Some(namespace) == payload.get("target_namespace").and_then(Value::as_str).or(Some("default"))
        )
    {
        return Some("Network plugin result does not match the approved command.");
    }
    let mode = payload.get("execution_mode").and_then(Value::as_str);
    if rollback {
        let result_revision = result.get("revision").and_then(Value::as_str);
        if result_revision.is_none_or(str::is_empty) {
            return Some("A network rollback must name the revision it replayed.");
        }
        if payload
            .get("revision")
            .and_then(Value::as_str)
            .is_some_and(|revision| Some(revision) != result_revision)
        {
            return Some("A network rollback replayed a different revision.");
        }
        if mode == Some("execute") {
            if !bool_is(result, "rollback_simulated", false)
                || !bool_is(result, "rollback_performed", true)
                || !bool_is(result, "side_effects", true)
            {
                return Some("An executed rollback must report the replay and its side effects.");
            }
            let restored = result.get("restored_objects").and_then(Value::as_array);
            let removed = result.get("removed_objects").and_then(Value::as_array);
            if restored.is_none() || removed.is_none() {
                return Some("A network rollback must report restored and removed objects.");
            }
            if restored.is_some_and(Vec::is_empty) && removed.is_some_and(Vec::is_empty) {
                return Some("A network rollback must replay at least one object.");
            }
            return None;
        }
        if !bool_is(result, "approval_required", true)
            || !bool_is(result, "rollback_simulated", true)
            || !bool_is(result, "rollback_performed", false)
            || !bool_is(result, "side_effects", false)
        {
            return Some("A rollback plan must be simulation-only until approved.");
        }
        return None;
    }
    if !matches_field(result, payload, "target_version", "target_version") {
        return Some("The upgrade result changed the approved target version.");
    }
    if mode == Some("execute") {
        if !bool_is(result, "upgrade_simulated", false)
            || !bool_is(result, "upgrade_performed", true)
            || !bool_is(result, "side_effects", true)
        {
            return Some("An executed upgrade must report the applied bundle and side effects.");
        }
        if !str_field(result, "revision").is_some_and(|value| !value.is_empty()) {
            return Some("An executed upgrade must report its captured revision.");
        }
        if !result
            .get("ready_workloads")
            .and_then(Value::as_array)
            .is_some_and(|items| !items.is_empty())
        {
            return Some("An executed upgrade must report ready workload objects.");
        }
        return None;
    }
    if !bool_is(result, "approval_required", true)
        || !bool_is(result, "upgrade_simulated", true)
        || !bool_is(result, "upgrade_performed", false)
        || !bool_is(result, "side_effects", false)
    {
        return Some("An upgrade plan must be simulation-only until approved.");
    }
    None
}

fn validate_network_revisions_result(payload: &Value, result: &Value) -> Option<&'static str> {
    if !matches_field(result, payload, "cluster_name", "target_cluster_name")
        || !matches_field(result, payload, "plugin", "plugin")
        || !matches!(
            result.get("namespace").and_then(Value::as_str),
            Some(namespace)
                if Some(namespace) == payload.get("target_namespace").and_then(Value::as_str).or(Some("default"))
        )
    {
        return Some("Network revision list does not match the requested scope.");
    }
    if !bool_is(result, "approval_required", false) || !bool_is(result, "side_effects", false) {
        return Some("A revision list is a read-only observation.");
    }
    let Some(revisions) = result.get("revisions").and_then(Value::as_array) else {
        return Some("A revision list must report a revisions array.");
    };
    if result
        .get("latest_revision")
        .is_some_and(|value| !value.is_null() && !value.is_string())
    {
        return Some("The latest revision must be a string or null.");
    }
    let names: Vec<&str> = revisions
        .iter()
        .map(|revision| {
            revision
                .get("revision")
                .and_then(Value::as_str)
                .unwrap_or("")
        })
        .collect();
    if revisions.iter().any(|revision| {
        revision
            .get("revision")
            .and_then(Value::as_str)
            .is_none_or(str::is_empty)
            || revision
                .get("config_map")
                .and_then(Value::as_str)
                .is_none_or(str::is_empty)
            || revision
                .get("object_count")
                .and_then(Value::as_i64)
                .is_none_or(|count| count < 1)
    }) {
        return Some("Every revision entry must name its revision and ConfigMap with a positive object count.");
    }
    if let Some(latest) = result.get("latest_revision").and_then(Value::as_str) {
        if !revisions.is_empty() && !names.contains(&latest) {
            return Some("The latest revision must appear in the revisions array.");
        }
    }
    None
}

fn validate_scale_result(payload: &Value, result: &Value) -> Option<&'static str> {
    if !matches_field(result, payload, "cluster_name", "target_cluster_name")
        || !matches_field(result, payload, "desired_workers", "desired_workers")
    {
        return Some("Cluster scale result does not match the approved command.");
    }
    let Some(machine_deployment) = result
        .get("machine_deployment")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
    else {
        return Some("Cluster scale result must name the MachineDeployment it scaled.");
    };
    if payload
        .get("machine_deployment")
        .and_then(Value::as_str)
        .is_some_and(|expected| expected != machine_deployment)
    {
        return Some("Cluster scale result does not match the approved MachineDeployment.");
    }
    if payload.get("execution_mode").and_then(Value::as_str) == Some("execute") {
        if !bool_is(result, "scale_simulated", false) {
            return Some("An executed scale cannot report a simulation.");
        }
        if bool_is(result, "scale_performed", true) {
            if !bool_is(result, "side_effects", true) {
                return Some("An executed scale must report side effects.");
            }
            if !result
                .get("controller_status")
                .is_some_and(Value::is_object)
            {
                return Some("An executed scale must report controller status.");
            }
            if result.get("controller_replicas").and_then(Value::as_i64)
                != payload.get("desired_workers").and_then(Value::as_i64)
            {
                return Some("The controller did not reach the requested replica count.");
            }
        } else if !bool_is(result, "side_effects", false) {
            return Some("A no-op scale cannot report side effects.");
        }
        return None;
    }
    if !bool_is(result, "approval_required", true)
        || !bool_is(result, "scale_simulated", true)
        || !bool_is(result, "scale_performed", false)
        || !bool_is(result, "side_effects", false)
    {
        return Some("A scale plan must be simulation-only until approved.");
    }
    if !bool_is(result, "bmh_capacity_sufficient", true) {
        return Some("A scale plan must show sufficient BareMetalHost capacity.");
    }
    None
}

fn validate_repair_result(payload: &Value, result: &Value) -> Option<&'static str> {
    if !matches_field(result, payload, "cluster_name", "target_cluster_name")
        || !matches!(
            result.get("namespace").and_then(Value::as_str),
            Some(namespace)
                if Some(namespace) == payload.get("target_namespace").and_then(Value::as_str).or(Some("default"))
        )
    {
        return Some("Cluster repair result does not match the approved command.");
    }
    if !str_field(result, "machine_deployment").is_some_and(|value| !value.is_empty()) {
        return Some("Cluster repair result must name the MachineDeployment it inspected.");
    }
    let Some(unhealthy) = result.get("unhealthy_machines").and_then(Value::as_array) else {
        return Some("Cluster repair result must list unhealthy Machine names.");
    };
    if unhealthy.iter().any(|name| name.as_str().is_none()) {
        return Some("Cluster repair result must list unhealthy Machine names.");
    }
    let selected = result.get("selected_machine");
    if unhealthy.is_empty() {
        if !selected.is_none() && !selected.is_some_and(Value::is_null) {
            return Some("A repair with no unhealthy Machine cannot select one.");
        }
    } else if !selected
        .and_then(Value::as_str)
        .is_some_and(|name| unhealthy.iter().any(|value| value.as_str() == Some(name)))
    {
        return Some("A repair with unhealthy Machines must select one of them.");
    }
    if payload.get("execution_mode").and_then(Value::as_str) == Some("execute") {
        if !bool_is(result, "repair_simulated", false) {
            return Some("An executed repair cannot report a simulation.");
        }
        if bool_is(result, "repair_performed", true) {
            if !bool_is(result, "side_effects", true) {
                return Some("An executed repair must report side effects.");
            }
            if result
                .get("deleted_machines")
                .and_then(Value::as_array)
                .is_none_or(|deleted| {
                    !(deleted.len() == 1
                        && selected
                            .and_then(Value::as_str)
                            .is_some_and(|name| deleted[0].as_str() == Some(name)))
                })
            {
                return Some("An executed repair must report the selected Machine as deleted.");
            }
        } else if !bool_is(result, "side_effects", false) {
            return Some("A no-op repair cannot report side effects.");
        }
        return None;
    }
    if !bool_is(result, "approval_required", true)
        || !bool_is(result, "repair_simulated", true)
        || !bool_is(result, "repair_performed", false)
        || !bool_is(result, "side_effects", false)
    {
        return Some("A repair plan must be simulation-only until approved.");
    }
    None
}

/// Check the result an Agent reports for a contract command. `None` means the
/// result is consistent with the approved payload; `Some(message)` is a 409.
pub fn validate_result(
    command_type: &str,
    payload: &Value,
    result: &Value,
) -> Option<&'static str> {
    match command_type {
        "cluster.kubernetes.version.upgrade" => {
            if !matches_field(result, payload, "cluster_name", "cluster_name")
                || !matches_field(result, payload, "current_version", "current_version")
                || !matches_field(result, payload, "target_version", "target_version")
            {
                return Some("Kubernetes upgrade result does not match the approved command.");
            }
            if payload.get("execution_mode").and_then(Value::as_str) == Some("execute") {
                return validate_executed_upgrade(result);
            }
            if !bool_is(result, "approval_required", true)
                || !bool_is(result, "upgrade_simulated", true)
                || !bool_is(result, "upgrade_performed", false)
                || !bool_is(result, "side_effects", false)
                || !bool_is(result, "requires_manual_execution", true)
                || !bool_is(result, "resume_supported", false)
            {
                return Some(
                    "Kubernetes upgrade result violates the simulation-only safety contract.",
                );
            }
            let Some(steps) = result.get("upgrade_steps").and_then(Value::as_array) else {
                return Some(
                    "Kubernetes upgrade plan must contain the four ordered, unexecuted stages.",
                );
            };
            let expected = [
                ("preflight", "not_run"),
                ("control_plane", "not_started"),
                ("workers", "not_started"),
                ("postflight", "not_started"),
            ];
            if steps.len() != expected.len()
                || steps.iter().enumerate().any(|(index, step)| {
                    step.get("sequence").and_then(Value::as_i64) != Some(index as i64 + 1)
                        || step.get("name").and_then(Value::as_str) != Some(expected[index].0)
                        || step.get("status").and_then(Value::as_str) != Some(expected[index].1)
                })
            {
                return Some("Kubernetes upgrade plan stages must remain ordered and unexecuted.");
            }
            None
        }
        "cluster.kubernetes.version.rollback" => {
            validate_kubernetes_rollback_result(payload, result)
        }
        "cluster.scale" => validate_scale_result(payload, result),
        "cluster.worker.repair" => validate_repair_result(payload, result),
        "target.network.upgrade" | "target.network.rollback" => {
            validate_network_result(command_type, payload, result)
        }
        "target.network.revisions" => validate_network_revisions_result(payload, result),
        _ => None,
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
    fn upgrade_result_must_prove_the_simulated_stages() {
        let payload = json!({
            "cluster_name": "prod-a",
            "current_version": "v1.34.2",
            "target_version": "v1.35.0",
            "execution_mode": "simulation"
        });
        let steps = json!([
            {"sequence": 1, "name": "preflight", "status": "not_run"},
            {"sequence": 2, "name": "control_plane", "status": "not_started"},
            {"sequence": 3, "name": "workers", "status": "not_started"},
            {"sequence": 4, "name": "postflight", "status": "not_started"}
        ]);
        let result = json!({
            "cluster_name": "prod-a",
            "current_version": "v1.34.2",
            "target_version": "v1.35.0",
            "approval_required": true,
            "upgrade_simulated": true,
            "upgrade_performed": false,
            "side_effects": false,
            "requires_manual_execution": true,
            "resume_supported": false,
            "upgrade_steps": steps
        });
        assert_eq!(
            validate_result("cluster.kubernetes.version.upgrade", &payload, &result),
            None
        );
        let unsafe_result = json!({
            "cluster_name": "prod-a",
            "current_version": "v1.34.2",
            "target_version": "v1.35.0",
            "approval_required": true,
            "upgrade_simulated": false,
            "upgrade_performed": true,
            "side_effects": true
        });
        assert!(validate_result(
            "cluster.kubernetes.version.upgrade",
            &payload,
            &unsafe_result
        )
        .is_some());
    }

    #[test]
    fn executed_scale_requires_the_controller_to_reach_the_request() {
        let payload = json!({
            "target_cluster_name": "prod-a",
            "desired_workers": 3,
            "execution_mode": "execute",
            "machine_deployment": "md-0"
        });
        let result = json!({
            "cluster_name": "prod-a",
            "desired_workers": 3,
            "machine_deployment": "md-0",
            "scale_simulated": false,
            "scale_performed": true,
            "side_effects": true,
            "controller_status": {},
            "controller_replicas": 3
        });
        assert_eq!(validate_result("cluster.scale", &payload, &result), None);
        let mut short = result.clone();
        short["controller_replicas"] = json!(2);
        assert!(validate_result("cluster.scale", &payload, &short).is_some());
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
