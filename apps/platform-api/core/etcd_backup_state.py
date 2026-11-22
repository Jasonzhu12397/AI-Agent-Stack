"""What the platform can say about a cluster's etcd backups, without asking it.

The Target Agent owns the backup directory. The platform only knows what its
commands reported, so this module turns the newest reports into one answer a
view, a UI panel and a watchdog command all share. Keeping it out of the view
means the watchdog cannot drift from what an operator sees on screen.
"""

from __future__ import annotations

import datetime
from typing import Any

from core.etcd_backup_policy import protected_names, spec_policy
from core.models import AgentCommand, ClusterDeployment
from packages.agent_contracts import etcd_backup as contract

#: How many commands are kept as history for the panel.
HISTORY_LIMIT = 12


def backup_policy(deployment: ClusterDeployment) -> dict[str, Any]:
    """The backup policy this deployment declares, or the contract defaults."""

    kubernetes = (deployment.spec or {}).get("kubernetes") or {}
    payload = {
        "cluster_name": deployment.cluster_name,
        "execution_mode": "execute",
        "confirm_cluster_name": deployment.cluster_name,
    }
    payload.update(spec_policy(kubernetes))
    protect = protected_names(deployment)
    if protect:
        payload["protect"] = protect
    try:
        request = contract.validate_request(payload, cluster_name=deployment.cluster_name)
    except contract.EtcdBackupError as exc:
        return {
            "enabled": kubernetes.get("etcd_backup") is not False,
            "valid": False,
            "error": {"code": exc.code, "message": str(exc)},
        }
    return {
        "enabled": kubernetes.get("etcd_backup") is not False,
        "valid": True,
        "directory": request["directory"],
        "keep_count": request["keep_count"],
        "keep_max_age_hours": request["keep_max_age_hours"],
        "pre_upgrade_keep_max_age_hours": request["pre_upgrade_keep_max_age_hours"],
        "max_age_hours": request["max_age_hours"],
        "protect": list(request["protect"]),
    }


#: Spec keys a caller may set through the policy endpoint, mapped to the request
#: field they control. ``etcd_backup`` itself is the on/off switch.
POLICY_FIELDS = {
    "keep_count": "etcd_backup_keep_count",
    "keep_max_age_hours": "etcd_backup_keep_max_age_hours",
    "pre_upgrade_keep_max_age_hours": "etcd_backup_pre_upgrade_keep_max_age_hours",
    "max_age_hours": "etcd_backup_max_age_hours",
    "directory": "etcd_backup_directory",
}


def store_policy(deployment: ClusterDeployment, values: dict[str, Any]) -> dict[str, Any]:
    """Validate a policy change against the contract, then write it into the spec.

    The contract is the only authority: a value it refuses is refused here with
    the same code, so a UI cannot save a policy the executor would reject.
    """

    kubernetes = dict((deployment.spec or {}).get("kubernetes") or {})
    for field, key in POLICY_FIELDS.items():
        if field in values:
            kubernetes[key] = values[field]
    if "enabled" in values:
        kubernetes["etcd_backup"] = bool(values["enabled"])
    request_payload = {
        "cluster_name": deployment.cluster_name,
        "execution_mode": "execute",
        "confirm_cluster_name": deployment.cluster_name,
    }
    request_payload.update(spec_policy(kubernetes))
    protect = protected_names(deployment)
    if protect:
        request_payload["protect"] = protect
    contract.validate_request(request_payload, cluster_name=deployment.cluster_name)
    spec = dict(deployment.spec or {})
    spec["kubernetes"] = kubernetes
    deployment.spec = spec
    deployment.save(update_fields=["spec"])
    return kubernetes


def _entries_from(names: Any, cluster_name: str) -> list[dict[str, Any]]:
    """Rebuild contract entries from the file names a command reported."""

    if not isinstance(names, list):
        return []
    return contract.snapshot_entries(
        [{"name": str(name)} for name in names if isinstance(name, str)],
        cluster_name=cluster_name,
    )


def _command_summary(command: AgentCommand) -> dict[str, Any]:
    result = command.result if isinstance(command.result, dict) else {}
    backup = result.get("backup") if isinstance(result.get("backup"), dict) else None
    return {
        "command_id": str(command.id),
        "status": command.status,
        "created_at": command.created_at.isoformat() if command.created_at else None,
        "finished_at": command.finished_at.isoformat() if command.finished_at else None,
        "backup": backup,
        "pruned": result.get("pruned") or [],
        "count": result.get("count"),
        "failure_code": result.get("failure_code"),
        "error": command.error or None,
    }


def etcd_backup_state(
    deployment: ClusterDeployment, *, now: datetime.datetime | None = None
) -> dict[str, Any]:
    """Describe the newest usable backup and whether it is still fresh."""

    policy = backup_policy(deployment)
    commands = list(
        AgentCommand.objects.filter(
            deployment=deployment,
            command_type__in=(contract.ACTION, contract.STATUS_ACTION),
        ).order_by("-created_at")[:HISTORY_LIMIT]
    )
    runs = [command for command in commands if command.command_type == contract.ACTION]
    state: dict[str, Any] = {
        "policy": policy,
        "last_run": _command_summary(runs[0]) if runs else None,
        "history": [_command_summary(command) for command in commands],
        "newest": None,
        "newest_age_hours": None,
        "stale": True,
        "ok": False,
        "findings": [
            {
                "name": "backup.command",
                "ok": False,
                "code": "ETCD_BACKUP_NOT_RUN_YET",
                "detail": "the scheduler has not reported a backup for this cluster",
            }
        ],
    }
    if not policy.get("valid"):
        state["findings"] = [
            {
                "name": "backup.policy",
                "ok": False,
                "code": policy["error"]["code"],
                "detail": policy["error"]["message"],
            }
        ]
        return state
    if not runs:
        return state

    # Use the newest run that reported a directory listing: a failed run that
    # died before listing says nothing about what is on disk.
    reported = None
    for command in runs:
        result = command.result if isinstance(command.result, dict) else {}
        if isinstance(result.get("kept"), list) or isinstance(result.get("backup"), dict):
            reported = command
            break
    if reported is None:
        return state

    result = reported.result
    cluster_name = deployment.cluster_name
    entries = _entries_from(result.get("kept"), cluster_name)
    backup = result.get("backup")
    if not entries and isinstance(backup, dict) and backup.get("name"):
        entries = _entries_from([backup["name"]], cluster_name)
    report = contract.assess(
        entries,
        keep_count=policy["keep_count"],
        keep_max_age_hours=policy["keep_max_age_hours"],
        pre_upgrade_keep_max_age_hours=policy["pre_upgrade_keep_max_age_hours"],
        max_age_hours=policy["max_age_hours"],
        protect=tuple(policy["protect"]),
        now=now,
    )
    state.update(
        {
            "newest": report["newest"],
            "newest_age_hours": report["newest_age_hours"],
            "stale": any(
                finding["code"] == "ETCD_BACKUP_STALE" for finding in report["findings"]
            ),
            "ok": report["ok"],
            "count": report["count"],
            "prunable": report["prunable"],
            "pre_upgrade_count": report["pre_upgrade_count"],
            "pre_upgrade": report["pre_upgrade"],
            "findings": report["findings"],
        }
    )
    return state


