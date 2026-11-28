//! Tenant token reservation and settlement for model calls owned by async runs.

use chrono::{Datelike, SecondsFormat, Utc};
use sha2::{Digest, Sha256};
use sqlx::{AnyPool, Row};

#[derive(Debug)]
pub struct Reservation {
    pub id: String,
    pub reserved_tokens: i64,
}

fn request_key(run_id: &str, step: &str, lease_token: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(format!("{run_id}:{step}:{lease_token}").as_bytes());
    format!("{:x}", hasher.finalize())
}

async fn period_usage(
    pool: &AnyPool,
    postgres: bool,
    tenant_id: &str,
    start: &str,
    now: &str,
) -> Result<i64, String> {
    let sql = if postgres {
        "SELECT COALESCE(SUM(CASE WHEN status = 'settled' THEN consumed_tokens \
         WHEN status = 'reserved' AND expires_at > $1::timestamptz THEN reserved_tokens ELSE 0 END), 0) \
         FROM core_tenanttokenusage WHERE CAST(tenant_id AS TEXT) = $2 AND created_at >= $3::timestamptz"
    } else {
        "SELECT COALESCE(SUM(CASE WHEN status = 'settled' THEN consumed_tokens \
         WHEN status = 'reserved' AND expires_at > ? THEN reserved_tokens ELSE 0 END), 0) \
         FROM core_tenanttokenusage WHERE CAST(tenant_id AS TEXT) = ? AND created_at >= ?"
    };
    sqlx::query_scalar::<_, i64>(sql)
        .bind(now)
        .bind(tenant_id)
        .bind(start)
        .fetch_one(pool)
        .await
        .map_err(|error| error.to_string())
}

pub struct ReservationRequest<'a> {
    pub tenant_id: &'a str,
    pub task_id: &'a str,
    pub agent_run_id: Option<&'a str>,
    pub workflow_run_id: Option<&'a str>,
    pub run_id: &'a str,
    pub step: &'a str,
    pub lease_token: &'a str,
    pub requested_tokens: i64,
}

/// Reserve tenant budget for one provider call. Returns a ledger row to settle.
pub async fn reserve(
    pool: &AnyPool,
    postgres: bool,
    request: ReservationRequest<'_>,
) -> Result<Reservation, String> {
    let ReservationRequest {
        tenant_id,
        task_id,
        agent_run_id,
        workflow_run_id,
        run_id,
        step,
        lease_token,
        requested_tokens,
    } = request;
    if requested_tokens <= 0 {
        return Err("Token reservation must be a positive integer.".into());
    }
    if (agent_run_id.is_none()) == (workflow_run_id.is_none()) {
        return Err("Exactly one AgentRun or WorkflowRun must own a reservation.".into());
    }
    let key = request_key(run_id, step, lease_token);
    let now = Utc::now();
    let now_text = now.to_rfc3339_opts(SecondsFormat::Micros, false);
    let start = now
        .date_naive()
        .and_hms_opt(0, 0, 0)
        .unwrap()
        .format("%Y-%m-%d %H:%M:%S")
        .to_string();
    let month_start = now
        .date_naive()
        .with_day(1)
        .unwrap()
        .and_hms_opt(0, 0, 0)
        .unwrap()
        .format("%Y-%m-%d %H:%M:%S")
        .to_string();
    let existing = sqlx::query(
        "SELECT CAST(id AS TEXT) AS id, status, CAST(reserved_tokens AS BIGINT) AS reserved_tokens \
         FROM core_tenanttokenusage WHERE CAST(tenant_id AS TEXT) = ? AND idempotency_key = ? LIMIT 1",
    )
    .bind(tenant_id)
    .bind(&key)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())?;
    if let Some(row) = existing {
        let status: String = row.try_get("status").unwrap_or_default();
        let reserved: i64 = row.try_get("reserved_tokens").unwrap_or(0);
        if status == "settled" {
            return Err(
                "A settled provider call cannot be replayed with the same idempotency key.".into(),
            );
        }
        if status == "reserved" {
            if reserved != requested_tokens {
                return Err(
                    "Token reservation amount changed for an existing provider call.".into(),
                );
            }
            return Ok(Reservation {
                id: row.try_get("id").unwrap_or_default(),
                reserved_tokens: reserved,
            });
        }
    }
    let budget = sqlx::query(
        "SELECT CAST(daily_limit AS BIGINT) AS daily_limit, CAST(monthly_limit AS BIGINT) AS monthly_limit \
         FROM core_tenanttokenbudget WHERE CAST(tenant_id AS TEXT) = ? LIMIT 1",
    )
    .bind(tenant_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())?;
    let (daily_limit, monthly_limit) = match budget {
        Some(row) => (
            row.try_get::<i64, _>("daily_limit").unwrap_or(100_000),
            row.try_get::<i64, _>("monthly_limit").unwrap_or(1_000_000),
        ),
        None => {
            let now_expr = if postgres {
                "NOW(), NOW()"
            } else {
                "datetime('now'), datetime('now')"
            };
            sqlx::query(&format!(
                "INSERT INTO core_tenanttokenbudget (tenant_id, daily_limit, monthly_limit, created_at, updated_at) \
                 VALUES (?, 100000, 1000000, {now_expr})"
            ))
            .bind(tenant_id)
            .execute(pool)
            .await
            .map_err(|error| error.to_string())?;
            (100_000, 1_000_000)
        }
    };
    let daily_used = period_usage(pool, postgres, tenant_id, &start, &now_text).await?;
    let monthly_used = period_usage(pool, postgres, tenant_id, &month_start, &now_text).await?;
    if daily_used + requested_tokens > daily_limit {
        return Err("TENANT_TOKEN_BUDGET_EXCEEDED: daily token budget would be exceeded.".into());
    }
    if monthly_used + requested_tokens > monthly_limit {
        return Err("TENANT_TOKEN_BUDGET_EXCEEDED: monthly token budget would be exceeded.".into());
    }
    let id = uuid::Uuid::new_v4().to_string();
    let expires =
        (now + chrono::Duration::minutes(10)).to_rfc3339_opts(SecondsFormat::Micros, false);
    let sql = if postgres {
        "INSERT INTO core_tenanttokenusage (id, idempotency_key, status, reserved_tokens, consumed_tokens, \
         expires_at, created_at, updated_at, agent_run_id, task_id, tenant_id, workflow_run_id) VALUES \
         (?::uuid, ?, 'reserved', ?, 0, ?::timestamptz, ?::timestamptz, ?::timestamptz, ?::uuid, ?::uuid, ?::uuid, ?::uuid)"
    } else {
        "INSERT INTO core_tenanttokenusage (id, idempotency_key, status, reserved_tokens, consumed_tokens, \
         expires_at, created_at, updated_at, agent_run_id, task_id, tenant_id, workflow_run_id) VALUES \
         (?, ?, 'reserved', ?, 0, ?, ?, ?, ?, ?, ?, ?)"
    };
    sqlx::query(sql)
        .bind(&id)
        .bind(&key)
        .bind(requested_tokens)
        .bind(&expires)
        .bind(&now_text)
        .bind(&now_text)
        .bind(agent_run_id)
        .bind(task_id)
        .bind(tenant_id)
        .bind(workflow_run_id)
        .execute(pool)
        .await
        .map_err(|error| error.to_string())?;
    Ok(Reservation {
        id,
        reserved_tokens: requested_tokens,
    })
}

pub struct AdHocRequest<'a> {
    pub tenant_id: &'a str,
    pub scope_ref: &'a str,
    pub idempotency_key: &'a str,
    pub requested_tokens: i64,
}

/// Reserve tenant budget for a provider call with no AgentRun or WorkflowRun.
///
/// Chat turns, scheduled turns, model connection tests and RAG embeddings own
/// no run row, so the ledger entry is anchored to a free-form scope instead.
pub async fn reserve_adhoc(
    pool: &AnyPool,
    postgres: bool,
    request: AdHocRequest<'_>,
) -> Result<Reservation, String> {
    let AdHocRequest {
        tenant_id,
        scope_ref,
        idempotency_key,
        requested_tokens,
    } = request;
    if requested_tokens <= 0 {
        return Err("Token reservation must be a positive integer.".into());
    }
    let trimmed = scope_ref.trim();
    if trimmed.is_empty() || trimmed.len() > 160 {
        return Err(
            "Ad-hoc token reservation scope reference must contain 1 to 160 characters.".into(),
        );
    }
    let now = Utc::now();
    let now_text = now.to_rfc3339_opts(SecondsFormat::Micros, false);
    let start = now
        .date_naive()
        .and_hms_opt(0, 0, 0)
        .unwrap()
        .format("%Y-%m-%d %H:%M:%S")
        .to_string();
    let month_start = now
        .date_naive()
        .with_day(1)
        .unwrap()
        .and_hms_opt(0, 0, 0)
        .unwrap()
        .format("%Y-%m-%d %H:%M:%S")
        .to_string();
    let existing = sqlx::query(
        "SELECT CAST(id AS TEXT) AS id, status, scope_ref, CAST(reserved_tokens AS BIGINT) AS reserved_tokens \
         FROM core_tenanttokenusage WHERE CAST(tenant_id AS TEXT) = ? AND idempotency_key = ? LIMIT 1",
    )
    .bind(tenant_id)
    .bind(idempotency_key)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())?;
    if let Some(row) = existing.as_ref() {
        let stored_scope: String = row.try_get("scope_ref").unwrap_or_default();
        if stored_scope != trimmed {
            return Err("Token reservation idempotency key belongs to another scope.".into());
        }
        let status: String = row.try_get("status").unwrap_or_default();
        let reserved: i64 = row.try_get("reserved_tokens").unwrap_or(0);
        if status == "settled" {
            return Err(
                "A settled provider call cannot be replayed with the same idempotency key.".into(),
            );
        }
        if status == "reserved" {
            if reserved != requested_tokens {
                return Err(
                    "Token reservation amount changed for an existing provider call.".into(),
                );
            }
            return Ok(Reservation {
                id: row.try_get("id").unwrap_or_default(),
                reserved_tokens: reserved,
            });
        }
    }
    let budget = sqlx::query(
        "SELECT CAST(daily_limit AS BIGINT) AS daily_limit, CAST(monthly_limit AS BIGINT) AS monthly_limit \
         FROM core_tenanttokenbudget WHERE CAST(tenant_id AS TEXT) = ? LIMIT 1",
    )
    .bind(tenant_id)
    .fetch_optional(pool)
    .await
    .map_err(|error| error.to_string())?;
    let (daily_limit, monthly_limit) = match budget {
        Some(row) => (
            row.try_get::<i64, _>("daily_limit").unwrap_or(100_000),
            row.try_get::<i64, _>("monthly_limit").unwrap_or(1_000_000),
        ),
        None => {
            let now_expr = if postgres {
                "NOW(), NOW()"
            } else {
                "datetime('now'), datetime('now')"
            };
            sqlx::query(&format!(
                "INSERT INTO core_tenanttokenbudget (tenant_id, daily_limit, monthly_limit, created_at, updated_at) \
                 VALUES (?, 100000, 1000000, {now_expr})"
            ))
            .bind(tenant_id)
            .execute(pool)
            .await
            .map_err(|error| error.to_string())?;
            (100_000, 1_000_000)
        }
    };
    let daily_used = period_usage(pool, postgres, tenant_id, &start, &now_text).await?;
    let monthly_used = period_usage(pool, postgres, tenant_id, &month_start, &now_text).await?;
    if daily_used + requested_tokens > daily_limit {
        return Err("TENANT_TOKEN_BUDGET_EXCEEDED: daily token budget would be exceeded.".into());
    }
    if monthly_used + requested_tokens > monthly_limit {
        return Err("TENANT_TOKEN_BUDGET_EXCEEDED: monthly token budget would be exceeded.".into());
    }
    let id = uuid::Uuid::new_v4().to_string();
    let expires =
        (now + chrono::Duration::minutes(10)).to_rfc3339_opts(SecondsFormat::Micros, false);
    let sql = if postgres {
        "INSERT INTO core_tenanttokenusage (id, idempotency_key, status, reserved_tokens, consumed_tokens, \
         expires_at, created_at, updated_at, agent_run_id, task_id, tenant_id, workflow_run_id, scope_ref) VALUES \
         (?::uuid, ?, 'reserved', ?, 0, ?::timestamptz, ?::timestamptz, ?::timestamptz, NULL, NULL, ?::uuid, NULL, ?)"
    } else {
        "INSERT INTO core_tenanttokenusage (id, idempotency_key, status, reserved_tokens, consumed_tokens, \
         expires_at, created_at, updated_at, agent_run_id, task_id, tenant_id, workflow_run_id, scope_ref) VALUES \
         (?, ?, 'reserved', ?, 0, ?, ?, ?, NULL, NULL, ?, NULL, ?)"
    };
    if let Some(row) = existing.as_ref() {
        if sqlx::query(
            "UPDATE core_tenanttokenusage SET status = 'reserved', reserved_tokens = ?, consumed_tokens = 0, \
             expires_at = ?, updated_at = ?, created_at = ?, scope_ref = ? WHERE CAST(id AS TEXT) = ?",
        )
        .bind(requested_tokens)
        .bind(&expires)
        .bind(&now_text)
        .bind(&now_text)
        .bind(trimmed)
        .bind(row.try_get::<String, _>("id").unwrap_or_default())
        .execute(pool)
        .await
        .is_err()
        {
            return Err("The ad-hoc token reservation could not be refreshed.".into());
        }
        return Ok(Reservation {
            id: row.try_get::<String, _>("id").unwrap_or_default(),
            reserved_tokens: requested_tokens,
        });
    }
    sqlx::query(sql)
        .bind(&id)
        .bind(idempotency_key)
        .bind(requested_tokens)
        .bind(&expires)
        .bind(&now_text)
        .bind(&now_text)
        .bind(tenant_id)
        .bind(trimmed)
        .execute(pool)
        .await
        .map_err(|error| error.to_string())?;
    Ok(Reservation {
        id,
        reserved_tokens: requested_tokens,
    })
}

pub async fn settle(
    pool: &AnyPool,
    reservation: &Reservation,
    consumed_tokens: i64,
) -> Result<(), String> {
    if consumed_tokens < 0 || consumed_tokens > reservation.reserved_tokens {
        return Err("Consumed tokens exceed the reserved provider-call ceiling.".into());
    }
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    sqlx::query(
        "UPDATE core_tenanttokenusage SET status = 'settled', consumed_tokens = ?, reserved_tokens = 0, \
         settled_at = ?, updated_at = ? WHERE CAST(id AS TEXT) = ? AND status = 'reserved'",
    )
    .bind(consumed_tokens)
    .bind(&now)
    .bind(&now)
    .bind(&reservation.id)
    .execute(pool)
    .await
    .map(|_| ())
    .map_err(|error| error.to_string())
}

pub async fn release(pool: &AnyPool, reservation: &Reservation) -> Result<(), String> {
    let now = Utc::now().to_rfc3339_opts(SecondsFormat::Micros, false);
    sqlx::query(
        "UPDATE core_tenanttokenusage SET status = 'released', reserved_tokens = 0, updated_at = ? \
         WHERE CAST(id AS TEXT) = ? AND status = 'reserved'",
    )
    .bind(&now)
    .bind(&reservation.id)
    .execute(pool)
    .await
    .map(|_| ())
    .map_err(|error| error.to_string())
}
