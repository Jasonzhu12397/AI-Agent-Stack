"""One place that turns a deployment into a queued backup command.

Two callers queue backups: the hourly scheduler command, and an operator who
wants one now. They must agree on the payload, on what makes a cluster
unqueueable, and above all on the idempotency key: if the scheduler already
queued this hour, pressing "back up now" should hand back that command rather
than take a second snapshot of the same data.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from django.db import transaction

from core.etcd_backup_policy import enabled_switch, protected_names, spec_policy
from core.models import AgentCommand, AgentNode, ClusterDeployment
from packages.agent_contracts.etcd_backup import (
    ACTION,
    STATUS_ACTION,
    EtcdBackupError,
    validate_request,
)

#: A command in one of these states will never produce a snapshot. Reusing it
#: would tell an operator "already queued" during the one hour a backup
#: actually matters, so the next attempt gets a new key instead.
DEAD_STATUSES = frozenset({"failed", "cancelled"})

#: Why this deployment may not be backed up right now, as a stable code. Empty
#: when it may.
NO_TARGET = "no_target_agent"
DISABLED = "disabled"


def hour_bucket(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%dT%H")


def payload_hash(command_type: str, payload: dict) -> str:
    encoded = json.dumps(
        {"command_type": command_type, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def status_bucket(now: datetime | None = None) -> str:
    """A ten-minute bucket for read-only checks.

    A status command only reads the directory, but running it for every click
    would still put a pod exec on the control plane each time. Ten minutes is
    short enough for an operator watching a run and long enough to absorb
    double clicks.
    """

    current = now or datetime.now(UTC)
    return f"{current.strftime('%Y%m%dT%H')}{current.minute // 10}"


def unqueueable_reason(deployment: ClusterDeployment) -> str:
    """Why this deployment cannot back up, or "" when it can."""

    spec = deployment.spec if isinstance(deployment.spec, dict) else {}
    kubernetes = spec.get("kubernetes")
    try:
        switch = enabled_switch(kubernetes)
    except EtcdBackupError as exc:
        # Never guess whether a hand-edited value means on or off.
        return exc.code
    if switch is False:
        return DISABLED
    node = deployment.target_node
    if node is None or node.node_type != AgentNode.NodeType.TARGET_CLUSTER:
        return NO_TARGET
    return ""


def backup_payload(
    deployment: ClusterDeployment, overrides: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The request a backup run would receive, validated by the contract."""

    spec = deployment.spec if isinstance(deployment.spec, dict) else {}
    kubernetes = spec.get("kubernetes")
    payload: dict[str, Any] = {
        "cluster_name": deployment.cluster_name,
        "execution_mode": "execute",
        "confirm_cluster_name": deployment.cluster_name,
    }
    payload.update(spec_policy(kubernetes))
    protect = protected_names(deployment)
    if protect:
        payload["protect"] = protect
    for field, value in (overrides or {}).items():
        # Zero is a real policy (min_free_bytes=0 means measure only), so the
        # test is for absence, not for truthiness; an unset CLI option arrives
        # as None or as an empty string and means "leave the policy alone".
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        payload[field] = value
    request = validate_request(payload)
    if not request["protect"]:
        # The contract always reports the protect set, empty or not. An empty
        # key in the command payload would change the plan hash that
        # idempotency compares, so it is dropped when there is nothing to
        # protect.
        request.pop("protect")
    return request


def _queue_with_retry(
    tenant: Any, key: str, defaults: dict[str, Any]
) -> tuple[AgentCommand, bool]:
    """Get or create this bucket's command, retrying when the bucket's is dead.

    Both queues share the rule, because both have the same failure: a command
    that ended failed or cancelled must not answer "already queued" to the
    operator who is trying to fix exactly that.
    """

    with transaction.atomic():
        command, created = AgentCommand.objects.get_or_create(
            tenant=tenant, idempotency_key=key, defaults=defaults
        )
        if created or command.status not in DEAD_STATUSES:
            return command, created
        attempts = list(
            AgentCommand.objects.filter(
                tenant=tenant, idempotency_key__startswith=f"{key}:retry:"
            ).order_by("idempotency_key")
        )
        if attempts and attempts[-1].status not in DEAD_STATUSES:
            # A live retry already covers this bucket: return it, do not stack.
            return attempts[-1], False
        return AgentCommand.objects.get_or_create(
            tenant=tenant,
            idempotency_key=f"{key}:retry:{len(attempts) + 1}",
            defaults=defaults,
        )


def queue_status(
    deployment: ClusterDeployment,
    *,
    now: datetime | None = None,
    requested_by: Any = None,
) -> tuple[AgentCommand | None, bool, str]:
    """Queue a read-only check of the backup directory.

    No approval and no lease: the command reads files and reports, so it is
    always safe to run and it is the only way to learn the truth about disk
    between two backup runs.
    """

    reason = unqueueable_reason(deployment)
    if reason:
        return None, False, reason
    try:
        payload = backup_payload(deployment)
    except EtcdBackupError as exc:
        return None, False, exc.code
    key = f"etcd-backup-status:{deployment.id}:{status_bucket(now)}"
    command, created = _queue_with_retry(
        deployment.tenant,
        key,
        {
            "project": deployment.project,
            "node": deployment.target_node,
            "requested_by": requested_by or deployment.requested_by,
            "command_type": STATUS_ACTION,
            "payload": payload,
            "status": AgentCommand.Status.QUEUED,
            "plan_hash": payload_hash(STATUS_ACTION, payload),
            "deployment": deployment,
        },
    )
    return command, created, ""


def queue_backup(
    deployment: ClusterDeployment,
    *,
    now: datetime | None = None,
    overrides: dict[str, Any] | None = None,
    requested_by: Any = None,
) -> tuple[AgentCommand | None, bool, str]:
    """Queue one backup for this deployment, at most once per hour bucket.

    Returns the command, whether this call created it, and the reason nothing
    was queued. A backup needs no approval: it writes a file and prunes by
    policy, and a backup that waits for a human is not a backup.
    """

    reason = unqueueable_reason(deployment)
    if reason:
        return None, False, reason
    node = deployment.target_node
    try:
        payload = backup_payload(deployment, overrides)
    except EtcdBackupError as exc:
        return None, False, exc.code
    key = f"etcd-backup:{deployment.id}:{hour_bucket(now)}"
    command, created = _queue_with_retry(
        deployment.tenant,
        key,
        {
            "project": deployment.project,
            "node": node,
            "requested_by": requested_by or deployment.requested_by,
            "command_type": ACTION,
            "payload": payload,
            "status": AgentCommand.Status.QUEUED,
            "plan_hash": payload_hash(ACTION, payload),
            "deployment": deployment,
        },
    )
    return command, created, ""
