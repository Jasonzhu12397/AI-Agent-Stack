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
    DEFAULT_STAGING_MAX_PUBLISH_ATTEMPTS,
    DEFAULT_STAGING_REPUBLISH_WINDOW_HOURS,
    STAGING_STATE_ABANDONED,
    STAGING_STATE_PENDING,
    STAGING_STATE_PUBLISHED,
    EtcdBackupError,
    assess,
    backup_filename,
    decode_staging_state,
    disk_verdict,
    encode_staging_state,
    parse_backup_name,
    parse_pre_upgrade_name,
    partial_name,
    public_entries,
    select_prunable,
    snapshot_entries,
    staging_name_belongs,
    staging_occupancy,
    staging_sidecar_name,
    staging_state_decision,
)

Runner = Callable[..., dict[str, Any]]

STEP_TIMEOUT_SECONDS = 900
#: A staging file older than this was not left by a run that is still going: it
#: was left by a run whose process died, and no retention pass will ever look
#: at it because it is not a snapshot.
PARTIAL_STALE_MINUTES = 24 * 60
ETCD_PKI_DIR = "/etc/kubernetes/pki/etcd"
#: Shared with the other mutating actions: a lease lost mid-run stops the
#: command before it deletes or overwrites anything else.
COMMAND_LEASE_LOST_DURING_EXECUTION = "COMMAND_LEASE_LOST_DURING_EXECUTION"


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
        self._lease_lost_event: Any = None
        self._cleanup_failures: list[dict[str, str]] = []

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

    # ------------------------------------------------------------------ staging state
    def _read_text(self, path: str) -> str | None:
        result = self._runner(
            self._pod_shell(["cat", path]), timeout=STEP_TIMEOUT_SECONDS
        )
        if not _ok(result):
            return None
        body = str(result.get("stdout") or "").strip()
        return body or None

    def _write_text(self, path: str, body: str) -> bool:
        outcome = self._runner(
            self._pod_shell(
                ["sh", "-c", 'printf "%s" "$1" > "$2"', "sh", body, path]
            ),
            timeout=STEP_TIMEOUT_SECONDS,
        )
        return _ok(outcome)

    def _read_staging_state(
        self, directory: str, partial: str, cluster_name: str
    ) -> dict[str, Any] | None:
        sidecar = f"{directory}/{staging_sidecar_name(partial)}"
        body = self._read_text(sidecar)
        if body is None:
            return None
        return decode_staging_state(body, cluster_name=cluster_name)

    def _write_staging_state(
        self, directory: str, partial: str, record: dict[str, Any]
    ) -> None:
        sidecar = f"{directory}/{staging_sidecar_name(partial)}"
        self._write_text(sidecar, encode_staging_state(record))

    def _remove_staging_state(self, directory: str, partial: str) -> None:
        self._best_effort_remove(
            f"{directory}/{staging_sidecar_name(partial)}"
        )

    def _staging_record(
        self,
        *,
        cluster_name: str,
        partial: str,
        final_name: str,
        directory: str,
        now: datetime,
        state: str,
        attempts: int,
        last_failure: str,
    ) -> dict[str, Any]:
        return {
            "cluster_name": cluster_name,
            "final_name": final_name,
            "directory": directory,
            "verified_at": now.isoformat(),
            "state": state,
            "attempts": attempts,
            "last_failure": last_failure,
            "updated_at": now.isoformat(),
        }

    @staticmethod
    def _stamp(name: str) -> datetime | None:
        parsed = parse_backup_name(name) or parse_pre_upgrade_name(name)
        if not parsed:
            return None
        try:
            return datetime.fromisoformat(parsed["taken_at"].replace("Z", "+00:00"))
        except ValueError:
            return None

    def _transition_abandoned(
        self,
        directory: str,
        partial: str,
        record: dict[str, Any],
        now: datetime,
        reason: str,
    ) -> None:
        record["state"] = STAGING_STATE_ABANDONED
        record["updated_at"] = now.isoformat()
        record["last_failure"] = f"{reason}: {record['last_failure']}".strip(": ")
        self._write_staging_state(directory, partial, record)
        self._best_effort_remove(f"{directory}/{partial}")
        self._remove_staging_state(directory, partial)

    # ------------------------------------------------------------------ sweep
    def _sweep_stale_partials(
        self,
        directory: str,
        cluster_name: str,
        *,
        now: datetime,
        staging_report: list[dict[str, str]],
    ) -> list[str]:
        """Remove staging files an earlier, killed run left behind.

        The age filter still lists only files older than
        ``PARTIAL_STALE_MINUTES`` that sit directly in the backup directory.
        What happens next depends on the sidecar state record, not the mtime:

        * a ``pending_publish`` record inside its republish window is kept,
          whatever its age;
        * a record past the window, or over its attempt budget, is first
          transitioned to ``abandoned`` and only then removed;
        * a partial with no readable record follows the legacy age sweep.
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
        removed: list[str] = []
        for line in str(listing.get("stdout") or "").splitlines():
            path = line.strip()
            if not path.endswith(".db.partial"):
                continue
            if path.rsplit("/", 1)[0] != directory.rstrip("/") or ".." in path:
                continue
            name = path.rsplit("/", 1)[-1]
            if not staging_name_belongs(name, cluster_name=cluster_name):
                continue
            record = self._read_staging_state(directory, name, cluster_name)
            if record is not None:
                decision = staging_state_decision(
                    record,
                    now=now,
                    window_hours=DEFAULT_STAGING_REPUBLISH_WINDOW_HOURS,
                    max_attempts=DEFAULT_STAGING_MAX_PUBLISH_ATTEMPTS,
                )
                if decision["action"] == "keep":
                    if record["state"] == STAGING_STATE_PENDING:
                        staging_report.append(
                            {"name": name, "directory": directory, "state": record["state"]}
                        )
                    continue
                self._transition_abandoned(
                    directory, name, record, now, reason=decision["reason"]
                )
                removed.append(name)
                staging_report.append(
                    {"name": name, "directory": directory, "state": STAGING_STATE_ABANDONED}
                )
                continue
            outcome = self._best_effort_remove(path)
            if outcome["ok"]:
                removed.append(name)
        return removed

    # ------------------------------------------------------------- republish
    def _republish_pending(
        self,
        directories: list[str],
        cluster_name: str,
        *,
        now: datetime,
        republished: list[dict[str, str]],
        staging_report: list[dict[str, str]],
        swept: list[str],
    ) -> None:
        """Republish verified staging files the previous run could not rename.

        Runs after the sweep, once per candidate directory. A pending record
        is renamed to its final name when no newer final file already exists.
        A successful republish clears the sidecar; a failed one bumps the
        attempt budget and abandons the record when the budget or the window
        runs out. Nothing here overwrites an existing final snapshot.
        """

        for directory in directories:
            entries = self._list_directory(directory)
            for entry in entries:
                name = entry["name"]
                if not name.endswith(".db.partial"):
                    continue
                if not staging_name_belongs(name, cluster_name=cluster_name):
                    continue
                record = self._read_staging_state(directory, name, cluster_name)
                if record is None or record["state"] != STAGING_STATE_PENDING:
                    continue
                decision = staging_state_decision(
                    record,
                    now=now,
                    window_hours=DEFAULT_STAGING_REPUBLISH_WINDOW_HOURS,
                    max_attempts=DEFAULT_STAGING_MAX_PUBLISH_ATTEMPTS,
                )
                if decision["action"] == "abandon":
                    self._transition_abandoned(
                        directory, name, record, now, reason=decision["reason"]
                    )
                    swept.append(name)
                    staging_report.append(
                        {"name": name, "directory": directory, "state": STAGING_STATE_ABANDONED}
                    )
                    continue

                final_name = record["final_name"]
                final_path = f"{directory}/{final_name}"
                record_stamp = self._stamp(record["final_name"])
                existing_finals = [
                    item["name"]
                    for item in entries
                    if not item["name"].endswith(".db.partial")
                ]
                newer_exists = False
                for existing_name in existing_finals:
                    existing_stamp = self._stamp(existing_name)
                    if (
                        existing_stamp is not None
                        and record_stamp is not None
                        and existing_stamp >= record_stamp
                    ):
                        newer_exists = True
                        break
                if newer_exists:
                    self._transition_abandoned(
                        directory,
                        name,
                        record,
                        now,
                        reason="final_exists_newer",
                    )
                    swept.append(name)
                    staging_report.append(
                        {
                            "name": name,
                            "directory": directory,
                            "state": STAGING_STATE_ABANDONED,
                        }
                    )
                    continue

                outcome = self._runner(
                    self._pod_shell(["mv", f"{directory}/{name}", final_path]),
                    timeout=STEP_TIMEOUT_SECONDS,
                )
                if _ok(outcome):
                    record["state"] = STAGING_STATE_PUBLISHED
                    record["updated_at"] = now.isoformat()
                    self._write_staging_state(directory, name, record)
                    self._remove_staging_state(directory, name)
                    republished.append({"name": final_name, "directory": directory})
                    staging_report.append(
                        {"name": name, "directory": directory, "state": STAGING_STATE_PUBLISHED}
                    )
                    continue

                record["attempts"] += 1
                record["last_failure"] = _output(outcome) or "publish rename failed"
                record["updated_at"] = now.isoformat()
                retry = staging_state_decision(
                    record,
                    now=now,
                    window_hours=DEFAULT_STAGING_REPUBLISH_WINDOW_HOURS,
                    max_attempts=DEFAULT_STAGING_MAX_PUBLISH_ATTEMPTS,
                )
                if retry["action"] == "abandon":
                    self._transition_abandoned(
                        directory, name, record, now, reason=retry["reason"]
                    )
                    swept.append(name)
                    staging_report.append(
                        {
                            "name": name,
                            "directory": directory,
                            "state": STAGING_STATE_ABANDONED,
                        }
                    )
                else:
                    self._write_staging_state(directory, name, record)
                    staging_report.append(
                        {"name": name, "directory": directory, "state": STAGING_STATE_PENDING}
                    )

    # ------------------------------------------------------------------ run
    def run(
        self,
        request: dict[str, Any],
        *,
        now: datetime | None = None,
        lease_lost_event: Any = None,
    ) -> dict[str, Any]:
        """Take one snapshot, verify it, publish it, then prune old ones."""

        self._lease_lost_event = lease_lost_event
        stamp_now = now or datetime.now(UTC)
        directory = request["directory"]
        directories = request.get("directories") or [directory]
        # One protect variable for the whole run: both the prune decision and
        # the post-run assessment read this same tuple, instead of each
        # parsing the request payload again and drifting apart.
        protect = tuple(request.get("protect") or ())
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
        republished: list[dict[str, str]] = []
        staging_report: list[dict[str, str]] = []
        for entry_directory in directories:
            swept.extend(
                self._sweep_stale_partials(
                    entry_directory,
                    request["cluster_name"],
                    now=stamp_now,
                    staging_report=staging_report,
                )
            )
        self._republish_pending(
            directories,
            request["cluster_name"],
            now=stamp_now,
            republished=republished,
            staging_report=staging_report,
            swept=swept,
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
        verdict = self._disk_verdict(directory, request, directories)
        disk["detail"] = verdict["detail"]
        if not verdict["ok"]:
            return self._failed(
                verdict["code"], directory, request, verdict=verdict
            )
        disk["status"] = "succeeded"
        disk["exit_code"] = 0

        if self._lease_lost():
            return self._failed(
                COMMAND_LEASE_LOST_DURING_EXECUTION,
                directory,
                request,
                verdict=verdict,
                swept_partials=swept,
                republished=republished,
                staging=staging_report,
            )

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
            return self._failed(
                "ETCD_BACKUP_SAVE_FAILED",
                directory,
                request,
                swept_partials=swept,
                republished=republished,
                staging=staging_report,
            )

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
            return self._failed(
                "ETCD_BACKUP_VERIFY_FAILED",
                directory,
                request,
                swept_partials=swept,
                republished=republished,
                staging=staging_report,
            )

        # Post-write recheck: the new file has consumed its space, so the
        # remaining free floor is measured again instead of assumed from the
        # pre-write verdict. A zero floor stays measure-only.
        post_write = self._post_write_check(
            directory, request.get("min_free_bytes", 0)
        )
        if not post_write["ok"]:
            self._best_effort_remove(staging)
            return self._failed(
                post_write["code"],
                directory,
                request,
                post_write=post_write,
                swept_partials=swept,
                republished=republished,
                staging=staging_report,
            )

        if self._lease_lost():
            # The verified staging file is kept with a pending-publish record so
            # the next run can republish it; nothing is deleted or overwritten.
            record = self._staging_record(
                cluster_name=request["cluster_name"],
                partial=partial_name(filename),
                final_name=filename,
                directory=directory,
                now=stamp_now,
                state=STAGING_STATE_PENDING,
                attempts=1,
                last_failure="lease lost before publish",
            )
            self._write_staging_state(directory, partial_name(filename), record)
            staging_report.append(
                {
                    "name": partial_name(filename),
                    "directory": directory,
                    "state": STAGING_STATE_PENDING,
                }
            )
            return self._failed(
                COMMAND_LEASE_LOST_DURING_EXECUTION,
                directory,
                request,
                staging_kept=staging,
                swept_partials=swept,
                republished=republished,
                staging=staging_report,
            )

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
            # deleted. The verify-time state record is what protects it from
            # the age sweep and lets the next run republish it.
            record = self._staging_record(
                cluster_name=request["cluster_name"],
                partial=partial_name(filename),
                final_name=filename,
                directory=directory,
                now=stamp_now,
                state=STAGING_STATE_PENDING,
                attempts=1,
                last_failure=_output(result) or "publish rename failed",
            )
            self._write_staging_state(directory, partial_name(filename), record)
            staging_report.append(
                {
                    "name": partial_name(filename),
                    "directory": directory,
                    "state": STAGING_STATE_PENDING,
                }
            )
            return self._failed(
                "ETCD_BACKUP_PUBLISH_FAILED",
                directory,
                request,
                staging_kept=staging,
                swept_partials=swept,
                republished=republished,
                staging=staging_report,
            )

        if self._lease_lost():
            # The snapshot is already published; retention must not run under
            # a lost lease. The new final stays, nothing old is pruned.
            return self._failed(
                COMMAND_LEASE_LOST_DURING_EXECUTION,
                directory,
                request,
                backup={
                    "name": filename,
                    "path": final,
                    "verified": True,
                    "taken_at": stamp_now.isoformat(),
                },
                swept_partials=swept,
                republished=republished,
                staging=staging_report,
            )

        entries = self._union_entries(directories, request["cluster_name"])
        prunable = select_prunable(
            entries,
            keep_count=request["keep_count"],
            keep_max_age_hours=request["keep_max_age_hours"],
            pre_upgrade_keep_max_age_hours=request["pre_upgrade_keep_max_age_hours"],
            now=stamp_now,
            protect=protect,
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
            protect=protect,
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
            "disk": {
                "free_bytes": verdict.get("free_bytes"),
                "occupancy_bytes": verdict.get("staging_bytes", 0),
                "required_bytes": verdict.get("required_bytes"),
                "verdict": verdict["ok"],
                "code": verdict["code"],
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
            "republished": republished,
            "staging": staging_report,
            "cleanup_failures": list(self._cleanup_failures),
        }
        return result

    def status(self, request: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
        """Read-only: what backups exist and whether the newest one is fresh."""

        directories = request.get("directories") or [request["directory"]]
        entries = self._union_entries(directories, request["cluster_name"])
        protect = tuple(request.get("protect") or ())
        report = assess(
            entries,
            keep_count=request["keep_count"],
            keep_max_age_hours=request["keep_max_age_hours"],
            pre_upgrade_keep_max_age_hours=request["pre_upgrade_keep_max_age_hours"],
            max_age_hours=request["max_age_hours"],
            now=now,
            protect=protect,
        )
        return {
            "cluster_name": request["cluster_name"],
            "directory": request["directory"],
            "entries": public_entries(entries),
            **report,
        }

    # ------------------------------------------------------------------ helpers
    def _lease_lost(self) -> bool:
        return (
            self._lease_lost_event is not None
            and self._lease_lost_event.is_set()
        )

    def _best_effort_remove(self, path: str) -> dict[str, Any]:
        """Remove a staging file or sidecar, reporting the outcome.

        A failed removal is recorded in ``cleanup_failures`` instead of being
        swallowed: the file stays on disk, so the next run's sweep retries it.
        A successful removal does not change the ``swept_partials`` semantics,
        which still list only names this run actually deleted.
        """

        outcome = self._runner(
            self._pod_shell(["rm", "-f", path]), timeout=60
        )
        if _ok(outcome):
            return {"ok": True, "error": ""}
        error = _output(outcome) or "rm failed"
        self._cleanup_failures.append({"path": path, "error": error})
        return {"ok": False, "error": error}

    def _staging_occupancy(self, directories: list[str], cluster_name: str) -> int:
        """Bytes held by this cluster's in-flight staging files across dirs."""

        entries: list[dict[str, Any]] = []
        for entry_directory in directories:
            entries.extend(self._list_directory(entry_directory))
        return staging_occupancy(entries, cluster_name=cluster_name)

    def _disk_verdict(
        self,
        directory: str,
        request: dict[str, Any],
        directories: list[str],
    ) -> dict[str, Any]:
        """Ask the live database its size, the staging occupancy and free space.

        Both answers come from the etcd pod: the size from ``endpoint status``
        and the free space from ``df``. If either cannot be read the run does
        not guess - a snapshot written into a nearly full volume is worse than a
        snapshot that was skipped and reported. The verdict also counts bytes
        already held by in-flight ``*.db.partial`` files, so the floor is
        judged against the volume as it will be after this write.
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
                "staging_bytes": 0,
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
                "staging_bytes": 0,
                "required_bytes": None,
            }
        occupancy = self._staging_occupancy(directories, request["cluster_name"])
        return disk_verdict(
            db_size,
            free,
            min_free_bytes=request.get("min_free_bytes", 0),
            staging_bytes=occupancy,
        )

    def _post_write_check(self, directory: str, floor: int) -> dict[str, Any]:
        """Re-measure free space after the snapshot file has been written.

        The pre-write verdict assumed the new file's size from the database
        size; this check confirms the volume still keeps its reserved floor
        afterwards. A zero floor stays measure-only and never refuses.
        """

        free = self._free_bytes(directory)
        if free is None:
            return {
                "ok": False,
                "code": "ETCD_BACKUP_DISK_CHECK_FAILED",
                "free_bytes": None,
                "required_bytes": None,
            }
        if floor > 0 and free < floor:
            return {
                "ok": False,
                "code": "ETCD_BACKUP_DISK_POST_WRITE_LOW",
                "free_bytes": free,
                "required_bytes": floor,
            }
        return {"ok": True, "free_bytes": free, "required_bytes": floor}

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
            "swept_partials": [],
            "republished": [],
            "staging": [],
            "cleanup_failures": list(self._cleanup_failures),
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
