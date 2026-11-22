"""The retention policy a deployment declares, and the files it is holding.

Two callers need the same answer: the scheduler turns the declaration into a
command, and the read-only view shows it. Keeping the mapping and the "which
snapshots a rollback still needs" query in one place means the two cannot drift
and a new policy key cannot be honoured by one and ignored by the other.
"""

from __future__ import annotations

from typing import Any

from core.models import AgentCommand, ClusterDeployment
from packages.agent_contracts.kubernetes_rollback import ACTION as ROLLBACK_ACTION

#: ``spec.kubernetes`` keys that carry retention policy, mapped to the request
#: fields they become. Unknown keys are ignored; the contract refuses bad values.
SPEC_POLICY_KEYS = {
    "directory": "etcd_backup_directory",
    "keep_count": "etcd_backup_keep_count",
    "keep_max_age_hours": "etcd_backup_keep_max_age_hours",
    "pre_upgrade_keep_max_age_hours": "etcd_backup_pre_upgrade_keep_max_age_hours",
    "max_age_hours": "etcd_backup_max_age_hours",
}

#: A rollback that ended this way no longer holds the snapshot it named.
ROLLBACK_ENDED = (
    AgentCommand.Status.FAILED,
    AgentCommand.Status.CANCELLED,
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
