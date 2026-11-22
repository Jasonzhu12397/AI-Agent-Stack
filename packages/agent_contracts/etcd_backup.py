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
        "pre_upgrade_keep_max_age_hours",
        "max_age_hours",
        "execution_mode",
        "confirm_cluster_name",
        "protect",
        "min_free_bytes",
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
#: Pre-upgrade snapshots are a different kind of file with a different duty:
#: they exist so a bad upgrade can be undone, so they may live for a week and
#: the newest one is never deleted. Without a policy here they would accumulate
#: one per upgrade forever and quietly fill the disk.
DEFAULT_PRE_UPGRADE_KEEP_MAX_AGE_HOURS = 168
MAX_PRE_UPGRADE_KEEP_MAX_AGE_HOURS = 8760
#: The backup directory lives on the etcd data volume. A snapshot roughly the
#: size of the live database has to fit next to the database, or the save fails
#: halfway and the volume is left full for the cluster that is still running.
#: This is the floor of free space a run insists on before it writes anything.
DEFAULT_MIN_FREE_BYTES = 512 * 1024 * 1024
MIN_FREE_BYTES_FLOOR = 0
MAX_MIN_FREE_BYTES = 1024 * 1024 * 1024 * 1024

_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
_DIRECTORY = re.compile(r"^/var/lib/etcd[A-Za-z0-9._/-]*$")
_FILE = re.compile(r"^(?P<cluster>[a-z0-9][-a-z0-9.]*)-(?P<stamp>\d{8}T\d{6}Z)\.db$")
#: The upgrade executor writes these before it changes a control plane. They
#: carry the cluster name from this version on; older files only carry the
#: target version, and are still recognised because the directory belongs to
#: one Target node.
_PRE_UPGRADE = re.compile(
    r"^snapshot-pre-(?P<tail>[A-Za-z0-9][A-Za-z0-9._-]*)-(?P<stamp>\d{8}T\d{6}Z)\.db$"
)


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


def disk_verdict(
    db_size_bytes: int, free_bytes: int, *, min_free_bytes: int
) -> dict[str, Any]:
    """Decide whether a snapshot fits, before anything is written.

    ``required`` is one copy of the database plus the floor: a run needs room
    for the file it is about to write and room to keep operating afterwards.
    """

    db_size = max(0, int(db_size_bytes))
    free = max(0, int(free_bytes))
    required = db_size + max(0, int(min_free_bytes))
    ok = free >= required
    return {
        "ok": ok,
        "db_size_bytes": db_size,
        "free_bytes": free,
        "required_bytes": required,
        "min_free_bytes": max(0, int(min_free_bytes)),
        "code": "" if ok else "ETCD_BACKUP_DISK_LOW",
        "detail": (
            f"{free} byte(s) free, {required} required "
            f"(database {db_size} + floor {max(0, int(min_free_bytes))})"
        ),
    }


def _protect_names(value: Any, *, cluster_name: str) -> tuple[str, ...]:
    """Names a caller is holding, e.g. a snapshot a rollback will restore.

    Only bare file names are accepted: a caller may not point the retention
    pass at a path outside the backup directory, and a name that is not one of
    the two snapshot kinds means nothing here.
    """

    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise EtcdBackupError("ETCD_BACKUP_PROTECT_INVALID", "protect must be a list")
    names = []
    for item in value:
        name = str(item or "").strip()
        if not name or "/" in name or ".." in name:
            raise EtcdBackupError(
                "ETCD_BACKUP_PROTECT_INVALID", "protect entries must be bare file names"
            )
        if (
            parse_backup_name(name, cluster_name=cluster_name) is None
            and parse_pre_upgrade_name(name, cluster_name=cluster_name) is None
        ):
            raise EtcdBackupError(
                "ETCD_BACKUP_PROTECT_INVALID", f"{name} is not a snapshot of this cluster"
            )
        names.append(name)
    return tuple(sorted(set(names)))


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
    protect = _protect_names(payload.get("protect"), cluster_name=cluster)
    min_free = _int(
        payload.get("min_free_bytes"),
        code="ETCD_BACKUP_DISK_POLICY_INVALID",
        low=MIN_FREE_BYTES_FLOOR,
        high=MAX_MIN_FREE_BYTES,
        default=DEFAULT_MIN_FREE_BYTES,
    )
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
    pre_upgrade_age = _int(
        payload.get("pre_upgrade_keep_max_age_hours"),
        code="ETCD_BACKUP_RETENTION_INVALID",
        low=1,
        high=MAX_PRE_UPGRADE_KEEP_MAX_AGE_HOURS,
        default=DEFAULT_PRE_UPGRADE_KEEP_MAX_AGE_HOURS,
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
        "pre_upgrade_keep_max_age_hours": pre_upgrade_age,
        "max_age_hours": max_age,
        "execution_mode": mode,
        "confirm_cluster_name": cluster if mode == "execute" else "",
        "protect": protect,
        "min_free_bytes": min_free,
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


def parse_pre_upgrade_name(
    name: str, *, cluster_name: str = ""
) -> dict[str, Any] | None:
    """Return an entry for a pre-upgrade snapshot, or ``None``.

    The current naming is ``snapshot-pre-<cluster>-<target version>-<stamp>.db``.
    Files written by older versions are ``snapshot-pre-<target version>-<stamp>.db``:
    they cannot name their cluster, but the backup directory belongs to one
    Target node, so they are still this cluster's files. Legacy files are
    reported with ``cluster: ""`` instead of being guessed at.
    """

    match = _PRE_UPGRADE.fullmatch(name)
    if not match:
        return None
    tail = match.group("tail")
    stamp = match.group("stamp")
    try:
        taken_at = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    cluster = ""
    version = tail
    if cluster_name and tail.startswith(cluster_name + "-") and len(tail) > len(cluster_name) + 1:
        cluster = cluster_name
        version = tail[len(cluster_name) + 1 :]
    return {
        "name": name,
        "cluster": cluster,
        "version": version,
        "stamp": stamp,
        "taken_at": taken_at.isoformat(),
        "kind": "pre_upgrade",
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
        if parsed:
            parsed = {**parsed, "kind": "periodic"}
        else:
            parsed = parse_pre_upgrade_name(name, cluster_name=cluster_name)
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


def restore_point(
    entries: list[dict[str, Any]], *, now: datetime | None = None
) -> dict[str, Any] | None:
    """The snapshot an operator would put back to undo the last change.

    A pre-upgrade snapshot is taken immediately before a change and is the only
    file that describes the state before it, so it wins over a recurring backup
    even when the recurring one is newer. Falling back to the newest recurring
    backup keeps this answer non-empty on a cluster that has never been upgraded.
    """

    current = now or datetime.now(UTC)
    if not entries:
        return None
    pre_upgrade = [entry for entry in entries if _kind(entry) == "pre_upgrade"]
    periodic = [entry for entry in entries if _kind(entry) == "periodic"]
    entry = (pre_upgrade or periodic)[0]
    taken_at = entry["_taken_at"]
    return {
        "name": entry["name"],
        "kind": _kind(entry),
        "taken_at": entry["taken_at"],
        "age_hours": round((current - taken_at).total_seconds() / 3600.0, 3),
        "size_bytes": entry.get("size_bytes"),
        "version": entry.get("version"),
    }


def _kind(entry: Mapping[str, Any]) -> str:
    return str(entry.get("kind") or "periodic")


def select_prunable(
    entries: list[dict[str, Any]],
    *,
    keep_count: int,
    keep_max_age_hours: int,
    pre_upgrade_keep_max_age_hours: int = DEFAULT_PRE_UPGRADE_KEEP_MAX_AGE_HOURS,
    now: datetime | None = None,
    protect: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Return the entries a run may delete, oldest first.

    Recurring backups are kept by count and by age. Pre-upgrade snapshots are
    kept by age only, minus the newest one: the newest pre-upgrade snapshot is
    what a rollback of the last change would restore, so it outlives the policy.
    ``entries`` must be sorted newest first (see :func:`snapshot_entries`).
    """

    current = now or datetime.now(UTC)
    keep_age = timedelta(hours=keep_max_age_hours)
    pre_upgrade_age = timedelta(hours=pre_upgrade_keep_max_age_hours)
    min_age = timedelta(hours=MIN_AGE_HOURS)
    keep = max(1, keep_count)
    protected = set(protect)
    prunable = []
    seen_pre_upgrade = 0
    seen_periodic = 0
    for entry in entries:
        taken_at = entry.get("_taken_at")
        if taken_at is None:
            continue
        if entry["name"] in protected:
            continue
        age = current - taken_at
        if age < min_age:
            continue
        if _kind(entry) == "pre_upgrade":
            seen_pre_upgrade += 1
            if seen_pre_upgrade == 1:
                continue
            if age < pre_upgrade_age:
                continue
            prunable.append(entry)
            continue
        # keep_count counts recurring snapshots, not positions in the whole
        # listing: a few recent pre-upgrade files must not use up the slots and
        # cause a periodic backup inside the count to be deleted.
        seen_periodic += 1
        if seen_periodic <= keep:
            continue
        if age < keep_age:
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
    pre_upgrade_keep_max_age_hours: int = DEFAULT_PRE_UPGRADE_KEEP_MAX_AGE_HOURS,
    now: datetime | None = None,
    protect: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Describe whether this cluster has a usable, fresh backup set."""

    current = now or datetime.now(UTC)
    periodic = [entry for entry in entries if _kind(entry) == "periodic"]
    pre_upgrade = [entry for entry in entries if _kind(entry) == "pre_upgrade"]
    findings: list[dict[str, Any]] = []
    if not periodic:
        findings.append(
            {
                "name": "backup.newest",
                "ok": False,
                "code": "ETCD_BACKUP_MISSING",
                "detail": (
                    "no recurring backup of this cluster exists in the backup "
                    "directory; a pre-upgrade snapshot only protects one change"
                ),
            }
        )
        return {
            "count": 0,
            "newest": None,
            "newest_age_hours": None,
            "oldest": None,
            "pre_upgrade_count": len(pre_upgrade),
            "pre_upgrade": public_entries(pre_upgrade),
            "prunable": [],
            "restore_point": None,
            "findings": findings,
            "ok": False,
        }
    newest = periodic[0]
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
        pre_upgrade_keep_max_age_hours=pre_upgrade_keep_max_age_hours,
        now=current,
        protect=protect,
    )
    point = restore_point(entries, now=current)
    findings.append(
        {
            "name": "backup.restore_point",
            "ok": point is not None,
            "code": "" if point else "ETCD_BACKUP_NO_RESTORE_POINT",
            "detail": (
                f"a rollback would restore {point['name']} ({point['kind']}, "
                f"{point['age_hours']:.1f}h old)"
                if point
                else "no snapshot exists that a rollback could restore"
            ),
        }
    )
    return {
        "count": len(periodic),
        "newest": {k: v for k, v in newest.items() if not k.startswith("_")},
        "newest_age_hours": round(newest_age, 3),
        "oldest": {
            k: v for k, v in periodic[-1].items() if not k.startswith("_")
        },
        "pre_upgrade_count": len(pre_upgrade),
        "pre_upgrade": public_entries(pre_upgrade),
        "prunable": public_entries(prunable),
        "restore_point": point,
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
                "name": "prepare",
                "commands": [
                    f"kubectl -n kube-system exec <etcd-pod> -- mkdir -p {request['directory']}",
                    "kubectl -n kube-system exec <etcd-pod> -- find <directory> -maxdepth 1 "
                    "-name '*.db.partial' -mmin +1440 -print | xargs -r rm -f",
                ],
                "detail": (
                    "the etcd pod mounts the host backup directory at this path; "
                    "staging files older than a day were left by a run that died "
                    "and are removed, because retention never looks at them"
                ),
            },
            {
                "sequence": 2,
                "name": "disk_check",
                "commands": [
                    "kubectl -n kube-system exec <etcd-pod> -- etcdctl endpoint status "
                    "--write-out=json",
                    f"kubectl -n kube-system exec <etcd-pod> -- df -PB1 {request['directory']}",
                ],
                "detail": (
                    "refuse the run when the database plus the "
                    f"{request['min_free_bytes']} byte floor do not fit; a snapshot "
                    "written into a nearly full etcd volume is worse than no snapshot"
                ),
            },
            {
                "sequence": 3,
                "name": "snapshot_save",
                "commands": [
                    f"kubectl -n kube-system exec <etcd-pod> -- etcdctl snapshot save "
                    f"{request['directory']}/{partial_name(name)}",
                ],
                "detail": "written as a partial file so it never looks published",
            },
            {
                "sequence": 4,
                "name": "snapshot_verify",
                "commands": [
                    f"kubectl -n kube-system exec <etcd-pod> -- etcdctl snapshot status "
                    f"{request['directory']}/{partial_name(name)} --write-out=json",
                ],
                "detail": "a snapshot that cannot be read is not a backup",
            },
            {
                "sequence": 5,
                "name": "snapshot_publish",
                "commands": [
                    f"kubectl -n kube-system exec <etcd-pod> -- mv "
                    f"{request['directory']}/{partial_name(name)} {path}",
                ],
                "detail": "rename only after the snapshot verified",
            },
            {
                "sequence": 6,
                "name": "retention",
                "commands": [
                    f"kubectl -n kube-system exec <etcd-pod> -- rm -f "
                    f"{request['directory']}/<pruned-snapshot>"
                ],
                "detail": (
                    f"keep {request['keep_count']} newest files and anything younger "
                    f"than {request['keep_max_age_hours']}h; pre-upgrade snapshots "
                    f"older than {request['pre_upgrade_keep_max_age_hours']}h except "
                    "the newest one; never the newest, never a snapshot an approved "
                    "rollback references"
                ),
            },
        ],
    }
