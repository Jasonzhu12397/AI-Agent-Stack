//! Queue the etcd snapshot that follows a cluster mutation.
//!
//! The mutation command's result and its follow-up snapshot share one request
//! flow: after a real change every older snapshot describes a past state, so
//! one backup is queued immediately instead of waiting for the hourly slot.

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use sqlx::{AnyPool, Row};

const ACTION: &str = "cluster.kubernetes.backup.run";
const DEFAULT_DIRECTORY: &str = "/var/lib/etcd/backups";

/// Policy fields copied from `spec.kubernetes` into the backup payload.
const POLICY_FIELDS: [(&str, i64, i64); 4] = [
    ("keep_count", 2, 168),
    ("keep_max_age_hours", 1, 8760),
    ("pre_upgrade_keep_max_age_hours", 1, 8760),
    ("max_age_hours", 1, 8760),
];

fn hour_bucket(now: &str) -> String {
    // `now` is `YYYY-MM-DD HH:MM:SS` in UTC.
    let digits: String = now.chars().filter(char::is_ascii_digit).collect();
    if digits.len() < 10 {
        return "unknown".into();
    }
    format!("{}T{}", &digits[..8], &digits[8..10])
}

fn payload_hash(command_type: &str, payload: &Value) -> String {
    let canonical = json!({"command_type": command_type, "payload": payload});
    hex::encode(Sha256::digest(canonical.to_string().as_bytes()))
}

fn policy_value(kubernetes: &Map<String, Value>, key: &str) -> Result<Option<i64>, &'static str> {
    let Some(value) = kubernetes.get(key) else {
        return Ok(None);
    };
    let Some(number) = value.as_i64() else {
        return Err("ETCD_BACKUP_POLICY_INVALID");
    };
    let bounds = POLICY_FIELDS
        .iter()
        .find(|(field, _, _)| *field == key)
        .map(|(_, low, high)| (*low, *high));
    if let Some((low, high)) = bounds {
        if !(low..=high).contains(&number) {
            return Err("ETCD_BACKUP_POLICY_INVALID");
        }
    }
    Ok(Some(number))
}

/// Build the validated backup payload from a deployment spec. `Err` carries
/// the stable reason the change cannot be snapshotted.
pub fn backup_payload(spec: &Value, cluster_name: &str) -> Result<Value, &'static str> {
    let kubernetes = spec
        .get("kubernetes")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    match kubernetes.get("etcd_backup") {
        None => {}
        Some(Value::Bool(false)) => return Err("ETCD_BACKUP_DISABLED"),
        Some(Value::Bool(true)) => {}
        Some(_) => return Err("ETCD_BACKUP_ENABLED_INVALID"),
    }
    let directory = kubernetes
        .get("directory")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .unwrap_or(DEFAULT_DIRECTORY);
    if !directory.starts_with("/var/lib/etcd")
        || directory.contains("..")
        || !directory
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '/' | '-'))
    {
        return Err("ETCD_BACKUP_DIRECTORY_INVALID");
    }
    let mut payload = Map::new();
    payload.insert("cluster_name".into(), json!(cluster_name));
    payload.insert("directory".into(), json!(directory.trim_end_matches('/')));
    for (key, _, _) in POLICY_FIELDS {
        if let Some(value) = policy_value(&kubernetes, key)? {
            payload.insert(key.into(), json!(value));
        }
    }
    payload.insert("execution_mode".into(), json!("execute"));
    payload.insert("confirm_cluster_name".into(), json!(cluster_name));
    Ok(Value::Object(payload))
}

struct DeploymentContext {
    cluster_name: String,
    spec: Value,
    node_id: String,
}

async fn deployment_context(
    pool: &AnyPool,
    deployment_id: &str,
) -> Result<Option<DeploymentContext>, String> {
    let row = sqlx::query(
        "SELECT cluster_name, CAST(spec AS TEXT) AS spec, CAST(target_node_id AS TEXT) AS target_node_id \
         FROM core_clusterdeployment WHERE CAST(id AS TEXT) = ? LIMIT 1",
    )
    .bind(deployment_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())?;
    let Some(row) = row else {
        return Ok(None);
    };
    let node_id: Option<String> = row.try_get("target_node_id").ok().flatten();
    let Some(node_id) = node_id.filter(|value| !value.is_empty()) else {
        return Ok(None);
    };
    let node_type: Option<String> =
        sqlx::query_scalar("SELECT node_type FROM core_agentnode WHERE CAST(id AS TEXT) = ?")
            .bind(&node_id)
            .fetch_optional(pool)
            .await
            .ok()
            .flatten();
    if node_type.as_deref() != Some("target_cluster") {
        return Ok(None);
    }
    Ok(Some(DeploymentContext {
        cluster_name: row.try_get("cluster_name").unwrap_or_default(),
        spec: row
            .try_get::<String, _>("spec")
            .ok()
            .and_then(|text| serde_json::from_str(&text).ok())
            .unwrap_or_else(|| json!({})),
        node_id,
    }))
}

/// Insert one backup command for a slot, retrying when that slot ended dead.
async fn insert_backup(
    pool: &AnyPool,
    deployment_id: &str,
    node_id: &str,
    base_key: &str,
    payload: &Value,
) -> Result<bool, String> {
    let mut key = base_key.to_string();
    let mut attempt = 0u32;
    loop {
        let existing: Option<(String, String)> = sqlx::query_as(
            "SELECT CAST(id AS TEXT) AS id, status FROM core_agentcommand \
             WHERE CAST(tenant_id AS TEXT) = (SELECT CAST(tenant_id AS TEXT) FROM core_clusterdeployment \
             WHERE CAST(id AS TEXT) = ?) AND idempotency_key = ? LIMIT 1",
        )
        .bind(deployment_id)
        .bind(&key)
        .fetch_optional(pool)
        .await
        .map_err(|error| error.to_string())?;
        if let Some((_, status)) = existing {
            if status != "failed" && status != "cancelled" {
                return Ok(false);
            }
            attempt += 1;
            key = format!("{base_key}:retry:{attempt}");
            continue;
        }
        let context = sqlx::query(
            "SELECT CAST(tenant_id AS TEXT) AS tenant_id, CAST(project_id AS TEXT) AS project_id, \
             requested_by_id FROM core_clusterdeployment WHERE CAST(id AS TEXT) = ? LIMIT 1",
        )
        .bind(deployment_id)
        .fetch_one(pool)
        .await
        .map_err(|error| error.to_string())?;
        let tenant_id: String = context.try_get("tenant_id").unwrap_or_default();
        let project_id: String = context.try_get("project_id").unwrap_or_default();
        let requested_by: i64 = context.try_get("requested_by_id").unwrap_or(0);
        let command_id = uuid::Uuid::new_v4().simple().to_string();
        let insert = "INSERT INTO core_agentcommand (id, command_type, payload, status, idempotency_key, \
                      plan_hash, attempts, max_attempts, created_at, updated_at, node_id, tenant_id, \
                      project_id, requested_by_id, deployment_id) \
                      VALUES (?, ?, ?, 'queued', ?, ?, 0, 3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?)";
        sqlx::query(insert)
            .bind(&command_id)
            .bind(ACTION)
            .bind(serde_json::to_string(payload).unwrap_or_else(|_| "{}".into()))
            .bind(&key)
            .bind(payload_hash(ACTION, payload))
            .bind(node_id)
            .bind(&tenant_id)
            .bind(&project_id)
            .bind(requested_by)
            .bind(deployment_id)
            .execute(pool)
            .await
            .map_err(|error| error.to_string())?;
        return Ok(true);
    }
}

async fn queue_for_deployment(
    pool: &AnyPool,
    deployment_id: &str,
    slot: &str,
) -> Result<bool, String> {
    let Some(context) = deployment_context(pool, deployment_id).await? else {
        return Ok(false);
    };
    let payload = match backup_payload(&context.spec, &context.cluster_name) {
        Ok(payload) => payload,
        Err(_) => return Ok(false),
    };
    let base_key = format!("etcd-backup:{deployment_id}:{slot}");
    insert_backup(pool, deployment_id, &context.node_id, &base_key, &payload).await
}

/// Queue one post-change backup. Returns whether a row was created. A missing
/// target node, a disabled policy or a malformed spec is a quiet no-op.
pub async fn queue_change_backup(
    pool: &AnyPool,
    deployment_id: &str,
    command_id: &str,
    now: &str,
) -> Result<bool, String> {
    queue_for_deployment(
        pool,
        deployment_id,
        &format!("{}:change:cmd:{command_id}", hour_bucket(now)),
    )
    .await
}

/// Queue the recurring hourly backup for one deployment. The hourly slot makes
/// repeated scheduler passes in the same hour create at most one command.
pub async fn queue_hourly_backup(
    pool: &AnyPool,
    deployment_id: &str,
    now: &str,
) -> Result<bool, String> {
    queue_for_deployment(pool, deployment_id, &hour_bucket(now)).await
}

/// Queue the hourly backup for every installed Target cluster.
pub async fn dispatch_hourly_backups(pool: &AnyPool, now: &str) -> Result<usize, String> {
    let deployment_ids: Vec<String> = sqlx::query_scalar(
        "SELECT CAST(id AS TEXT) FROM core_clusterdeployment \
         WHERE status = 'succeeded' AND target_node_id IS NOT NULL",
    )
    .fetch_all(pool)
    .await
    .map_err(|error| error.to_string())?;
    let mut queued = 0;
    for deployment_id in deployment_ids {
        if queue_hourly_backup(pool, &deployment_id, now).await? {
            queued += 1;
        }
    }
    Ok(queued)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn builds_policy_payload_and_refuses_disabled_backups() {
        let spec = json!({
            "kubernetes": {
                "directory": "/var/lib/etcd/backups/",
                "keep_count": 8,
                "keep_max_age_hours": 72,
                "etcd_backup": true
            }
        });
        let payload = backup_payload(&spec, "prod-a").unwrap();
        assert_eq!(payload["directory"], "/var/lib/etcd/backups");
        assert_eq!(payload["keep_count"], 8);
        assert_eq!(payload["confirm_cluster_name"], "prod-a");

        let disabled = json!({"kubernetes": {"etcd_backup": false}});
        assert_eq!(
            backup_payload(&disabled, "prod-a").unwrap_err(),
            "ETCD_BACKUP_DISABLED"
        );
        let bad_switch = json!({"kubernetes": {"etcd_backup": "false"}});
        assert_eq!(
            backup_payload(&bad_switch, "prod-a").unwrap_err(),
            "ETCD_BACKUP_ENABLED_INVALID"
        );
    }
}
