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

/// Append an authentication event to the tenant-less Django audit chain.
///
/// Django hashes a sorted, compact JSON document that includes the previous
/// event hash. Rust must reproduce the exact byte sequence or the chain
/// verifier rejects the event, so the canonical map is a `BTreeMap` and the
/// timestamp matches Python's `datetime.isoformat()` formatting.
pub async fn record_auth_event(
    pool: &AnyPool,
    database_url: &str,
    event: AuthAudit<'_>,
) -> Result<(), String> {
    let postgres =
        database_url.starts_with("postgres://") || database_url.starts_with("postgresql://");
    let timestamp = Utc::now();
    let created_at = isoformat(timestamp);
    let event_id = uuid::Uuid::new_v4();
    let metadata = json!({ "reason": event.reason });

    let mut transaction = pool.begin().await.map_err(|error| error.to_string())?;
    if postgres {
        sqlx::query("SELECT pg_advisory_xact_lock($1)")
            .bind(GLOBAL_AUDIT_CHAIN_LOCK_ID)
            .execute(&mut *transaction)
            .await
            .map_err(|error| error.to_string())?;
    }

    let previous_hash = sqlx::query_scalar::<_, String>(
        "SELECT event_hash FROM core_auditevent \
         WHERE tenant_id IS NULL ORDER BY created_at DESC, id DESC LIMIT 1",
    )
    .fetch_optional(&mut *transaction)
    .await
    .map_err(|error| error.to_string())?
    .unwrap_or_default();

    let canonical = canonical_event(
        &event_id,
        event.actor_id,
        &event,
        &metadata,
        &previous_hash,
        &created_at,
    );
    let event_hash = hex::encode(Sha256::digest(canonical.as_bytes()));

    let sql = if postgres {
        "INSERT INTO core_auditevent (\
         id, tenant_id, project_id, actor_id, event_type, resource_type, \
         resource_id, outcome, request_id, remote_addr, user_agent, metadata, \
         previous_hash, event_hash, created_at) \
         VALUES (?::uuid, NULL, NULL, ?, ?, ?, '', ?, ?, ?, ?, ?::jsonb, ?, ?, ?::timestamptz)"
    } else {
        "INSERT INTO core_auditevent (\
         id, tenant_id, project_id, actor_id, event_type, resource_type, \
         resource_id, outcome, request_id, remote_addr, user_agent, metadata, \
         previous_hash, event_hash, created_at) \
         VALUES (?, NULL, NULL, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?)"
    };
    let query = sqlx::query(sql)
        .bind(event_id.to_string())
        .bind(event.actor_id)
        .bind(event.event_type)
        .bind("session")
        .bind(event.outcome)
        .bind(event.request_id)
        .bind(event.remote_addr)
        .bind(event.user_agent)
        .bind(metadata.to_string())
        .bind(&previous_hash)
        .bind(&event_hash)
        .bind(&created_at);
    query
        .execute(&mut *transaction)
        .await
        .map_err(|error| error.to_string())?;
    transaction
        .commit()
        .await
        .map_err(|error| error.to_string())?;
    Ok(())
}

fn canonical_event(
    event_id: &uuid::Uuid,
    actor_id: Option<i64>,
    event: &AuthAudit<'_>,
    metadata: &Value,
    previous_hash: &str,
    created_at: &str,
) -> String {
    let mut map: BTreeMap<&str, Value> = BTreeMap::new();
    map.insert("id", json!(event_id.to_string()));
    map.insert("tenant_id", Value::Null);
    map.insert("project_id", Value::Null);
    map.insert("actor_id", json!(actor_id));
    map.insert("event_type", json!(event.event_type));
    map.insert("resource_type", json!("session"));
    map.insert("resource_id", json!(""));
    map.insert("outcome", json!(event.outcome));
    map.insert("request_id", json!(event.request_id));
    map.insert("remote_addr", json!(event.remote_addr));
    map.insert("user_agent", json!(event.user_agent));
    map.insert("metadata", metadata.clone());
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

    #[test]
    fn canonical_json_is_sorted_and_compact() {
        let id = uuid::Uuid::nil();
        let event = AuthAudit {
            event_type: "auth.login.succeeded",
            outcome: "success",
            reason: "authenticated",
            actor_id: Some(7),
            request_id: "req-1",
            remote_addr: None,
            user_agent: "test",
        };
        let value = canonical_event(
            &id,
            Some(7),
            &event,
            &json!({"reason": "authenticated"}),
            "",
            "2026-11-26T08:00:00+00:00",
        );
        assert!(value.starts_with('{'));
        assert!(!value.contains(' '));
        assert!(value.contains("\"actor_id\":7"));
        assert!(value.find("actor_id") < value.find("created_at"));
    }
}
