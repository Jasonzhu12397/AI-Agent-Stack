"""What the platform can say about a cluster's etcd backups, without asking it.

The Target Agent owns the backup directory. The platform only knows what its
commands reported, so this module turns the newest reports into one answer a
view, a UI panel and a watchdog command all share. Keeping it out of the view
means the watchdog cannot drift from what an operator sees on screen.
"""

from __future__ import annotations

import datetime
from typing import Any

from core.etcd_backup_policy import enabled_switch, protected_names, spec_policy
from core.models import AgentCommand, AuditEvent, ClusterDeployment
from packages.agent_contracts import etcd_backup as contract

#: How many commands are kept as history for the panel.
HISTORY_LIMIT = 12


def backup_policy(deployment: ClusterDeployment) -> dict[str, Any]:
    """The backup policy this deployment declares, or the contract defaults."""

    kubernetes = (deployment.spec or {}).get("kubernetes") or {}
    try:
        enabled = enabled_switch(kubernetes)
    except contract.EtcdBackupError as exc:
        return {
            "enabled": True,
            "valid": False,
            "error": {"code": exc.code, "message": str(exc)},
        }
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
        "enabled": True if enabled is None else enabled,
        "valid": True,
        "directory": request["directory"],
        "keep_count": request["keep_count"],
        "keep_max_age_hours": request["keep_max_age_hours"],
        "pre_upgrade_keep_max_age_hours": request["pre_upgrade_keep_max_age_hours"],
        "max_age_hours": request["max_age_hours"],
        "min_free_bytes": request["min_free_bytes"],
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
    "min_free_bytes": "etcd_backup_min_free_bytes",
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
        enabled = values["enabled"]
        if not isinstance(enabled, bool):
            # bool("false") is True: accepting a string here would silently
            # switch backups back on when an operator asked to switch them off.
            raise contract.EtcdBackupError(
                "ETCD_BACKUP_ENABLED_INVALID",
                "enabled must be true or false, not a string or a number",
            )
        kubernetes["etcd_backup"] = enabled
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


SCHEDULER_EVENT = "cluster.etcd_backup.scheduler.run"
#: The scheduler runs hourly; two silent hours is a scheduler that stopped, not
#: a scheduler between ticks.
DEFAULT_SCHEDULER_SILENCE_HOURS = 2

NOT_RUN_FINDING = {
    "name": "backup.command",
    "ok": False,
    "code": "ETCD_BACKUP_NOT_RUN_YET",
    "detail": "the scheduler has not reported a backup for this cluster",
}

#: A backup command that ended badly leaves the newest report on disk from an
#: earlier run. Without this finding the panel and the watchdog would both call
#: the cluster healthy on yesterday's snapshot while today's attempt has been
#: failing every hour, which is the one failure mode nobody would notice.
LAST_RUN_FAILED = "ETCD_BACKUP_LAST_RUN_FAILED"
#: A command queued for longer than this is not a slow backup, it is a command
#: no agent ever picked up: the lease was never claimed, or the Target Agent is
#: gone. Waiting longer would only delay the day the gap is noticed.
RUN_STUCK = "ETCD_BACKUP_RUN_STUCK"
STUCK_AFTER_HOURS = 6.0
UNFINISHED_STATUSES = frozenset({"queued", "running", "cancel_requested"})
FAILED_STATUSES = frozenset({"failed", "cancelled"})

#: An upgrade that reported its snapshot is the only place the platform learns
#: about a restore point before the next recurring backup lists the directory.
UPGRADE_ACTION = "cluster.kubernetes.version.upgrade"


def _reported_restore_point(
    deployment: ClusterDeployment, *, now: datetime.datetime | None = None
) -> dict[str, Any] | None:
    """The snapshot the newest settled upgrade says it took and verified.

    A pending upgrade is ignored: the platform may not offer a file a command
    never proved it wrote. A *failed* upgrade is not ignored, because a snapshot
    is taken before anything changes: an upgrade that died afterwards still left
    the exact file a rollback needs, and that is the moment it is needed.
    """

    command = (
        AgentCommand.objects.filter(
            deployment=deployment,
            command_type=UPGRADE_ACTION,
            status__in=(AgentCommand.Status.SUCCEEDED, AgentCommand.Status.FAILED),
        )
        .order_by("-created_at")
        .only("result")
        .first()
    )
    if command is None:
        return None
    result = command.result if isinstance(command.result, dict) else {}
    snapshot = result.get("etcd_snapshot")
    if not isinstance(snapshot, dict) or not snapshot.get("verified"):
        return None
    path = str(snapshot.get("path") or "").strip()
    name = path.rsplit("/", 1)[-1]
    entries = _entries_from([name], deployment.cluster_name)
    if not entries:
        return None
    point = contract.restore_point(entries, now=now)
    if not point:
        return None
    return {**point, "path": path, "listed": False}


def _with_location(
    point: dict[str, Any] | None,
    *,
    directory: str,
    listed: bool,
) -> dict[str, Any] | None:
    """One shape for a restore point, whichever command taught us about it."""

    if not point:
        return None
    # A command that saw the file knows where it is; rebuilding the path from
    # the policy directory would point a rollback at a file that is not there
    # when the policy directory has been changed since the snapshot was taken.
    path = str(point.get("path") or "").strip()
    if not path:
        path = f"{directory.rstrip('/')}/{point['name']}"
    return {**point, "path": path, "listed": listed}


def _listing_directory(command: AgentCommand, fallback: str) -> str:
    """Where the file a command listed actually lives."""

    payload = command.payload if isinstance(command.payload, dict) else {}
    directory = str(payload.get("directory") or "").strip()
    return directory or fallback


def _listing(result: dict[str, Any]) -> list[Any]:
    """The directory listing a command reported, whichever shape it used.

    A backup run reports what it kept; a read-only status command reports every
    entry it saw. The status answer is the better evidence, so it wins when both
    exist, and either is enough to place a restore point.
    """

    entries = result.get("entries")
    if isinstance(entries, list) and entries:
        return entries
    kept = result.get("kept")
    if isinstance(kept, list):
        return kept
    backup = result.get("backup")
    if isinstance(backup, dict) and backup.get("name"):
        return [backup["name"]]
    return []


def _scheduler_last_run(deployment: ClusterDeployment) -> datetime.datetime | None:
    """When the hourly scheduler last evaluated this cluster, on its own record.

    The heartbeat is written per deployment, so only a record that names this
    deployment counts. Older tenant-level rows (empty ``resource_id``) never
    match: they are tolerated as history, but they must not lend this cluster
    freshness they did not earn.
    """

    return (
        AuditEvent.objects.filter(
            tenant=deployment.tenant,
            event_type=SCHEDULER_EVENT,
            resource_type="etcd_backup_scheduler",
            resource_id=str(deployment.id),
        )
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )


def _entries_from(names: Any, cluster_name: str) -> list[dict[str, Any]]:
    """Rebuild contract entries from the file names a command reported."""

    if not isinstance(names, list):
        return []
    items = [
        item if isinstance(item, dict) else {"name": str(item)} for item in names
    ]
    return contract.snapshot_entries(items, cluster_name=cluster_name)


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
    # Read-only checks are cheap and an operator may press "check now" a dozen
    # times in an hour. Asking the window above for the newest backup run would
    # let those checks push the run out of it and turn "backed up an hour ago"
    # into "never backed up", so the runs are asked for separately.
    runs = list(
        AgentCommand.objects.filter(
            deployment=deployment,
            command_type=contract.ACTION,
        ).order_by("-created_at")[:HISTORY_LIMIT]
    )
    scheduler_at = _scheduler_last_run(deployment)
    scheduler_age = (
        (now or datetime.datetime.now(datetime.UTC)) - scheduler_at
    ).total_seconds() / 3600.0 if scheduler_at else None
    state: dict[str, Any] = {
        "policy": policy,
        "restore_point": None,
        "scheduler_last_run": scheduler_at.isoformat() if scheduler_at else None,
        "scheduler_age_hours": (
            round(scheduler_age, 3) if scheduler_age is not None else None
        ),
        "last_run": _command_summary(runs[0]) if runs else None,
        "history": [_command_summary(command) for command in commands],
        "newest": None,
        "newest_age_hours": None,
        "stale": True,
        "ok": False,
        "findings": [dict(NOT_RUN_FINDING)],
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
    if policy.get("enabled") is False:
        # Switched off on purpose is not a broken backup: the panel says so
        # instead of painting "never backed up" over an intentional choice.
        state["enabled"] = False
        state["ok"] = False
        state["stale"] = True
        state["findings"] = []
        state["restore_point"] = _reported_restore_point(deployment, now=now)
        return state
    state["enabled"] = True
    if not runs:
        # No backup command has reported yet, but a read-only check may still
        # have listed the directory: keep the "not run yet" finding and let the
        # rest of this function describe what the check saw.
        state["not_run_yet"] = True

    # Use the newest report that listed the directory: a failed run that died
    # before listing says nothing about what is on disk, and a read-only status
    # command that just read it says more than an older backup run.
    reported = None
    for command in commands:
        result = command.result if isinstance(command.result, dict) else {}
        if _listing(result):
            reported = command
            break
    if reported is None:
        state["restore_point"] = _reported_restore_point(deployment, now=now)
        return state

    result = reported.result
    cluster_name = deployment.cluster_name
    entries = _entries_from(_listing(result), cluster_name)
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
            "reported_by": reported.command_type,
            "reported_at": (
                reported.finished_at.isoformat() if reported.finished_at else None
            ),
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
            "restore_point": _with_location(
                report["restore_point"] or _reported_restore_point(deployment, now=now),
                # The listing came from the directory the command was given, so
                # that is where its files are, even if the policy has since
                # been pointed somewhere else.
                directory=_listing_directory(reported, policy["directory"]),
                listed=bool(report["restore_point"]),
            ),
            "findings": report["findings"],
        }
    )
    newest_run = runs[0] if runs else None
    # Asked of the table rather than of the history window: a command dropped
    # twenty hours ago must still be found after fourteen later runs pushed it
    # out of the twelve most recent ones.
    current = now or datetime.datetime.now(datetime.UTC)
    stuck_cutoff = current - datetime.timedelta(hours=STUCK_AFTER_HOURS)
    stuck_run = (
        AgentCommand.objects.filter(
            deployment=deployment,
            command_type=contract.ACTION,
            status__in=UNFINISHED_STATUSES,
            created_at__lt=stuck_cutoff,
        )
        .order_by("created_at")
        .first()
    )
    waited = (
        (current - stuck_run.created_at).total_seconds() / 3600.0
        if stuck_run is not None
        else 0.0
    )
    if stuck_run is not None:
        state["findings"] = [
            *state["findings"],
            {
                "name": "backup.queue",
                "ok": False,
                "code": RUN_STUCK,
                "detail": f"queued {waited:.1f}h ago and no agent has finished it",
            },
        ]
        state["ok"] = False
        state["run_stuck_hours"] = round(waited, 3)
    newest_result = (
        newest_run.result if newest_run and isinstance(newest_run.result, dict) else {}
    )
    if newest_run is not None and (
        newest_run.status in FAILED_STATUSES or newest_result.get("ok") is False
    ):
        code = newest_result.get("failure_code") or LAST_RUN_FAILED
        detail = newest_run.error or newest_result.get("detail") or ""
        state["findings"] = [
            *state["findings"],
            {
                "name": "backup.last_run",
                "ok": False,
                "code": code,
                "detail": detail or "the newest backup command did not finish",
            },
        ]
        state["ok"] = False
        state["last_run_failed"] = code
    if state.get("not_run_yet"):
        # A read-only check can describe the disk, but it cannot replace the
        # fact that no scheduled backup has ever reported: freshness is
        # claimed by runs, never by a manual look.
        state["findings"] = [*report["findings"], dict(NOT_RUN_FINDING)]
        state["ok"] = False
    return state


