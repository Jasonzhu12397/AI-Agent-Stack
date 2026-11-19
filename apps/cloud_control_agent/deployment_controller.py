"""Result-driven deployment progression for the Cloud Control Agent.

The state machine in :mod:`packages.agent_contracts.orchestration` knows which
step comes next; this module is the side-effecting bridge that turns that plan
into a queued command. It runs only after a successful result whose checkpoint
is already visible on the deployment, and it deduplicates through the same
idempotency key used by manual dispatch. A missing eligible Agent is not an
error: the step is left pending and Agent registration retries it later.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from packages.agent_contracts.orchestration import (
    OrchestrationError,
    is_terminal,
    plan_next_step,
)

MAX_COMMAND_ATTEMPTS = 3

LOG = logging.getLogger("cloud-control-agent")


def _deployment(connection: psycopg.Connection[Any], deployment_id: UUID) -> dict[str, Any] | None:
    return connection.execute(
        """
        SELECT deployment_id, installation_id, cluster_id, phase, spec, state
        FROM agent_control_deployments
        WHERE deployment_id = %s
        """,
        (deployment_id,),
    ).fetchone()


def _latest_command(
    connection: psycopg.Connection[Any],
    deployment_id: UUID,
    command_id: UUID | None,
) -> dict[str, Any] | None:
    if command_id is not None:
        return connection.execute(
            """
            SELECT command_id, status, action, from_phase, next_phase
            FROM agent_control_commands
            WHERE command_id = %s AND deployment_id = %s
            """,
            (command_id, deployment_id),
        ).fetchone()
    return connection.execute(
        """
        SELECT command_id, status, action, from_phase, next_phase
        FROM agent_control_commands
        WHERE deployment_id = %s AND status = 'succeeded'
        ORDER BY updated_at DESC, created_at DESC
        LIMIT 1
        """,
        (deployment_id,),
    ).fetchone()


def _eligible_agent(
    connection: psycopg.Connection[Any],
    deployment: dict[str, Any],
    role: str,
) -> UUID | None:
    installation_id = deployment["installation_id"]
    if role == "target":
        row = connection.execute(
            """
            SELECT agent_id FROM agent_control_agents
            WHERE role = 'target' AND enabled = TRUE
              AND installation_id = %s AND cluster_id = %s
            ORDER BY last_seen DESC NULLS LAST, agent_id
            LIMIT 1
            """,
            (installation_id, deployment["cluster_id"]),
        ).fetchone()
    else:
        row = connection.execute(
            """
            SELECT agent_id FROM agent_control_agents
            WHERE role = 'eph' AND enabled = TRUE
              AND installation_id = %s AND cluster_id IS NULL
            ORDER BY last_seen DESC NULLS LAST, agent_id
            LIMIT 1
            """,
            (installation_id,),
        ).fetchone()
    return row["agent_id"] if row else None


def advance_after_command(
    connection: psycopg.Connection[Any],
    deployment_id: UUID,
    command_id: UUID | None = None,
    *,
    require_command: bool = True,
) -> dict[str, Any]:
    """Queue the next planned command after a successful, checkpointed result.

    The function is idempotent: an existing command with the planned
    idempotency key is returned as ``exists`` instead of being duplicated.
    """

    deployment = _deployment(connection, deployment_id)
    if deployment is None:
        return {"status": "deployment_not_found"}
    phase = deployment["phase"]
    if is_terminal(phase):
        return {"status": "terminal", "phase": phase}

    command = _latest_command(connection, deployment_id, command_id)
    if command is None:
        if require_command:
            return {"status": "no_command"}
    elif command["status"] != "succeeded":
        return {"status": "not_succeeded", "command_status": command["status"]}
    elif not command["next_phase"] or command["next_phase"] != phase:
        return {
            "status": "checkpoint_not_advanced",
            "deployment_phase": phase,
            "command_next_phase": command["next_phase"],
        }

    spec = deployment["spec"] if isinstance(deployment["spec"], dict) else {}
    stored_state = deployment["state"] if isinstance(deployment["state"], dict) else {}
    evidence = stored_state.get("evidence")
    artifacts = stored_state.get("artifacts")
    if isinstance(artifacts, dict):
        evidence = {
            **(evidence if isinstance(evidence, dict) else {}),
            **artifacts,
        }
    try:
        plan = plan_next_step(
            deployment_id,
            phase,
            spec,
            evidence if isinstance(evidence, dict) else None,
        )
    except OrchestrationError as exc:
        return {"status": "invalid_plan", "error": str(exc)}
    if plan is None:
        return {"status": "terminal", "phase": phase}

    existing = connection.execute(
        """
        SELECT command_id, status FROM agent_control_commands
        WHERE idempotency_key = %s
        """,
        (plan.idempotency_key,),
    ).fetchone()
    if existing is not None:
        return {
            "status": "exists",
            "command_id": str(existing["command_id"]),
            "command_status": existing["status"],
            "action": plan.action,
        }

    agent_id = _eligible_agent(connection, deployment, plan.role)
    if agent_id is None:
        return {"status": "no_eligible_agent", "role": plan.role, "action": plan.action}

    command_id = uuid4()
    connection.execute(
        """
        INSERT INTO agent_control_commands (
            command_id, deployment_id, target_agent_id, required_role,
            action, idempotency_key, payload, from_phase, next_phase, status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'queued')
        ON CONFLICT (idempotency_key) DO NOTHING
        """,
        (
            command_id,
            deployment_id,
            agent_id,
            plan.role,
            plan.action,
            plan.idempotency_key,
            Jsonb(plan.payload),
            phase,
            plan.next_phase.value,
        ),
    )
    return {
        "status": "queued",
        "command_id": str(command_id),
        "role": plan.role,
        "action": plan.action,
        "from_phase": phase,
        "next_phase": plan.next_phase.value,
    }


def _has_active_command(
    connection: psycopg.Connection[Any],
    deployment_id: UUID,
) -> bool:
    row = connection.execute(
        """
        SELECT 1 FROM agent_control_commands
        WHERE deployment_id = %s AND status IN ('queued', 'leased')
        LIMIT 1
        """,
        (deployment_id,),
    ).fetchone()
    return row is not None


def requeue_retryable_commands(
    connection: psycopg.Connection[Any],
    limit: int = 100,
    max_attempts: int = MAX_COMMAND_ATTEMPTS,
) -> dict[str, Any]:
    """Requeue failed commands only when the Agent declared them retryable.

    A failed action with side effects is not automatically repeated: the Agent
    must say ``retryable: true``. The same command row is reused, its stale lease
    is cleared, and the attempt counter is capped so a broken deployment cannot
    spin forever.
    """

    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= 1000
        or not isinstance(max_attempts, int)
        or isinstance(max_attempts, bool)
        or not 1 <= max_attempts <= 10
    ):
        return {"status": "invalid_limit", "requeued": 0}
    rows = connection.execute(
        """
        SELECT command_id, attempts, result
        FROM agent_control_commands
        WHERE status = 'failed' AND attempts < %s
        ORDER BY updated_at
        LIMIT %s
        """,
        (max_attempts, limit),
    ).fetchall()
    requeued = 0
    skipped = 0
    for row in rows:
        result = row["result"] if isinstance(row["result"], dict) else {}
        if result.get("retryable") is not True:
            skipped += 1
            continue
        updated = connection.execute(
            """
            UPDATE agent_control_commands
            SET status = 'queued', attempts = attempts + 1,
                lease_id = NULL, lease_token_hash = NULL,
                lease_expires_at = NULL, issued_at = NULL,
                deadline = NULL, updated_at = NOW()
            WHERE command_id = %s AND status = 'failed'
              AND attempts = %s
            RETURNING command_id
            """,
            (row["command_id"], row["attempts"]),
        ).fetchone()
        if updated is not None:
            requeued += 1
        else:
            skipped += 1
    return {
        "status": "completed",
        "scanned": len(rows),
        "requeued": requeued,
        "skipped": skipped,
    }


def reconcile_deployments(
    connection: psycopg.Connection[Any],
    limit: int = 100,
) -> dict[str, Any]:
    """Re-plan every non-terminal deployment that has no active command.

    This is the recovery path for a result whose auto-advance was interrupted by
    a database error, for a deployment created before its Agent registered, and
    for an operator retry after a transient control-plane failure. It never
    advances a deployment that still owns a queued or leased command.
    """

    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
        return {"status": "invalid_limit"}
    retry = requeue_retryable_commands(connection, limit=limit)
    deployments = connection.execute(
        """
        SELECT deployment_id FROM agent_control_deployments
        WHERE phase <> 'completed'
        ORDER BY updated_at, created_at
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    summary = {
        "status": "completed",
        "scanned": 0,
        "queued": 0,
        "skipped": 0,
        "retry_requeued": retry["requeued"],
        "results": [],
    }
    for row in deployments:
        summary["scanned"] += 1
        deployment_id = row["deployment_id"]
        if _has_active_command(connection, deployment_id):
            result = {"status": "active_command", "deployment_id": str(deployment_id)}
        else:
            try:
                result = advance_after_command(
                    connection,
                    deployment_id,
                    require_command=False,
                )
            except (OrchestrationError, psycopg.Error) as exc:
                LOG.warning(
                    "Deployment reconciliation skipped for %s: %s",
                    deployment_id,
                    type(exc).__name__,
                )
                result = {
                    "status": "error",
                    "deployment_id": str(deployment_id),
                    "error": type(exc).__name__,
                }
            result["deployment_id"] = str(deployment_id)
        summary["results"].append(result)
        if result["status"] == "queued":
            summary["queued"] += 1
        else:
            summary["skipped"] += 1
    return summary


def advance_for_agent(
    connection: psycopg.Connection[Any],
    installation_id: UUID,
    cluster_id: UUID | None,
) -> list[dict[str, Any]]:
    """Retry pending progression for the deployments an Agent just joined."""

    if cluster_id is None:
        deployments = connection.execute(
            """
            SELECT deployment_id FROM agent_control_deployments
            WHERE installation_id = %s AND cluster_id IS NOT NULL
              AND phase <> 'completed'
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (installation_id,),
        ).fetchall()
    else:
        deployments = connection.execute(
            """
            SELECT deployment_id FROM agent_control_deployments
            WHERE installation_id = %s AND cluster_id = %s
              AND phase <> 'completed'
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (installation_id, cluster_id),
        ).fetchall()
    results = []
    for row in deployments:
        try:
            results.append(
                advance_after_command(
                    connection,
                    row["deployment_id"],
                    require_command=False,
                )
            )
        except (OrchestrationError, psycopg.Error) as exc:
            LOG.warning("Deployment retry after Agent registration skipped: %s", type(exc).__name__)
    return results
