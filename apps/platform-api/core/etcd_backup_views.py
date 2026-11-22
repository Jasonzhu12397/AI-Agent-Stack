"""Read-only view of a Target cluster's etcd backups.

The Target Agent owns the backup directory; the platform only knows what
commands reported. This module turns the newest command results into a single
answer a UI or a monitoring check can poll, and it reuses the contract's
retention validation instead of inventing a second policy dialect.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.cluster_deployment_views import _can_edit_project
from core.models import AgentCommand, ClusterDeployment, ProjectMembership
from packages.agent_contracts import etcd_backup as contract

HISTORY_LIMIT = 12


def _policy(deployment: ClusterDeployment) -> dict[str, Any]:
    """The backup policy this deployment declares, or the contract defaults."""

    kubernetes = (deployment.spec or {}).get("kubernetes") or {}
    payload = {"cluster_name": deployment.cluster_name}
    if kubernetes.get("etcd_backup_directory"):
        payload["directory"] = kubernetes["etcd_backup_directory"]
    if kubernetes.get("etcd_backup_keep_count") is not None:
        payload["keep_count"] = kubernetes["etcd_backup_keep_count"]
    if kubernetes.get("etcd_backup_keep_max_age_hours") is not None:
        payload["keep_max_age_hours"] = kubernetes["etcd_backup_keep_max_age_hours"]
    if kubernetes.get("etcd_backup_max_age_hours") is not None:
        payload["max_age_hours"] = kubernetes["etcd_backup_max_age_hours"]
    payload["execution_mode"] = "execute"
    payload["confirm_cluster_name"] = deployment.cluster_name
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
        "max_age_hours": request["max_age_hours"],
    }


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

    policy = _policy(deployment)
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
        max_age_hours=policy["max_age_hours"],
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
            "findings": report["findings"],
        }
    )
    return state


def _deployment_or_error(request, deployment_id):
    deployment = get_object_or_404(
        ClusterDeployment.objects.filter(tenant__memberships__user=request.user),
        pk=deployment_id,
    )
    if not _can_edit_project(request.user, deployment.tenant, deployment.project):
        if not ProjectMembership.objects.filter(
            project=deployment.project, user=request.user
        ).exists():
            return None, Response({"detail": "Project membership required."}, status=403)
    return deployment, None


class ClusterDeploymentEtcdBackupView(APIView):
    """What backups exist for this cluster, and how old the newest one is."""

    permission_classes = [IsAuthenticated]

    def get(self, request, deployment_id):
        deployment, error = _deployment_or_error(request, deployment_id)
        if error is not None:
            return error
        return Response(
            {
                "deployment_id": str(deployment.id),
                "cluster_name": deployment.cluster_name,
                **etcd_backup_state(deployment),
            }
        )
