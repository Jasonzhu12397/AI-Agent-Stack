use std::collections::BTreeMap;

use chrono::{SecondsFormat, Utc};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use sqlx::AnyPool;

const GLOBAL_AUDIT_CHAIN_LOCK_ID: i64 = 930417;

pub struct AuthAudit<'a> {
    pub event_type: &'a str,
    pub outcome: &'a str,
    pub reason: &'a str,
    pub actor_id: Option<i64>,
    pub request_id: &'a str,
    pub remote_addr: Option<&'a str>,
    pub user_agent: &'a str,
}

pub struct AuditEvent<'a> {
    pub tenant_id: Option<&'a str>,
    pub project_id: Option<&'a str>,
    pub event_type: &'a str,
    pub resource_type: &'a str,
    pub resource_id: &'a str,
    pub outcome: &'a str,
    pub actor_id: Option<i64>,
    pub request_id: &'a str,
    pub remote_addr: Option<&'a str>,
    pub user_agent: &'a str,
    pub metadata: Value,
}

/// Append an authentication event to the tenant-less Django audit chain.
pub async fn record_auth_event(
    pool: &AnyPool,
    database_url: &str,
    event: AuthAudit<'_>,
) -> Result<(), String> {
    record_event(
        pool,
        database_url,
        AuditEvent {
            tenant_id: None,
            project_id: None,
            event_type: event.event_type,
            resource_type: "session",
            resource_id: "",
            outcome: event.outcome,
            actor_id: event.actor_id,
            request_id: event.request_id,
            remote_addr: event.remote_addr,
            user_agent: event.user_agent,
            metadata: json!({ "reason": event.reason }),
        },
    )
    .await
}

/// Append an event to the tenant-scoped Django audit chain.
///
/// Django serializes tenant chains by locking the tenant row, so Rust takes
/// the same row lock before reading the previous hash. The canonical document
/// must match Django byte for byte or `verify_global_audit_chain` rejects it.
pub async fn record_event(
    pool: &AnyPool,
    database_url: &str,
    event: AuditEvent<'_>,
) -> Result<(), String> {
    let postgres =
        database_url.starts_with("postgres://") || database_url.starts_with("postgresql://");
    let mut transaction = pool.begin().await.map_err(|error| error.to_string())?;
    record_event_in_transaction(&mut transaction, postgres, &event).await?;
    transaction
        .commit()
        .await
        .map_err(|error| error.to_string())
}

/// Append the audit row inside a caller-owned transaction.
///
/// Tenant writes must commit the business row and its audit row together, so
/// this function never opens or commits its own transaction.
pub async fn record_event_in_transaction(
    transaction: &mut sqlx::Transaction<'_, sqlx::Any>,
    postgres: bool,
    event: &AuditEvent<'_>,
) -> Result<(), String> {
    let timestamp = Utc::now();
    let created_at = isoformat(timestamp);
    let event_id = uuid::Uuid::new_v4();

    if postgres {
        match event.tenant_id {
            Some(tenant_id) => {
                sqlx::query("SELECT id FROM core_tenant WHERE id = $1::uuid FOR UPDATE")
                    .bind(tenant_id)
                    .execute(&mut **transaction)
                    .await
                    .map_err(|error| error.to_string())?;
            }
            None => {
                sqlx::query("SELECT pg_advisory_xact_lock($1)")
                    .bind(GLOBAL_AUDIT_CHAIN_LOCK_ID)
                    .execute(&mut **transaction)
                    .await
                    .map_err(|error| error.to_string())?;
            }
        }
    }

    let previous_hash = match event.tenant_id {
        Some(tenant_id) if postgres => {
            sqlx::query_scalar::<_, String>(
                "SELECT event_hash FROM core_auditevent \
             WHERE tenant_id = $1::uuid ORDER BY created_at DESC, id DESC LIMIT 1",
            )
            .bind(tenant_id)
            .fetch_optional(&mut **transaction)
            .await
        }
        Some(tenant_id) => {
            sqlx::query_scalar::<_, String>(
                "SELECT event_hash FROM core_auditevent \
             WHERE tenant_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            )
            .bind(tenant_id)
            .fetch_optional(&mut **transaction)
            .await
        }
        None => {
            sqlx::query_scalar::<_, String>(
                "SELECT event_hash FROM core_auditevent \
             WHERE tenant_id IS NULL ORDER BY created_at DESC, id DESC LIMIT 1",
            )
            .fetch_optional(&mut **transaction)
            .await
        }
    }
    .map_err(|error| error.to_string())?
    .unwrap_or_default();

    let canonical = canonical_event(&event_id, event, &previous_hash, &created_at);
    let event_hash = hex::encode(Sha256::digest(canonical.as_bytes()));

    let sql = if postgres {
        "INSERT INTO core_auditevent (\
         id, tenant_id, project_id, actor_id, event_type, resource_type, \
         resource_id, outcome, request_id, remote_addr, user_agent, metadata, \
         previous_hash, event_hash, created_at) \
         VALUES (?::uuid, ?::uuid, ?::uuid, ?, ?, ?, ?, ?, ?, ?, ?, ?::jsonb, ?, ?, ?::timestamptz)"
    } else {
        "INSERT INTO core_auditevent (\
         id, tenant_id, project_id, actor_id, event_type, resource_type, \
         resource_id, outcome, request_id, remote_addr, user_agent, metadata, \
         previous_hash, event_hash, created_at) \
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    };
    sqlx::query(sql)
        .bind(event_id.to_string())
        .bind(event.tenant_id)
        .bind(event.project_id)
        .bind(event.actor_id)
        .bind(event.event_type)
        .bind(event.resource_type)
        .bind(event.resource_id)
        .bind(event.outcome)
        .bind(event.request_id)
        .bind(event.remote_addr)
        .bind(event.user_agent)
        .bind(event.metadata.to_string())
        .bind(&previous_hash)
        .bind(&event_hash)
        .bind(&created_at)
        .execute(&mut **transaction)
        .await
        .map_err(|error| error.to_string())?;
    Ok(())
}

fn canonical_event(
    event_id: &uuid::Uuid,
    event: &AuditEvent<'_>,
    previous_hash: &str,
    created_at: &str,
) -> String {
    let mut map: BTreeMap<&str, Value> = BTreeMap::new();
    map.insert("id", json!(event_id.to_string()));
    map.insert("tenant_id", json!(event.tenant_id));
    map.insert("project_id", json!(event.project_id));
    map.insert("actor_id", json!(event.actor_id));
    map.insert("event_type", json!(event.event_type));
    map.insert("resource_type", json!(event.resource_type));
    map.insert("resource_id", json!(event.resource_id));
    map.insert("outcome", json!(event.outcome));
    map.insert("request_id", json!(event.request_id));
    map.insert("remote_addr", json!(event.remote_addr));
    map.insert("user_agent", json!(event.user_agent));
    map.insert("metadata", event.metadata.clone());
    map.insert("previous_hash", json!(previous_hash));
    map.insert("created_at", json!(created_at));
    serde_json::to_string(&map).unwrap_or_default()
}

fn isoformat(timestamp: chrono::DateTime<Utc>) -> String {
    if timestamp.timestamp_subsec_micros() == 0 {
        timestamp.format("%Y-%m-%dT%H:%M:%S+00:00").to_string()
    } else {
        timestamp
            .to_rfc3339_opts(SecondsFormat::Micros, false)
            .to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn auth_event<'a>(event_type: &'a str) -> AuthAudit<'a> {
        AuthAudit {
            event_type,
            outcome: "success",
            reason: "authenticated",
            actor_id: Some(7),
            request_id: "req-1",
            remote_addr: None,
            user_agent: "test",
        }
    }

    #[test]
    fn canonical_json_is_sorted_and_compact() {
        let event = AuditEvent {
            tenant_id: None,
            project_id: None,
            event_type: "auth.login.succeeded",
            resource_type: "session",
            resource_id: "",
            outcome: "success",
            actor_id: Some(7),
            request_id: "req-1",
            remote_addr: None,
            user_agent: "test",
            metadata: json!({"reason": "authenticated"}),
        };
        let value = canonical_event(&uuid::Uuid::nil(), &event, "", "2026-11-26T08:00:00+00:00");
        assert!(value.starts_with('{'));
        assert!(!value.contains(' '));
        assert!(value.contains("\"actor_id\":7"));
        assert!(value.find("actor_id") < value.find("created_at"));
    }

    #[test]
    fn tenant_events_keep_the_tenant_and_project_ids() {
        let event = AuditEvent {
            tenant_id: Some("11111111-1111-1111-1111-111111111111"),
            project_id: Some("22222222-2222-2222-2222-222222222222"),
            event_type: "project.created",
            resource_type: "project",
            resource_id: "22222222-2222-2222-2222-222222222222",
            outcome: "success",
            actor_id: Some(7),
            request_id: "",
            remote_addr: None,
            user_agent: "",
            metadata: json!({"slug": "demo", "name": "Demo"}),
        };
        let value = canonical_event(&uuid::Uuid::nil(), &event, "", "2026-11-26T08:00:00+00:00");
        assert!(value.contains("\"tenant_id\":\"11111111"));
        assert!(value.contains("\"project_id\":\"22222222"));
        assert!(auth_event("auth.login.succeeded").event_type == "auth.login.succeeded");
    }
}
