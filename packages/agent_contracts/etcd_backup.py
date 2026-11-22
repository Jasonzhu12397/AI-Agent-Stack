"""Periodic etcd snapshots with a retention policy the cluster can defend.

Before this contract the only snapshot in the product was the one an upgrade
took minutes before touching the control plane. That is a rollback aid, not a
backup: a lost or corrupted data directory two weeks after an install had
nothing to restore from. This module describes the recurring backup job and
decides, deterministically, which old files may be deleted.

Two rules keep the deletion side safe:

* a file is pruned only after a newer, verified snapshot exists — the executor
  writes into ``*.partial`` and renames only after ``snapshot status`` passes,
  so a half-written file never counts as a backup;
* the newest file, anything younger than ``min_age_hours`` and anything an
  approved rollback references are never pruned, whatever the count says.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from packages.agent_contracts.kubernetes_rollback import snapshot_pod_path_problem

ACTION = "cluster.kubernetes.backup.run"
STATUS_ACTION = "cluster.kubernetes.backup.status"

EXECUTION_MODES: tuple[str, ...] = ("simulation", "execute")
PAYLOAD_KEYS = frozenset(
    {
        "cluster_name",
        "directory",
        "keep_count",
        "keep_max_age_hours",
        "max_age_hours",
        "execution_mode",
        "confirm_cluster_name",
    }
)

#: Recurring jobs run without a human in the loop, so the floor is the schedule,
#: not an approval: an hourly job with fewer than two kept files could delete the
#: only copy that survived a failure.
DEFAULT_DIRECTORY = "/var/lib/etcd/backups"
MIN_KEEP_COUNT = 2
MAX_KEEP_COUNT = 168
MAX_KEEP_MAX_AGE_HOURS = 8760
DEFAULT_MAX_AGE_HOURS = 26
#: A snapshot written minutes ago must survive the same run that took it.
MIN_AGE_HOURS = 1
MAX_STALE_AGE_HOURS = 8760

_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
_DIRECTORY = re.compile(r"^/var/lib/etcd[A-Za-z0-9._/-]*$")
_FILE = re.compile(r"^(?P<cluster>[a-z0-9][-a-z0-9.]*)-(?P<stamp>\d{8}T\d{6}Z)\.db$")


class EtcdBackupError(ValueError):
    """A backup request that will not be executed."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _int(value: Any, *, code: str, low: int, high: int, default: int | None) -> int:
    if value in (None, "") and default is not None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise EtcdBackupError(code, f"must be an integer between {low} and {high}")
    return value


def validate_request(
    payload: Any, *, cluster_name: str | None = None
) -> dict[str, Any]:
    """Normalize a backup job request, or refuse it with a named code."""

    if not isinstance(payload, Mapping):
        raise EtcdBackupError("ETCD_BACKUP_PAYLOAD_INVALID", "payload must be a JSON object")
    unknown = sorted(set(payload) - PAYLOAD_KEYS)
    if unknown:
        raise EtcdBackupError(
            "ETCD_BACKUP_PAYLOAD_INVALID",
            f"unknown payload keys: {', '.join(unknown)}",
        )
    cluster = str(payload.get("cluster_name") or "").strip()
    if not cluster or not _NAME.fullmatch(cluster) or len(cluster) > 63:
        raise EtcdBackupError(
            "ETCD_BACKUP_CLUSTER_NAME_INVALID", "cluster_name must be a DNS label"
        )
    expected = (cluster_name or os.environ.get("TARGET_CLUSTER_NAME") or "").strip()
    if expected and expected != cluster:
        raise EtcdBackupError(
            "ETCD_BACKUP_CLUSTER_NAME_MISMATCH",
            f"this agent serves {expected}, not {cluster}",
        )
    mode = str(payload.get("execution_mode") or "simulation").strip()
    if mode not in EXECUTION_MODES:
        raise EtcdBackupError(
            "ETCD_BACKUP_EXECUTION_MODE_INVALID",
            f"execution_mode must be one of {', '.join(EXECUTION_MODES)}",
        )
    if mode == "execute":
        confirmed = str(payload.get("confirm_cluster_name") or "").strip()
        if confirmed != cluster:
            raise EtcdBackupError(
                "ETCD_BACKUP_CONFIRMATION_MISMATCH",
                "execute needs confirm_cluster_name equal to cluster_name",
            )
    directory = str(payload.get("directory") or DEFAULT_DIRECTORY).strip()
    if not _DIRECTORY.fullmatch(directory) or "//" in directory or "/../" in directory:
        raise EtcdBackupError(
            "ETCD_BACKUP_DIRECTORY_INVALID",
            "directory must be an absolute path under /var/lib/etcd without traversal",
        )
    problem = snapshot_pod_path_problem(directory.rstrip("/") + "/placeholder.db")
    if problem:
        raise EtcdBackupError("ETCD_BACKUP_DIRECTORY_INVALID", problem)
    keep_count = _int(
        payload.get("keep_count"),
        code="ETCD_BACKUP_RETENTION_INVALID",
        low=MIN_KEEP_COUNT,
        high=MAX_KEEP_COUNT,
        default=MIN_KEEP_COUNT,
    )
    keep_age = _int(
        payload.get("keep_max_age_hours"),
        code="ETCD_BACKUP_RETENTION_INVALID",
        low=1,
        high=MAX_KEEP_MAX_AGE_HOURS,
        default=MAX_KEEP_MAX_AGE_HOURS,
    )
    max_age = _int(
        payload.get("max_age_hours"),
        code="ETCD_BACKUP_STALENESS_INVALID",
        low=1,
        high=MAX_STALE_AGE_HOURS,
        default=DEFAULT_MAX_AGE_HOURS,
    )
    return {
        "cluster_name": cluster,
        "directory": directory.rstrip("/"),
        "keep_count": keep_count,
        "keep_max_age_hours": keep_age,
        "max_age_hours": max_age,
        "execution_mode": mode,
        "confirm_cluster_name": cluster if mode == "execute" else "",
    }


def backup_filename(cluster_name: str, *, now: datetime | None = None) -> str:
    """Return ``<cluster>-<UTC stamp>.db`` for one run."""

    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{cluster_name}-{stamp}.db"


def partial_name(filename: str) -> str:
    """The staging name a run writes before it has verified the snapshot."""

    return f"{filename}.partial"


def parse_backup_name(name: str, *, cluster_name: str = "") -> dict[str, str] | None:
    """Return ``{name, cluster, stamp, taken_at}`` for a backup file name."""

    match = _FILE.fullmatch(name)
    if not match:
        return None
    if cluster_name and match.group("cluster") != cluster_name:
        return None
    try:
        taken_at = datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%SZ").replace(
            tzinfo=UTC
        )
    except ValueError:
        return None
    return {
        "name": name,
        "cluster": match.group("cluster"),
        "stamp": match.group("stamp"),
        "taken_at": taken_at.isoformat(),
    }


def _as_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def snapshot_entries(entries: Any, *, cluster_name: str = "") -> list[dict[str, Any]]:
    """Normalize a directory listing into ordered backup entries.

    The executor reads the directory inside the etcd pod; this keeps only the
    files that belong to this cluster and that parse as a backup, so a stray
    ``etcd.db`` or another cluster's snapshot can never enter the retention
    decision.
    """

    if not isinstance(entries, list):
        return []
    normalized = []
    for item in entries:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        parsed = parse_backup_name(name, cluster_name=cluster_name)
        if not parsed:
            continue
        taken_at = _as_time(parsed["taken_at"])
        if taken_at is None:
            continue
        size = item.get("size_bytes")
        normalized.append(
            {
                **parsed,
                "size_bytes": size if isinstance(size, int) and size >= 0 else None,
                "mtime": parsed["taken_at"],
                "_taken_at": taken_at,
            }
        )
    normalized.sort(key=lambda entry: entry["_taken_at"], reverse=True)
    return normalized


def public_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip internal keys before an entry leaves this module."""

    return [{k: v for k, v in entry.items() if not k.startswith("_")} for entry in entries]


def select_prunable(
    entries: list[dict[str, Any]],
    *,
    keep_count: int,
    keep_max_age_hours: int,
    now: datetime | None = None,
    protect: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Return the entries a run may delete, oldest first.

    ``entries`` must be sorted newest first (see :func:`snapshot_entries`).
    """

    current = now or datetime.now(UTC)
    keep_age = timedelta(hours=keep_max_age_hours)
    min_age = timedelta(hours=MIN_AGE_HOURS)
    keep = max(1, keep_count)
    protected = set(protect)
    prunable = []
    for index, entry in enumerate(entries):
        taken_at = entry.get("_taken_at")
        if taken_at is None:
            continue
        if index < keep:
            continue
        if entry["name"] in protected:
            continue
        age = current - taken_at
        if age < keep_age:
            continue
        if age < min_age:
            continue
        prunable.append(entry)
    prunable.sort(key=lambda entry: entry["_taken_at"])
    return prunable


def assess(
    entries: list[dict[str, Any]],
    *,
    keep_count: int,
    keep_max_age_hours: int,
    max_age_hours: int,
    now: datetime | None = None,
    protect: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Describe whether this cluster has a usable, fresh backup set."""

    current = now or datetime.now(UTC)
    findings: list[dict[str, Any]] = []
    if not entries:
        findings.append(
            {
                "name": "backup.newest",
                "ok": False,
                "code": "ETCD_BACKUP_MISSING",
                "detail": "no snapshot of this cluster exists in the backup directory",
            }
        )
        return {
            "count": 0,
            "newest": None,
            "newest_age_hours": None,
            "oldest": None,
            "prunable": [],
            "findings": findings,
            "ok": False,
        }
    newest = entries[0]
    newest_at = newest["_taken_at"]
    newest_age = (current - newest_at).total_seconds() / 3600.0
    findings.append(
        {
            "name": "backup.newest",
            "ok": newest_age <= max_age_hours,
            "code": "" if newest_age <= max_age_hours else "ETCD_BACKUP_STALE",
            "detail": (
                f"newest snapshot is {newest['name']}, "
                f"{newest_age:.1f}h old (limit {max_age_hours}h)"
            ),
        }
    )
    prunable = select_prunable(
        entries,
        keep_count=keep_count,
        keep_max_age_hours=keep_max_age_hours,
        now=current,
        protect=protect,
    )
    return {
        "count": len(entries),
        "newest": {k: v for k, v in newest.items() if not k.startswith("_")},
        "newest_age_hours": round(newest_age, 3),
        "oldest": {
            k: v for k, v in entries[-1].items() if not k.startswith("_")
        },
        "prunable": public_entries(prunable),
        "findings": findings,
        "ok": all(item["ok"] for item in findings),
    }


def simulation_plan(request: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Return the steps a run would execute, without touching the cluster."""

    name = backup_filename(request["cluster_name"], now=now)
    path = f"{request['directory']}/{name}"
    return {
        "cluster_name": request["cluster_name"],
        "execution_mode": "simulation",
        "backup_path": path,
        "approval_required": False,
        "requires_manual_execution": False,
        "performed_steps": [],
        "steps": [
            {
                "sequence": 1,
                "name": "snapshot_save",
                "commands": [
                    f"kubectl -n kube-system exec <etcd-pod> -- etcdctl snapshot save "
                    f"{request['directory']}/{partial_name(name)}",
                ],
                "detail": "",
            },
            {
                "sequence": 2,
                "name": "snapshot_verify",
                "commands": [
                    f"kubectl -n kube-system exec <etcd-pod> -- etcdctl snapshot status "
                    f"{request['directory']}/{partial_name(name)} --write-out=json",
                ],
                "detail": "",
            },
            {
                "sequence": 3,
                "name": "snapshot_publish",
                "commands": [
                    f"kubectl -n kube-system exec <etcd-pod> -- mv "
                    f"{request['directory']}/{partial_name(name)} {path}",
                ],
                "detail": "rename only after the snapshot verified",
            },
            {
                "sequence": 4,
                "name": "retention",
                "commands": [
                    f"kubectl -n kube-system exec <etcd-pod> -- rm -f "
                    f"{request['directory']}/<pruned-snapshot>"
                ],
                "detail": (
                    f"keep {request['keep_count']} newest files and anything younger "
                    f"than {request['keep_max_age_hours']}h; never the newest, never a "
                    "snapshot an approved rollback references"
                ),
            },
        ],
    }
