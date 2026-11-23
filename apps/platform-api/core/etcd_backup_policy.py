"""The retention policy a deployment declares, and the files it is holding.

Two callers need the same answer: the scheduler turns the declaration into a
command, and the read-only view shows it. Keeping the mapping and the "which
snapshots a rollback still needs" query in one place means the two cannot drift
and a new policy key cannot be honoured by one and ignored by the other.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from core.audit import record_audit_event
from core.models import AgentCommand, AuditEvent, ClusterDeployment
from packages.agent_contracts.etcd_backup import (
    ACTION,
    STATUS_ACTION,
    EtcdBackupError,
    candidate_directories,
)
from packages.agent_contracts.kubernetes_rollback import ACTION as ROLLBACK_ACTION

UPGRADE_ACTION = "cluster.kubernetes.version.upgrade"
POLICY_AUDIT_EVENT = "cluster.etcd_backup.policy.updated"

#: Audit event written once, when an approved-but-unclaimed rollback leaves the
#: live set and releases the snapshot it named.
ROLLBACK_PROTECTION_RELEASED_EVENT = (
    "cluster.kubernetes.rollback.protection.released"
)

#: ``spec.kubernetes`` key that overrides how long an approved rollback that no
#: agent has claimed keeps protecting its snapshot, in minutes.
ROLLBACK_PROTECTION_SPEC_KEY = "etcd_backup_rollback_protection_minutes"

#: Default protection horizon, in minutes: a rollback that was approved but
#: never leased within this window can never run against a live cluster, so the
#: snapshot must not be held indefinitely. Both the sweep and the lease
#: deadline read the same value through ``rollback_protection_horizon_minutes``.
DEFAULT_ROLLBACK_PROTECTION_HORIZON_MINUTES = 24 * 60

#: ``spec.kubernetes`` keys that carry retention policy, mapped to the request
#: fields they become. Unknown keys are ignored; the contract refuses bad values.
SPEC_POLICY_KEYS = {
    "directory": "etcd_backup_directory",
    "keep_count": "etcd_backup_keep_count",
    "keep_max_age_hours": "etcd_backup_keep_max_age_hours",
    "pre_upgrade_keep_max_age_hours": "etcd_backup_pre_upgrade_keep_max_age_hours",
    "max_age_hours": "etcd_backup_max_age_hours",
    "min_free_bytes": "etcd_backup_min_free_bytes",
}

#: A rollback that ended this way no longer holds the snapshot it named: it
#: either failed (the file is still usable, but nothing is waiting on it) or it
#: succeeded, in which case that snapshot has already done its job and must not
#: be kept forever by a protection nobody releases. ``expired`` is the horizon
#: terminal state: an approved command no agent ever claimed, released by the
#: sweep below.
ROLLBACK_ENDED = (
    AgentCommand.Status.SUCCEEDED,
    AgentCommand.Status.FAILED,
    AgentCommand.Status.CANCELLED,
    AgentCommand.Status.EXPIRED,
)


def rollback_protection_horizon_minutes(deployment: ClusterDeployment) -> int:
    """How long an approved, unclaimed rollback keeps protecting its snapshot.

    The value comes from one policy source so the sweep and the protection
    deadline cannot drift apart. A deployment may override the default through
    ``spec.kubernetes.etcd_backup_rollback_protection_minutes``; a missing,
    non-integral or non-positive value falls back to the default instead of
    disabling protection or breaking the retention pass.
    """

    kubernetes = (deployment.spec or {}).get("kubernetes")
    raw = kubernetes.get(ROLLBACK_PROTECTION_SPEC_KEY) if isinstance(kubernetes, dict) else None
    if isinstance(raw, bool):
        return DEFAULT_ROLLBACK_PROTECTION_HORIZON_MINUTES
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw.isdigit():
            return DEFAULT_ROLLBACK_PROTECTION_HORIZON_MINUTES
        raw = int(raw)
    if isinstance(raw, int) and raw > 0:
        return raw
    return DEFAULT_ROLLBACK_PROTECTION_HORIZON_MINUTES


def enabled_switch(kubernetes: Any) -> bool | None:
    """The on/off switch a spec declares, refusing values that are not a bool.

    A spec edited by hand can carry "false", or 0, or "no". Interpreting those
    would need a guess, and either guess is wrong half the time: backups either
    run when they were switched off or stop when they were left on. The caller
    is told the value is unusable so it can say so instead of guessing.
    """

    if not isinstance(kubernetes, dict):
        return None
    if "etcd_backup" not in kubernetes:
        return None
    value = kubernetes["etcd_backup"]
    if isinstance(value, bool):
        return value
    raise EtcdBackupError(
        "ETCD_BACKUP_ENABLED_INVALID",
        "spec.kubernetes.etcd_backup must be true or false",
    )


def spec_policy(kubernetes: Any) -> dict[str, Any]:
    """Read the retention policy a deployment declares, ignoring empty values."""

    if not isinstance(kubernetes, dict):
        return {}
    policy = {}
    for field, key in SPEC_POLICY_KEYS.items():
        value = kubernetes.get(key)
        if value is None or value == "":
            continue
        policy[field] = value
    return policy


def policy_directory(deployment: ClusterDeployment) -> str:
    """The snapshot directory this deployment declares, or "".

    The upgrade writes the snapshot an operator rolls back to. If it writes to a
    different directory than the one the recurring backup prunes, retention can
    never see the file: it would be both impossible to prune and impossible to
    find when it is needed. One deployment therefore has one directory.
    """

    directory = spec_policy((deployment.spec or {}).get("kubernetes")).get("directory")
    return str(directory or "").strip()


def known_directories(deployment: ClusterDeployment) -> dict[str, Any]:
    """Locations this cluster's snapshots may occupy, derived from its records.

    After a policy directory rotation, snapshots left in the retired directory
    are still this cluster's files. The candidate set therefore unions the
    current spec directory, directories past backup/status commands reported,
    the parent directory of an upgrade-reported snapshot, and directories the
    policy audit event recorded. The contract validates and deduplicates them;
    anything invalid comes back flagged instead of being dropped quietly.
    """

    sources: list[str] = []
    declared = spec_policy((deployment.spec or {}).get("kubernetes")).get("directory")
    if declared:
        sources.append(str(declared))

    history = AgentCommand.objects.filter(
        deployment=deployment,
        command_type__in=(ACTION, STATUS_ACTION),
    ).only("payload")
    for command in history:
        payload = command.payload if isinstance(command.payload, dict) else {}
        directory = payload.get("directory")
        if isinstance(directory, str) and directory.strip():
            sources.append(directory)

    upgrades = AgentCommand.objects.filter(
        deployment=deployment, command_type=UPGRADE_ACTION
    ).only("result")
    for command in upgrades:
        result = command.result if isinstance(command.result, dict) else {}
        snapshot = result.get("etcd_snapshot")
        if not isinstance(snapshot, dict):
            continue
        path = str(snapshot.get("path") or "").strip()
        if path:
            sources.append(path.rsplit("/", 1)[0])

    events = AuditEvent.objects.filter(
        tenant=deployment.tenant,
        event_type=POLICY_AUDIT_EVENT,
        resource_type="cluster_deployment",
        resource_id=str(deployment.id),
    ).only("metadata")
    for event in events:
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        policy = metadata.get("policy")
        if not isinstance(policy, dict):
            continue
        directory = policy.get("etcd_backup_directory")
        if isinstance(directory, str) and directory.strip():
            sources.append(directory)

    return candidate_directories(sources)


def protected_names(deployment: ClusterDeployment) -> list[str]:
    """Snapshot files a pending or completed rollback of this cluster still needs.

    Retention is allowed to delete old files; it is not allowed to delete the
    one an operator is about to restore from.
    """

    names = set()
    commands = AgentCommand.objects.filter(
        deployment=deployment, command_type=ROLLBACK_ACTION
    ).exclude(status__in=ROLLBACK_ENDED)
    for command in commands.only("payload"):
        payload = command.payload if isinstance(command.payload, dict) else {}
        path = str(payload.get("snapshot_path") or "").strip()
        if not path:
            continue
        names.add(path.rsplit("/", 1)[-1])
    return sorted(names)


def refresh_command_protect(command: AgentCommand) -> None:
    """Recompute the ``protect`` set on a queued backup at claim time.

    The payload frozen at enqueue cannot know about a rollback created later,
    so the poll path refreshes protect from live command state before dispatch.
    Every other payload key (the candidate ``directories`` set included) is
    preserved; only protect is recalculated. A command without a deployment link
    has nothing deployment-scoped to protect, so it is left untouched.
    """

    if command.deployment_id is None or not isinstance(command.payload, dict):
        return
    protect = protected_names(command.deployment)
    payload = dict(command.payload)
    if protect:
        payload["protect"] = protect
    else:
        payload.pop("protect", None)
    command.payload = payload


def release_expired_rollback_protection(
    now: Any = None,
) -> list[UUID]:
    """Move approved-but-unclaimed rollbacks past their horizon to ``expired``.

    A rollback that was approved and queued but never leased by an agent — the
    agent is permanently offline, so it can never run — must not keep holding
    its snapshot forever. Each such command past its deployment's protection
    horizon transitions to the terminal ``expired`` state, releases the lease
    fields, and writes one audit event. Running or claimed commands are not
    touched, and neither are commands still inside the horizon.

    The pass is idempotent: rows are locked with ``select_for_update``, the
    status is re-checked under the lock, and only a transition writes the audit
    event, so a repeated sweep neither migrates a command twice nor duplicates
    the event. Returns the ids of the commands released in this pass.
    """

    now = now or timezone.now()
    released: list[UUID] = []

    with transaction.atomic():
        candidates = (
            AgentCommand.objects.select_for_update(skip_locked=True)
            .filter(
                command_type=ROLLBACK_ACTION,
                status=AgentCommand.Status.QUEUED,
                approved_at__isnull=False,
                lease_token__isnull=True,
                deployment__isnull=False,
            )
            .select_related("deployment")
            .order_by("created_at", "id")
        )
        for command in candidates:
            deployment = command.deployment
            horizon = rollback_protection_horizon_minutes(deployment)
            deadline = command.approved_at + timedelta(minutes=horizon)
            if deadline > now:
                continue

            # Re-read under the lock: the command may have been claimed or
            # settled between the snapshot and the lock.
            command.refresh_from_db()
            if (
                command.status != AgentCommand.Status.QUEUED
                or command.approved_at is None
                or command.lease_token is not None
            ):
                continue

            payload = command.payload if isinstance(command.payload, dict) else {}
            snapshot_path = str(payload.get("snapshot_path") or "").strip()

            command.status = AgentCommand.Status.EXPIRED
            command.finished_at = now
            command.lease_token = None
            command.lease_expires_at = None
            command.error = {
                "code": "ROLLBACK_PROTECTION_LEASE_EXPIRED",
                "retryable": False,
            }
            command.save(
                update_fields=[
                    "status",
                    "error",
                    "finished_at",
                    "lease_token",
                    "lease_expires_at",
                    "updated_at",
                ]
            )

            record_audit_event(
                None,
                command.tenant,
                ROLLBACK_PROTECTION_RELEASED_EVENT,
                "agent_command",
                str(command.id),
                project=command.project,
                outcome=AuditEvent.Outcome.SUCCESS,
                metadata={
                    "deployment_id": str(deployment.id),
                    "command_type": command.command_type,
                    "snapshot_path": snapshot_path,
                    "approved_at": command.approved_at.isoformat(),
                    "protection_horizon_minutes": horizon,
                },
            )
            released.append(command.id)

    return released
