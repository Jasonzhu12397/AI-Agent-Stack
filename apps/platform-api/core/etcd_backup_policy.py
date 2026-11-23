"""The retention policy a deployment declares, and the files it is holding.

Two callers need the same answer: the scheduler turns the declaration into a
command, and the read-only view shows it. Keeping the mapping and the "which
snapshots a rollback still needs" query in one place means the two cannot drift
and a new policy key cannot be honoured by one and ignored by the other.
"""

from __future__ import annotations

from typing import Any

from core.models import AgentCommand, ClusterDeployment
from packages.agent_contracts.etcd_backup import EtcdBackupError
from packages.agent_contracts.kubernetes_rollback import ACTION as ROLLBACK_ACTION

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
#: be kept forever by a protection nobody releases.
ROLLBACK_ENDED = (
    AgentCommand.Status.SUCCEEDED,
    AgentCommand.Status.FAILED,
    AgentCommand.Status.CANCELLED,
)


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
