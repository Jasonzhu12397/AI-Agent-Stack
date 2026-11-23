"""Disk capacity accounting, cleanup failure reporting, and name uniqueness.

Three root-cause gaps pinned here:

* A2: the pre-write disk verdict ignored bytes already held by in-flight
  ``*.db.partial`` files, so a volume that fit the database alone could be
  refused only after a half-written snapshot filled it. The verdict now counts
  staging occupancy, reports free / occupancy / required, and rechecks the free
  floor after the write.
* B2: ``_best_effort_remove`` swallowed a failed ``rm``; a partial the sweep
  could not delete vanished from the report and was retried only by accident.
  Removal failures are now reported with path and error and left on disk for
  the next sweep.
* D2: the final name carried a second-resolution UTC stamp only, so two runs
  of the same cluster in the same second collided, and the publish rename
  silently overwrote an existing final file. Staging paths carry a per-run id,
  the publish side picks a non-colliding final name, and an existing final is
  never overwritten.
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
    backup_filename,
    disk_verdict,
    parse_backup_name,
    partial_name,
    staging_name_belongs,
    staging_occupancy,
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

    Mirrors the staging-state test double: the executor's own decisions are
    exercised, while argv interpretation talks to the local filesystem.
    """

    def __init__(
        self,
        root: str,
        *,
        fail_rm: bool = False,
        db_size: int = 8 * 1024 * 1024,
        free_bytes: int = 4 * 1024 * 1024 * 1024,
    ) -> None:
        self.root = Path(root)
        self.calls: list[list[str]] = []
        self.fail_rm = fail_rm
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
            dash_c = rest.index("-c")
            body = rest[dash_c + 3]
            target = self._fs(rest[dash_c + 4])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
            return ok()
        if rest and rest[0] == "rm":
            if self.fail_rm:
                return fail("rm: cannot remove: permission denied")
            for arg in rest[1:]:
                if arg == "-f":
                    continue
                target = self._fs(arg)
                if target.exists():
                    target.unlink()
            return ok()
        if rest and rest[0] == "mv":
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
            target = self._fs(rest[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"snapshot")
            return ok()
        if "snapshot" in rest and "status" in rest:
            return ok()
        return ok()


# ------------------------------------------------------------------- A2


def test_verdict_counts_in_flight_staging_occupancy():
    # 100 bytes free, a 100 byte database, 50 bytes already held by a partial:
    # the database alone fits, database plus the held staging does not.
    enough = disk_verdict(100, 150, min_free_bytes=0, staging_bytes=50)
    short = disk_verdict(100, 100, min_free_bytes=0, staging_bytes=50)
    assert enough["ok"] is True
    assert enough["required_bytes"] == 150
    assert short["ok"] is False
    assert short["code"] == "ETCD_BACKUP_DISK_LOW"
    assert short["staging_bytes"] == 50
    assert short["required_bytes"] == 150


def test_verdict_zero_floor_still_measures_but_does_not_reserve_floor():
    verdict = disk_verdict(100, 100, min_free_bytes=0, staging_bytes=0)
    assert verdict["ok"] is True
    assert verdict["required_bytes"] == 100
    assert verdict["min_free_bytes"] == 0


def test_staging_occupancy_sums_this_clusters_partials_only():
    entries = [
        {"name": "lab-20261122T020000Z.db.partial", "size_bytes": 500},
        {"name": "lab-20261122T010000Z.db", "size_bytes": 4096},
        {"name": "other-20261122T020000Z.db.partial", "size_bytes": 700},
        {"name": "lab-20261122T020001Z.db.partial", "size_bytes": None},
    ]
    assert staging_occupancy(entries, cluster_name="lab") == 500


def test_executor_refuses_when_free_omits_existing_partial_bytes(tmp_path):
    staging_dir = _fs(tmp_path, DEFAULT_DIRECTORY)
    staging_dir.mkdir(parents=True, exist_ok=True)
    partial = staging_dir / partial_name(backup_filename("lab", now=NOW - timedelta(hours=1)))
    partial.write_bytes(b"x" * 500)

    runner = FsRunner(
        str(tmp_path),
        db_size=1000,
        free_bytes=1000,
    )
    result = EtcdBackupExecutor(runner).run(_execute_request(min_free_bytes=0), now=NOW)

    assert result["failure_code"] == "ETCD_BACKUP_DISK_LOW"
    verdict = result["verdict"]
    assert verdict["staging_bytes"] == 500
    assert verdict["required_bytes"] == 1500
    assert verdict["free_bytes"] == 1000
    assert not [call for call in runner.calls if "snapshot" in call and "save" in call]
    assert partial.exists(), "the in-flight partial must not be touched by a refused run"


def test_successful_run_reports_the_disk_block_with_occupancy(tmp_path):
    runner = FsRunner(str(tmp_path), db_size=1000, free_bytes=4096)
    result = EtcdBackupExecutor(runner).run(_execute_request(min_free_bytes=0), now=NOW)

    assert result["failure_code"] is None
    disk = result["disk"]
    assert disk["free_bytes"] == 4096
    assert disk["occupancy_bytes"] == 0
    assert disk["required_bytes"] == 1000
    assert disk["verdict"] is True


# ------------------------------------------------------------------- B2


def test_failed_cleanup_is_reported_and_left_for_the_next_sweep(tmp_path):
    stale_name = partial_name(backup_filename("lab", now=NOW - timedelta(hours=30)))
    stale = _fs(tmp_path, DEFAULT_DIRECTORY) / stale_name
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"half written")
    _backdate(stale)

    runner = FsRunner(str(tmp_path), fail_rm=True)
    result = EtcdBackupExecutor(runner).run(_execute_request(), now=NOW)

    assert stale.exists(), "a removal that failed must not be reported as swept"
    assert result["swept_partials"] == []
    failures = result["cleanup_failures"]
    assert len(failures) == 1
    assert failures[0]["path"].endswith(stale_name)
    assert "permission denied" in failures[0]["error"]


# ------------------------------------------------------------------- D2


def test_two_runs_same_second_get_distinct_names_and_staging_paths():
    first = backup_filename("lab", now=NOW, run_id="aaaaaaaa")
    second = backup_filename("lab", now=NOW, run_id="bbbbbbbb")
    assert first != second
    assert partial_name(first) != partial_name(second)
    for name in (first, second):
        parsed = parse_backup_name(name, cluster_name="lab")
        assert parsed is not None
        assert parsed["stamp"] == "20261122T030000Z"


def test_legacy_filename_stays_parseable_without_a_run_id():
    name = backup_filename("lab", now=NOW)
    assert name == "lab-20261122T030000Z.db"
    parsed = parse_backup_name(name, cluster_name="lab")
    assert parsed is not None
    assert parsed["run_id"] is None


def test_publish_never_overwrites_an_existing_final(tmp_path):
    staging_dir = _fs(tmp_path, DEFAULT_DIRECTORY)
    staging_dir.mkdir(parents=True, exist_ok=True)
    base = backup_filename("lab", now=NOW)
    (staging_dir / base).write_bytes(b"already published")

    runner = FsRunner(str(tmp_path))
    result = EtcdBackupExecutor(runner).run(_execute_request(), now=NOW)

    assert result["failure_code"] is None
    assert (staging_dir / base).read_bytes() == b"already published"
    final = staging_dir / result["backup"]["name"]
    assert final.name != base
    assert final.read_bytes() == b"snapshot"


def test_two_executor_runs_same_second_keep_both_finals(tmp_path):
    first_runner = FsRunner(str(tmp_path))
    first = EtcdBackupExecutor(first_runner).run(_execute_request(), now=NOW)

    second_runner = FsRunner(str(tmp_path))
    second = EtcdBackupExecutor(second_runner).run(_execute_request(), now=NOW)

    assert first["failure_code"] is None
    assert second["failure_code"] is None
    assert first["backup"]["name"] != second["backup"]["name"]
    staging_dir = _fs(tmp_path, DEFAULT_DIRECTORY)
    finals = [p.name for p in staging_dir.iterdir() if p.name.endswith(".db")]
    assert sorted(finals) == sorted([first["backup"]["name"], second["backup"]["name"]])
    assert staging_name_belongs(
        partial_name(second["backup"]["name"]), cluster_name="lab"
    )
