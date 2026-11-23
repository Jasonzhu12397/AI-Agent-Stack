"""Staging state machine: a verified snapshot that failed to publish.

A save that passed ``snapshot status`` but whose rename failed is still a
usable backup. Before this track it was kept under its ``.partial`` name with
no state record at all, so the 24h mtime sweep eventually deleted it and no
run ever republished it. These tests pin the state machine down:

* the verified-but-unpublished file carries a sidecar state record written at
  verify time;
* the sweep and the republish pass read that state, never the mtime alone;
* pending snapshots inside the republish window are kept and republished by
  the next run;
* past the window, or after the attempt limit, the record turns ``abandoned``
  and only then the file is removed, listed in the report;
* legacy partials without any state record still follow the age sweep;
* republish is idempotent and never overwrites a newer final file.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from target_agent.etcd_backup_exec import EtcdBackupExecutor

from packages.agent_contracts.etcd_backup import (
    DEFAULT_DIRECTORY,
    DEFAULT_STAGING_MAX_PUBLISH_ATTEMPTS,
    DEFAULT_STAGING_REPUBLISH_WINDOW_HOURS,
    STAGING_STATE_ABANDONED,
    STAGING_STATE_PENDING,
    STAGING_STATE_PUBLISHED,
    backup_filename,
    decode_staging_state,
    encode_staging_state,
    partial_name,
    staging_sidecar_name,
    staging_state_decision,
    validate_request,
)

NOW = datetime(2026, 11, 22, 3, 0, tzinfo=UTC)


def _execute_request(**overrides):
    payload = {
        "cluster_name": "lab",
        "execution_mode": "execute",
        "confirm_cluster_name": "lab",
    }
    payload.update(overrides)
    return validate_request(payload)


def _fs(root: str, pod_path: str) -> Path:
    return Path(root) / pod_path.lstrip("/")


def _backdate(path: Path, *, hours: float = 25) -> None:
    old = time.time() - hours * 3600
    os.utime(path, (old, old))


class FsRunner:
    """Answers the pod calls against a real temporary directory.

    The runner is a command stub: it interprets the same shell argv the
    executor sends, but it talks to ``root`` on the local filesystem instead
    of a pod. The executor's own decision logic is not mocked.
    """

    def __init__(
        self,
        root: str,
        *,
        fail_mv: bool = False,
        fail_save: bool = False,
        fail_status: bool = False,
        db_size: int = 8 * 1024 * 1024,
        free_bytes: int = 4 * 1024 * 1024 * 1024,
    ) -> None:
        self.root = Path(root)
        self.calls: list[list[str]] = []
        self.fail_mv = fail_mv
        self.fail_save = fail_save
        self.fail_status = fail_status
        self.db_size = db_size
        self.free_bytes = free_bytes

    def _fs(self, pod_path: str) -> Path:
        return self.root / str(pod_path).lstrip("/")

    def __call__(self, argv, timeout=None):
        self.calls.append(list(argv))
        try:
            dash = argv.index("--")
            rest = list(argv[dash + 1 :])
        except ValueError:
            rest = list(argv)

        def ok(stdout: str = ""):
            return {"available": True, "returncode": 0, "stdout": stdout, "stderr": ""}

        def fail(stderr: str = "boom"):
            return {"available": True, "returncode": 1, "stdout": "", "stderr": stderr}

        if "get" in argv and "pods" in argv:
            return ok(
                json.dumps(
                    {"items": [{"metadata": {"name": "etcd-cp-0"}, "status": {"phase": "Running"}}]}
                )
            )
        if rest and rest[0] == "mkdir":
            for directory in rest[1:]:
                if directory == "-p":
                    continue
                self._fs(directory).mkdir(parents=True, exist_ok=True)
            return ok()
        if rest and rest[0] == "ls":
            directory = rest[-1]
            lines = ["total 0"]
            listed = self._fs(directory)
            if listed.exists():
                for entry in sorted(listed.iterdir()):
                    if entry.is_file():
                        lines.append(
                            f"-rw-r--r-- 1 root root {entry.stat().st_size} "
                            f"Jan  1 00:00 {entry.name}"
                        )
            return ok("\n".join(lines))
        if rest and rest[0] == "find":
            directory = rest[1].rstrip("/")
            mmin = None
            for index, arg in enumerate(rest):
                if arg == "-mmin":
                    mmin = int(rest[index + 1].lstrip("+"))
            listed = self._fs(directory)
            out: list[str] = []
            if listed.exists():
                cutoff = time.time() - mmin * 60 if mmin is not None else 0
                for entry in sorted(listed.iterdir()):
                    if (
                        entry.is_file()
                        and entry.name.endswith(".db.partial")
                        and entry.stat().st_mtime <= cutoff
                    ):
                        out.append(f"{directory}/{entry.name}")
            return ok("\n".join(out))
        if rest and rest[0] == "cat":
            target = self._fs(rest[-1])
            if not target.exists():
                return fail("no such file")
            return ok(target.read_text())
        if rest and rest[0] == "sh":
            # sh -c 'printf "%s" "$1" > "$2"' sh <body> <path>
            dash_c = rest.index("-c")
            body = rest[dash_c + 3]
            target = self._fs(rest[dash_c + 4])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
            return ok()
        if rest and rest[0] == "rm":
            for arg in rest[1:]:
                if arg == "-f":
                    continue
                target = self._fs(arg)
                if target.exists():
                    target.unlink()
            return ok()
        if rest and rest[0] == "mv":
            if self.fail_mv:
                return fail("mv: rename failed")
            src, dst = self._fs(rest[1]), self._fs(rest[2])
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            return ok()
        if rest and rest[0] == "df":
            return ok(
                "Filesystem 1B-blocks Used Available Capacity Mounted on\n"
                f"/dev/sda1 {self.free_bytes + 10} 10 {self.free_bytes} 1% /var"
            )
        if "endpoint" in rest and "status" in rest:
            return ok(json.dumps([{"Header": {"dbSize": self.db_size}}]))
        if "snapshot" in rest and "save" in rest:
            if self.fail_save:
                return fail("snapshot save failed")
            target = self._fs(rest[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"snapshot")
            return ok()
        if "snapshot" in rest and "status" in rest:
            if self.fail_status:
                return fail("snapshot status failed")
            return ok()
        return ok()


# ---------------------------------------------------------------- contract


def test_state_record_round_trips_through_the_sidecar():
    record = {
        "cluster_name": "lab",
        "final_name": "lab-20261122T030000Z.db",
        "directory": DEFAULT_DIRECTORY,
        "verified_at": NOW.isoformat(),
        "state": STAGING_STATE_PENDING,
        "attempts": 1,
        "last_failure": "mv: rename failed",
        "updated_at": NOW.isoformat(),
    }
    decoded = decode_staging_state(encode_staging_state(record), cluster_name="lab")
    assert decoded == record


def test_decode_rejects_broken_json_a_foreign_cluster_and_bad_state():
    assert decode_staging_state("not json", cluster_name="lab") is None
    foreign = {
        "cluster_name": "other",
        "final_name": "other-20261122T030000Z.db",
        "directory": DEFAULT_DIRECTORY,
        "verified_at": NOW.isoformat(),
        "state": STAGING_STATE_PENDING,
        "attempts": 1,
        "last_failure": "",
        "updated_at": NOW.isoformat(),
    }
    assert decode_staging_state(encode_staging_state(foreign), cluster_name="lab") is None
    bad_state = {**foreign, "cluster_name": "lab", "state": "missing"}
    assert decode_staging_state(encode_staging_state(bad_state), cluster_name="lab") is None


def test_decision_keeps_pending_within_window_and_drops_after():
    record = {
        "cluster_name": "lab",
        "final_name": "lab-20261122T030000Z.db",
        "directory": DEFAULT_DIRECTORY,
        "verified_at": NOW.isoformat(),
        "state": STAGING_STATE_PENDING,
        "attempts": 1,
        "last_failure": "mv: rename failed",
        "updated_at": NOW.isoformat(),
    }
    inside = staging_state_decision(
        record,
        now=NOW + timedelta(hours=2),
        window_hours=DEFAULT_STAGING_REPUBLISH_WINDOW_HOURS,
        max_attempts=DEFAULT_STAGING_MAX_PUBLISH_ATTEMPTS,
    )
    assert inside == {"action": "keep", "reason": "within_window"}

    expired = staging_state_decision(
        record,
        now=NOW + timedelta(hours=25),
        window_hours=DEFAULT_STAGING_REPUBLISH_WINDOW_HOURS,
        max_attempts=DEFAULT_STAGING_MAX_PUBLISH_ATTEMPTS,
    )
    assert expired == {"action": "abandon", "reason": "window_expired"}

    exhausted = staging_state_decision(
        {**record, "attempts": DEFAULT_STAGING_MAX_PUBLISH_ATTEMPTS},
        now=NOW + timedelta(hours=1),
        window_hours=DEFAULT_STAGING_REPUBLISH_WINDOW_HOURS,
        max_attempts=DEFAULT_STAGING_MAX_PUBLISH_ATTEMPTS,
    )
    assert exhausted == {"action": "abandon", "reason": "attempts_exhausted"}


# ---------------------------------------------------------------- executor


def test_failed_publish_writes_a_pending_publish_sidecar(tmp_path):
    runner = FsRunner(str(tmp_path), fail_mv=True)
    result = EtcdBackupExecutor(runner).run(_execute_request(), now=NOW)

    assert result["failure_code"] == "ETCD_BACKUP_PUBLISH_FAILED"
    staging = result["staging_kept"]
    partial = _fs(tmp_path, staging)
    assert partial.exists()

    sidecar = partial.with_name(staging_sidecar_name(partial.name))
    assert sidecar.exists(), "the verified staging file must carry a state record"
    record = decode_staging_state(sidecar.read_text(), cluster_name="lab")
    assert record is not None
    assert record["state"] == STAGING_STATE_PENDING
    assert record["cluster_name"] == "lab"
    assert record["final_name"] == backup_filename("lab", now=NOW)
    assert record["directory"] == DEFAULT_DIRECTORY
    assert record["attempts"] == 1
    assert record["last_failure"]

    staged = next(item for item in result["staging"] if item["name"] == partial.name)
    assert staged["directory"] == DEFAULT_DIRECTORY
    assert staged["state"] == STAGING_STATE_PENDING


def test_pending_publish_survives_the_24h_age_sweep(tmp_path):
    first = FsRunner(str(tmp_path), fail_mv=True)
    EtcdBackupExecutor(first).run(_execute_request(), now=NOW)
    staging_dir = _fs(tmp_path, DEFAULT_DIRECTORY)
    partial = next(p for p in staging_dir.iterdir() if p.name.endswith(".partial"))
    sidecar = partial.with_name(staging_sidecar_name(partial.name))
    # Age the file past the legacy 24h stale threshold; the sidecar still
    # says the snapshot is verified and awaiting publish.
    _backdate(partial)
    _backdate(sidecar)

    second = FsRunner(str(tmp_path), fail_mv=True)
    result = EtcdBackupExecutor(second).run(
        _execute_request(), now=NOW + timedelta(hours=2)
    )

    assert partial.exists(), "a pending verified snapshot inside its window must not be swept"
    assert sidecar.exists()
    assert result["swept_partials"] == []
    record = decode_staging_state(sidecar.read_text(), cluster_name="lab")
    assert record["state"] == STAGING_STATE_PENDING


def test_next_run_republishes_a_pending_snapshot(tmp_path):
    first = FsRunner(str(tmp_path), fail_mv=True)
    EtcdBackupExecutor(first).run(_execute_request(), now=NOW)
    final_name = backup_filename("lab", now=NOW)
    staging_dir = _fs(tmp_path, DEFAULT_DIRECTORY)
    partial = next(p for p in staging_dir.iterdir() if p.name.endswith(".partial"))

    second = FsRunner(str(tmp_path))
    result = EtcdBackupExecutor(second).run(
        _execute_request(), now=NOW + timedelta(hours=1)
    )

    final = staging_dir / final_name
    assert final.exists()
    assert not partial.exists(), "the staging name must be gone after republish"
    assert not (staging_dir / staging_sidecar_name(partial.name)).exists()
    assert result["republished"] == [
        {"name": final_name, "directory": DEFAULT_DIRECTORY}
    ]
    published = next(
        item for item in result["staging"] if item["name"] == partial.name
    )
    assert published["state"] == STAGING_STATE_PUBLISHED


def test_pending_past_the_window_is_abandoned_then_removed_and_reported(tmp_path):
    first = FsRunner(str(tmp_path), fail_mv=True)
    EtcdBackupExecutor(first).run(_execute_request(), now=NOW)
    staging_dir = _fs(tmp_path, DEFAULT_DIRECTORY)
    partial = next(p for p in staging_dir.iterdir() if p.name.endswith(".partial"))
    sidecar = staging_dir / staging_sidecar_name(partial.name)

    second = FsRunner(str(tmp_path))
    result = EtcdBackupExecutor(second).run(
        _execute_request(), now=NOW + timedelta(hours=25)
    )

    assert not partial.exists()
    assert not sidecar.exists()
    assert partial.name in result["swept_partials"]
    assert result["republished"] == []
    abandoned = next(item for item in result["staging"] if item["name"] == partial.name)
    assert abandoned["state"] == STAGING_STATE_ABANDONED
    assert not (staging_dir / backup_filename("lab", now=NOW)).exists()


def test_repeated_publish_failures_exhaust_the_attempt_limit(tmp_path):
    request = _execute_request()
    runner = FsRunner(str(tmp_path), fail_mv=True)
    EtcdBackupExecutor(runner).run(request, now=NOW)
    staging_dir = _fs(tmp_path, DEFAULT_DIRECTORY)
    partial = next(p for p in staging_dir.iterdir() if p.name.endswith(".partial"))
    sidecar = partial.with_name(staging_sidecar_name(partial.name))

    runner = FsRunner(str(tmp_path), fail_mv=True)
    EtcdBackupExecutor(runner).run(request, now=NOW + timedelta(hours=1))
    record = decode_staging_state(sidecar.read_text(), cluster_name="lab")
    assert record["attempts"] == 2
    assert partial.exists()

    runner = FsRunner(str(tmp_path), fail_mv=True)
    result = EtcdBackupExecutor(runner).run(request, now=NOW + timedelta(hours=2))

    assert not partial.exists(), "the attempt limit must abandon the staging file"
    assert not sidecar.exists()
    abandoned = next(item for item in result["staging"] if item["name"] == partial.name)
    assert abandoned["state"] == STAGING_STATE_ABANDONED


def test_a_partial_without_state_still_follows_the_age_sweep(tmp_path):
    stale_name = partial_name(backup_filename("lab", now=NOW - timedelta(hours=30)))
    stale = _fs(tmp_path, DEFAULT_DIRECTORY) / stale_name
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"half written")
    _backdate(stale)

    runner = FsRunner(str(tmp_path))
    result = EtcdBackupExecutor(runner).run(_execute_request(), now=NOW)

    assert not stale.exists()
    assert result["swept_partials"] == [stale_name]
    assert result["republished"] == []
    assert not (_fs(tmp_path, DEFAULT_DIRECTORY) / staging_sidecar_name(stale_name)).exists()


def test_republish_never_overwrites_a_newer_final_file(tmp_path):
    first = FsRunner(str(tmp_path), fail_mv=True)
    EtcdBackupExecutor(first).run(_execute_request(), now=NOW)
    staging_dir = _fs(tmp_path, DEFAULT_DIRECTORY)
    partial = next(p for p in staging_dir.iterdir() if p.name.endswith(".partial"))

    newer_name = backup_filename("lab", now=NOW + timedelta(hours=1))
    newer = staging_dir / newer_name
    newer.write_bytes(b"newer verified snapshot")

    second = FsRunner(str(tmp_path))
    result = EtcdBackupExecutor(second).run(
        _execute_request(), now=NOW + timedelta(hours=2)
    )

    assert newer.read_bytes() == b"newer verified snapshot"
    assert not partial.exists()
    assert result["republished"] == []
    abandoned = next(item for item in result["staging"] if item["name"] == partial.name)
    assert abandoned["state"] == STAGING_STATE_ABANDONED
    moved = [call for call in second.calls if "mv" in call]
    assert not any(newer_name in arg for call in moved for arg in call)


def test_pending_in_a_retired_directory_is_republished_there(tmp_path):
    retired = "/var/lib/etcd/retired"
    stamp = NOW - timedelta(hours=3)
    final_name = backup_filename("lab", now=stamp)
    partial_name_staged = partial_name(final_name)
    retired_dir = _fs(tmp_path, retired)
    retired_dir.mkdir(parents=True, exist_ok=True)
    (retired_dir / partial_name_staged).write_bytes(b"snapshot")
    record = {
        "cluster_name": "lab",
        "final_name": final_name,
        "directory": retired,
        "verified_at": stamp.isoformat(),
        "state": STAGING_STATE_PENDING,
        "attempts": 1,
        "last_failure": "mv: rename failed",
        "updated_at": stamp.isoformat(),
    }
    (retired_dir / staging_sidecar_name(partial_name_staged)).write_text(
        encode_staging_state(record)
    )

    request = _execute_request(directories=[DEFAULT_DIRECTORY, retired])
    runner = FsRunner(str(tmp_path))
    result = EtcdBackupExecutor(runner).run(request, now=NOW)

    assert (retired_dir / final_name).exists()
    assert not (retired_dir / partial_name_staged).exists()
    assert result["republished"] == [{"name": final_name, "directory": retired}]
