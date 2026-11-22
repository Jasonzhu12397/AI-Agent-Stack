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
    partial_name,
    public_entries,
    select_prunable,
    snapshot_entries,
)

Runner = Callable[..., dict[str, Any]]

STEP_TIMEOUT_SECONDS = 900
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

    # ------------------------------------------------------------------ run
    def run(self, request: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
        """Take one snapshot, verify it, publish it, then prune old ones."""

        stamp_now = now or datetime.now(UTC)
        directory = request["directory"]
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

        save = self._step(
            2,
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
            # A failed save must not reach retention: whatever exists stays.
            return self._failed("ETCD_BACKUP_SAVE_FAILED", directory, request)

        verify = self._step(
            3,
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
            4,
            "snapshot_publish",
            [f"kubectl -n kube-system exec <etcd-pod> -- mv {staging} {final}"],
            detail="rename only after the snapshot verified",
        )
        result = self._runner(
            self._pod_shell(["mv", staging, final]),
            timeout=STEP_TIMEOUT_SECONDS,
        )
        if not self._finish(publish, result):
            return self._failed("ETCD_BACKUP_PUBLISH_FAILED", directory, request)

        entries = snapshot_entries(
            self._list_directory(directory), cluster_name=request["cluster_name"]
        )
        prunable = select_prunable(
            entries,
            keep_count=request["keep_count"],
            keep_max_age_hours=request["keep_max_age_hours"],
            pre_upgrade_keep_max_age_hours=request["pre_upgrade_keep_max_age_hours"],
            now=stamp_now,
        )
        removed_names = {item["name"] for item in prunable}
        retained = [entry for entry in entries if entry["name"] not in removed_names]
        retention = self._step(
            5,
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
        removed = []
        for entry in prunable:
            path = f"{directory}/{entry['name']}"
            outcome = self._runner(
                self._pod_shell(["rm", "-f", path]),
                timeout=STEP_TIMEOUT_SECONDS,
            )
            if _ok(outcome):
                removed.append(entry["name"])
        retention["status"] = "succeeded"
        retention["exit_code"] = 0
        retention["detail"] = (
            f"removed {len(removed)} file(s)" if removed else "nothing to prune"
        )

        report = assess(
            snapshot_entries(self._list_directory(directory), cluster_name=request["cluster_name"]),
            keep_count=request["keep_count"],
            keep_max_age_hours=request["keep_max_age_hours"],
            max_age_hours=request["max_age_hours"],
            now=stamp_now,
        )
        return {
            "cluster_name": request["cluster_name"],
            "execution_mode": "execute",
            "backup": {
                "name": filename,
                "path": final,
                "verified": True,
                "taken_at": stamp_now.isoformat(),
            },
            "pruned": removed,
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
        }

    def status(self, request: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
        """Read-only: what backups exist and whether the newest one is fresh."""

        entries = snapshot_entries(
            self._list_directory(request["directory"]),
            cluster_name=request["cluster_name"],
        )
        report = assess(
            entries,
            keep_count=request["keep_count"],
            keep_max_age_hours=request["keep_max_age_hours"],
            pre_upgrade_keep_max_age_hours=request["pre_upgrade_keep_max_age_hours"],
            max_age_hours=request["max_age_hours"],
            now=now,
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

    def _failed(
        self, code: str, directory: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        for step in self._steps:
            if step["status"] == "running":
                step["status"] = "failed"
        return {
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
