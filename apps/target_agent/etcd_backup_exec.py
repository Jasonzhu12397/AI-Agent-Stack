"""Take a recurring etcd snapshot and enforce the retention policy.

The job is deliberately ordered the safe way round: save into ``*.partial``,
prove the file with ``snapshot status``, rename it to its final name, and only
then delete old files. A failed save therefore cannot delete the backup that
still exists, and a half-written file never looks like a backup.

Deletion itself is decided by :mod:`packages.agent_contracts.etcd_backup`, so
the platform, the CLI and this executor cannot disagree about which files may
go.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from packages.agent_contracts.etcd_backup import (
    EtcdBackupError,
    assess,
    backup_filename,
    disk_verdict,
    partial_name,
    public_entries,
    select_prunable,
    snapshot_entries,
    staging_name_belongs,
)

Runner = Callable[..., dict[str, Any]]

STEP_TIMEOUT_SECONDS = 900
#: A staging file older than this was not left by a run that is still going: it
#: was left by a run whose process died, and no retention pass will ever look
#: at it because it is not a snapshot.
PARTIAL_STALE_MINUTES = 24 * 60
ETCD_PKI_DIR = "/etc/kubernetes/pki/etcd"


class EtcdBackupExecutionError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _output(result: dict[str, Any]) -> str:
    return (
        str(result.get("stdout") or "").strip()
        or str(result.get("stderr") or "").strip()
    )


def _ok(result: dict[str, Any]) -> bool:
    return result.get("available", True) and result.get("returncode") == 0


def _db_size(result: dict[str, Any]) -> int | None:
    """Read ``dbSize`` out of ``etcdctl endpoint status --write-out=json``.

    The payload is a list of member statuses; any member answers with the
    cluster-wide database size, so the first well-formed one is enough.
    """

    if not _ok(result):
        return None
    body = str(result.get("stdout") or "").strip()
    if not body:
        return None
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    items = parsed if isinstance(parsed, list) else [parsed]
    for item in items:
        if not isinstance(item, dict):
            continue
        header = item.get("Header") if isinstance(item.get("Header"), dict) else item
        size = header.get("dbSize")
        if isinstance(size, int) and size >= 0:
            return size
    return None


class EtcdBackupExecutor:
    """One backup run against the local control plane's etcd pod."""

    def __init__(self, runner: Runner) -> None:
        self._runner = runner
        self._steps: list[dict[str, Any]] = []
        self._pod = ""

    # ------------------------------------------------------------------ steps
    def _step(
        self, sequence: int, name: str, commands: list[str], detail: str = ""
    ) -> dict[str, Any]:
        step = {
            "sequence": sequence,
            "name": name,
            "status": "running",
            "commands": commands,
            "exit_code": None,
            "detail": detail,
        }
        self._steps.append(step)
        return step

    def _finish(
        self, step: dict[str, Any], result: dict[str, Any], *, detail: str = ""
    ) -> bool:
        ok = _ok(result)
        step["status"] = "succeeded" if ok else "failed"
        step["exit_code"] = result.get("returncode")
        if detail:
            step["detail"] = detail
        if not ok and not step["detail"]:
            step["detail"] = _output(result) or "no output"
        return ok

    # ------------------------------------------------------------------ pod
    def _etcd_pod(self) -> str:
        """Return the etcd pod running on this node.

        A stacked etcd runs one pod per control plane; the snapshot this job
        writes is a cluster-wide one, so any member would do. The local member
        is used because the directory it writes is the one this agent can also
        read for retention, and because the same member is the one an upgrade
        snapshots.
        """

        if self._pod:
            return self._pod
        listing = self._runner(
            ["kubectl", "-n", "kube-system", "get", "pods", "-o", "json"],
            timeout=STEP_TIMEOUT_SECONDS,
        )
        if not _ok(listing):
            raise EtcdBackupExecutionError(
                "ETCD_BACKUP_POD_NOT_FOUND",
                "cannot list kube-system pods: " + (_output(listing) or "no output"),
            )
        try:
            items = json.loads(listing.get("stdout") or "").get("items") or []
        except ValueError as exc:
            raise EtcdBackupExecutionError(
                "ETCD_BACKUP_POD_NOT_FOUND", f"pod list is not JSON: {exc}"
            ) from exc
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str((item.get("metadata") or {}).get("name") or "")
            if not name.startswith("etcd-"):
                continue
            phase = str((item.get("status") or {}).get("phase") or "")
            if phase and phase != "Running":
                continue
            self._pod = name
            return name
        raise EtcdBackupExecutionError(
            "ETCD_BACKUP_POD_NOT_FOUND",
            "no running etcd pod in kube-system",
        )

    def _etcdctl_argv(self) -> list[str]:
        return [
            "kubectl",
            "-n",
            "kube-system",
            "exec",
            self._etcd_pod(),
            "--",
            "etcdctl",
            "--endpoints=https://127.0.0.1:2379",
            f"--cacert={ETCD_PKI_DIR}/ca.crt",
            f"--cert={ETCD_PKI_DIR}/server.crt",
            f"--key={ETCD_PKI_DIR}/server.key",
        ]

    def _pod_shell(self, argv: list[str]) -> list[str]:
        return ["kubectl", "-n", "kube-system", "exec", self._etcd_pod(), "--", *argv]

    # ------------------------------------------------------------------ directory
    def _list_directory(self, directory: str) -> list[dict[str, Any]]:
        """Read the backup directory through the etcd pod.

        ``ls -ln`` output is parsed rather than trusted as JSON: the pod carries
        only busybox tools, and the file name is the last field. Sizes and
        timestamps come from the listing; the authoritative timestamp is the one
        encoded in the file name.
        """

        listing = self._runner(
            self._pod_shell(["ls", "-ln", directory]),
            timeout=STEP_TIMEOUT_SECONDS,
        )
        if not _ok(listing):
            message = _output(listing)
            if "No such file" in message or "not found" in message:
                return []
            raise EtcdBackupExecutionError(
                "ETCD_BACKUP_DIRECTORY_UNREADABLE",
                f"cannot list {directory}: {message or 'no output'}",
            )
        entries = []
        for line in str(listing.get("stdout") or "").splitlines():
            fields = line.split()
            if len(fields) < 9 or fields[0].startswith("total"):
                continue
            name = fields[-1]
            size = fields[4]
            entries.append(
                {
                    "name": name,
                    "size_bytes": int(size) if size.isdigit() else None,
                }
            )
        return entries

    def _union_entries(
        self, directories: list[str], cluster_name: str
    ) -> list[dict[str, Any]]:
        """List every candidate directory and tag each entry with its location.

        After a directory rotation the retired directory still holds this
        cluster's snapshots, so retention and the status report enumerate the
        whole candidate set instead of only the current policy directory. Each
        entry keeps the directory it was found in, so a later prune deletes the
        file at its own absolute path.
        """

        listings: list[dict[str, Any]] = []
        for entry_directory in directories:
            for item in self._list_directory(entry_directory):
                item["directory"] = entry_directory
                listings.append(item)
        entries = snapshot_entries(listings, cluster_name=cluster_name)
        listing_dir = {item["name"]: item["directory"] for item in listings}
        for entry in entries:
            entry["directory"] = listing_dir.get(entry["name"], directories[0])
        return entries

    def _sweep_stale_partials(self, directory: str, cluster_name: str) -> list[str]:
        """Remove staging files an earlier, killed run left behind.

        Only files that match the staging suffix and are older than
        ``PARTIAL_STALE_MINUTES`` are touched, and only paths that sit directly
        in the backup directory: a sweep must never be able to delete anything
        a backup would otherwise keep.
        """

        listing = self._runner(
            self._pod_shell(
                [
                    "find",
                    directory,
                    "-maxdepth",
                    "1",
                    "-type",
                    "f",
                    "-name",
                    "*.db.partial",
                    "-mmin",
                    f"+{PARTIAL_STALE_MINUTES}",
                    "-print",
                ]
            ),
            timeout=STEP_TIMEOUT_SECONDS,
        )
        if not _ok(listing):
            return []
        removed = []
        for line in str(listing.get("stdout") or "").splitlines():
            path = line.strip()
            if not path.endswith(".db.partial"):
                continue
            if path.rsplit("/", 1)[0] != directory.rstrip("/") or ".." in path:
                continue
            if not staging_name_belongs(
                path.rsplit("/", 1)[-1], cluster_name=cluster_name
            ):
                continue
            outcome = self._runner(
                self._pod_shell(["rm", "-f", path]),
                timeout=STEP_TIMEOUT_SECONDS,
            )
            if _ok(outcome):
                removed.append(path.rsplit("/", 1)[-1])
        return removed

    # ------------------------------------------------------------------ run
    def run(self, request: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
        """Take one snapshot, verify it, publish it, then prune old ones."""

        stamp_now = now or datetime.now(UTC)
        directory = request["directory"]
        directories = request.get("directories") or [directory]
        filename = backup_filename(request["cluster_name"], now=stamp_now)
        staging = f"{directory}/{partial_name(filename)}"
        final = f"{directory}/{filename}"

        prepare = self._step(
            1,
            "prepare",
            [f"kubectl -n kube-system exec <etcd-pod> -- mkdir -p {directory}"],
        )
        result = self._runner(
            self._pod_shell(["mkdir", "-p", directory]),
            timeout=STEP_TIMEOUT_SECONDS,
        )
        if not self._finish(prepare, result):
            return self._failed("ETCD_BACKUP_PREPARE_FAILED", directory, request)
        swept: list[str] = []
        for entry_directory in directories:
            swept.extend(
                self._sweep_stale_partials(entry_directory, request["cluster_name"])
            )

        disk = self._step(
            2,
            "disk_check",
            [
                "kubectl -n kube-system exec <etcd-pod> -- etcdctl endpoint status "
                "--write-out=json",
                f"kubectl -n kube-system exec <etcd-pod> -- df -PB1 {directory}",
            ],
            detail="measure the live database and the free space before writing",
        )
        verdict = self._disk_verdict(directory, request)
        disk["detail"] = verdict["detail"]
        if not verdict["ok"]:
            return self._failed(verdict["code"], directory, request, verdict=verdict)
        disk["status"] = "succeeded"
        disk["exit_code"] = 0

        save = self._step(
            3,
            "snapshot_save",
            [
                f"kubectl -n kube-system exec <etcd-pod> -- etcdctl snapshot save {staging}"
            ],
        )
        result = self._runner(
            [*self._etcdctl_argv(), "snapshot", "save", staging],
            timeout=STEP_TIMEOUT_SECONDS,
        )
        if not self._finish(save, result):
            # A save that died mid-write leaves a half-written .partial behind,
            # as large as whatever it managed to dump. It is not a snapshot, so
            # retention will never look at it: it would sit on the volume until
            # the disk filled. Whatever existed before is untouched.
            self._best_effort_remove(staging)
            return self._failed("ETCD_BACKUP_SAVE_FAILED", directory, request)

        verify = self._step(
            4,
            "snapshot_verify",
            [
                "kubectl -n kube-system exec <etcd-pod> -- etcdctl snapshot status "
                f"{staging} --write-out=json"
            ],
        )
        result = self._runner(
            [*self._etcdctl_argv(), "snapshot", "status", staging, "--write-out=json"],
            timeout=STEP_TIMEOUT_SECONDS,
        )
        if not self._finish(verify, result):
            self._best_effort_remove(staging)
            return self._failed("ETCD_BACKUP_VERIFY_FAILED", directory, request)

        publish = self._step(
            5,
            "snapshot_publish",
            [f"kubectl -n kube-system exec <etcd-pod> -- mv {staging} {final}"],
            detail="rename only after the snapshot verified",
        )
        result = self._runner(
            self._pod_shell(["mv", staging, final]),
            timeout=STEP_TIMEOUT_SECONDS,
        )
        if not self._finish(publish, result):
            # A rename that failed leaves the verified file under its staging
            # name; it is still a usable snapshot, so it is kept rather than
            # deleted, and the next run's sweep reports it as an orphan.
            return self._failed(
                "ETCD_BACKUP_PUBLISH_FAILED",
                directory,
                request,
                staging_kept=staging,
            )

        entries = self._union_entries(directories, request["cluster_name"])
        prunable = select_prunable(
            entries,
            keep_count=request["keep_count"],
            keep_max_age_hours=request["keep_max_age_hours"],
            pre_upgrade_keep_max_age_hours=request["pre_upgrade_keep_max_age_hours"],
            now=stamp_now,
            protect=tuple(request.get("protect") or ()),
        )
        removed_names = {item["name"] for item in prunable}
        retained = [entry for entry in entries if entry["name"] not in removed_names]
        retention = self._step(
            6,
            "retention",
            [
                f"kubectl -n kube-system exec <etcd-pod> -- rm -f {item['name']}"
                for item in prunable
            ]
            or ["nothing to prune"],
            detail=(
                f"keep {request['keep_count']} newest and anything younger than "
                f"{request['keep_max_age_hours']}h; pre-upgrade snapshots older than "
                f"{request['pre_upgrade_keep_max_age_hours']}h except the newest"
            ),
        )
        removed: list[str] = []
        removed_entries: list[dict[str, str]] = []
        for entry in prunable:
            entry_directory = entry.get("directory") or directory
            path = f"{entry_directory}/{entry['name']}"
            outcome = self._runner(
                self._pod_shell(["rm", "-f", path]),
                timeout=STEP_TIMEOUT_SECONDS,
            )
            if _ok(outcome):
                removed.append(entry["name"])
                removed_entries.append(
                    {"name": entry["name"], "directory": entry_directory}
                )
        retention["status"] = "succeeded"
        retention["exit_code"] = 0
        retention["detail"] = (
            f"removed {len(removed)} file(s)" if removed else "nothing to prune"
        )

        report = assess(
            self._union_entries(directories, request["cluster_name"]),
            keep_count=request["keep_count"],
            keep_max_age_hours=request["keep_max_age_hours"],
            max_age_hours=request["max_age_hours"],
            now=stamp_now,
        )
        result = {
            "cluster_name": request["cluster_name"],
            "execution_mode": "execute",
            "backup": {
                "name": filename,
                "path": final,
                "verified": True,
                "taken_at": stamp_now.isoformat(),
            },
            "pruned": removed,
            "pruned_entries": removed_entries,
            "kept": public_entries(retained),
            "retention": {
                "keep_count": request["keep_count"],
                "keep_max_age_hours": request["keep_max_age_hours"],
            },
            "status": report["ok"],
            "findings": report["findings"],
            "count": report["count"],
            "pre_upgrade_count": report["pre_upgrade_count"],
            "pre_upgrade": report["pre_upgrade"],
            "steps": self._steps,
            "failure_code": None,
            "performed_steps": [step["name"] for step in self._steps],
            "swept_partials": swept,
        }
        return result

    def status(self, request: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
        """Read-only: what backups exist and whether the newest one is fresh."""

        directories = request.get("directories") or [request["directory"]]
        entries = self._union_entries(directories, request["cluster_name"])
        report = assess(
            entries,
            keep_count=request["keep_count"],
            keep_max_age_hours=request["keep_max_age_hours"],
            pre_upgrade_keep_max_age_hours=request["pre_upgrade_keep_max_age_hours"],
            max_age_hours=request["max_age_hours"],
            now=now,
            protect=tuple(request.get("protect") or ()),
        )
        return {
            "cluster_name": request["cluster_name"],
            "directory": request["directory"],
            "entries": public_entries(entries),
            **report,
        }

    # ------------------------------------------------------------------ helpers
    def _best_effort_remove(self, path: str) -> None:
        self._runner(self._pod_shell(["rm", "-f", path]), timeout=60)

    def _disk_verdict(self, directory: str, request: dict[str, Any]) -> dict[str, Any]:
        """Ask the live database its size and the volume its free space.

        Both answers come from the etcd pod: the size from ``endpoint status``
        and the free space from ``df``. If either cannot be read the run does not
        guess - a snapshot written into a nearly full volume is worse than a
        snapshot that was skipped and reported.
        """

        status = self._runner(
            [*self._etcdctl_argv(), "endpoint", "status", "--write-out=json"],
            timeout=STEP_TIMEOUT_SECONDS,
        )
        db_size = _db_size(status)
        if db_size is None:
            return {
                "ok": False,
                "code": "ETCD_BACKUP_DISK_CHECK_FAILED",
                "detail": (
                    "cannot read the database size from endpoint status: "
                    + (_output(status) or "no output")
                ),
                "db_size_bytes": None,
                "free_bytes": None,
                "required_bytes": None,
            }
        free = self._free_bytes(directory)
        if free is None:
            return {
                "ok": False,
                "code": "ETCD_BACKUP_DISK_CHECK_FAILED",
                "detail": f"cannot read free space in {directory}",
                "db_size_bytes": db_size,
                "free_bytes": None,
                "required_bytes": None,
            }
        return disk_verdict(
            db_size, free, min_free_bytes=request.get("min_free_bytes", 0)
        )

    def _free_bytes(self, directory: str) -> int | None:
        listing = self._runner(
            self._pod_shell(["df", "-PB1", directory]),
            timeout=STEP_TIMEOUT_SECONDS,
        )
        if not _ok(listing):
            return None
        for line in str(listing.get("stdout") or "").splitlines():
            fields = line.split()
            if len(fields) < 4 or not fields[0].startswith(("/", "overlay", "tmpfs")):
                continue
            if fields[3].isdigit():
                return int(fields[3])
        return None

    def _failed(
        self, code: str, directory: str, request: dict[str, Any], **extra: Any
    ) -> dict[str, Any]:
        for step in self._steps:
            if step["status"] == "running":
                step["status"] = "failed"
        result = {
            "cluster_name": request["cluster_name"],
            "directory": directory,
            "execution_mode": "execute",
            "backup": None,
            "pruned": [],
            "kept": [],
            "retention": {},
            "status": False,
            "findings": [],
            "count": 0,
            "steps": self._steps,
            "failure_code": code,
            "performed_steps": [step["name"] for step in self._steps],
        }
        # Callers pass what they measured so a failed report still explains
        # itself; before this, the extras were accepted and silently dropped.
        result.update(extra)
        return result


def validate_and_run(
    runner: Runner, payload: Any, *, cluster_name: str | None = None
) -> dict[str, Any]:
    """Validate a request, then either plan it or execute it."""

    from packages.agent_contracts.etcd_backup import simulation_plan as plan
    from packages.agent_contracts.etcd_backup import validate_request

    request = validate_request(payload, cluster_name=cluster_name)
    if request["execution_mode"] == "simulation":
        return plan(request)
    executor = EtcdBackupExecutor(runner)
    try:
        return executor.run(request)
    except EtcdBackupExecutionError as exc:
        return {
            "cluster_name": request["cluster_name"],
            "execution_mode": "execute",
            "backup": None,
            "pruned": [],
            "kept": [],
            "status": False,
            "failure_code": exc.code,
            "detail": exc.detail,
            "steps": executor._steps,
            "performed_steps": [step["name"] for step in executor._steps],
        }


__all__ = [
    "EtcdBackupError",
    "EtcdBackupExecutionError",
    "EtcdBackupExecutor",
    "validate_and_run",
]
