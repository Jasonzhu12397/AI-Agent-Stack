"""Lease guard for the mutating backup command.

The backup executor could run to completion without ever checking the command
lease: a lease lost mid-run would still publish a snapshot and then prune old
ones. The executor now accepts the same ``lease_lost_event`` the other mutating
actions use, checks it before saving, before publishing, and before pruning,
and stops with ``COMMAND_LEASE_LOST_DURING_EXECUTION`` without deleting or
overwriting anything once the lease is gone. The dispatch path in ``main.py``
forwards the event instead of dropping it.
"""

from __future__ import annotations

import json
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from target_agent import main as target_main
from target_agent.etcd_backup_exec import EtcdBackupExecutor

from packages.agent_contracts.etcd_backup import (
    DEFAULT_DIRECTORY,
    backup_filename,
    staging_sidecar_name,
    validate_request,
)

NOW = datetime(2026, 11, 22, 3, 0, tzinfo=UTC)
LEASE_LOST = "COMMAND_LEASE_LOST_DURING_EXECUTION"


def _execute_request(**overrides):
    payload = {
        "cluster_name": "lab",
        "execution_mode": "execute",
        "confirm_cluster_name": "lab",
    }
    payload.update(overrides)
    return validate_request(payload)


class HookedRunner:
    """Pod call stub over a temp dir, with a hook fired after chosen steps."""

    def __init__(self, root: str, *, hook: Any = None) -> None:
        self.root = Path(root)
        self.calls: list[list[str]] = []
        self.hook = hook
        self.db_size = 8 * 1024 * 1024
        self.free_bytes = 4 * 1024 * 1024 * 1024

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
            return ok("")
        if rest and rest[0] == "cat":
            target = self._fs(rest[-1])
            if not target.exists():
                return {"available": True, "returncode": 1, "stdout": "", "stderr": "no such file"}
            return ok(target.read_text())
        if rest and rest[0] == "sh":
            dash_c = rest.index("-c")
            target = self._fs(rest[dash_c + 4])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rest[dash_c + 3])
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
            src, dst = self._fs(rest[1]), self._fs(rest[2])
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            if self.hook:
                self.hook("publish")
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
            if self.hook:
                self.hook("save")
            return ok()
        if "snapshot" in rest and "status" in rest:
            if self.hook:
                self.hook("verify")
            return ok()
        return ok()


def test_lease_lost_before_save_stops_without_writing(tmp_path):
    event = threading.Event()
    event.set()
    runner = HookedRunner(str(tmp_path))
    result = EtcdBackupExecutor(runner).run(
        _execute_request(), now=NOW, lease_lost_event=event
    )

    assert result["failure_code"] == LEASE_LOST
    assert not [call for call in runner.calls if "snapshot" in call and "save" in call]
    assert not [call for call in runner.calls if "mv" in call]
    assert not [call for call in runner.calls if "rm" in call]


def test_lease_lost_before_publish_keeps_staging_and_pending_sidecar(tmp_path):
    event = threading.Event()

    def hook(step: str) -> None:
        if step == "verify":
            event.set()

    runner = HookedRunner(str(tmp_path), hook=hook)
    result = EtcdBackupExecutor(runner).run(
        _execute_request(), now=NOW, lease_lost_event=event
    )

    assert result["failure_code"] == LEASE_LOST
    assert not [call for call in runner.calls if "mv" in call]
    staging_dir = _fs(tmp_path, DEFAULT_DIRECTORY)
    partials = [p for p in staging_dir.iterdir() if p.name.endswith(".partial")]
    assert len(partials) == 1
    sidecar = staging_dir / staging_sidecar_name(partials[0].name)
    assert sidecar.exists(), "verified staging must keep a pending state record"
    record = json.loads(sidecar.read_text())
    assert record["state"] == "pending_publish"


def test_lease_lost_before_prune_keeps_the_published_snapshot(tmp_path):
    event = threading.Event()

    def hook(step: str) -> None:
        if step == "publish":
            event.set()

    runner = HookedRunner(str(tmp_path), hook=hook)
    result = EtcdBackupExecutor(runner).run(
        _execute_request(), now=NOW, lease_lost_event=event
    )

    assert result["failure_code"] == LEASE_LOST
    staging_dir = _fs(tmp_path, DEFAULT_DIRECTORY)
    finals = [p for p in staging_dir.iterdir() if p.name.endswith(".db")]
    assert len(finals) == 1
    assert not [call for call in runner.calls if "rm" in call]


def test_dispatch_forwards_the_lease_event_to_the_executor(monkeypatch):
    captured: dict[str, Any] = {}

    class FakeExecutor:
        def __init__(self, runner):
            captured["runner"] = runner

        def run(self, request, **kwargs):
            captured["kwargs"] = kwargs
            return {"failure_code": None, "backup": {"verified": True}, "steps": []}

    monkeypatch.setattr(target_main, "EtcdBackupExecutor", FakeExecutor)
    event = threading.Event()
    command = {
        "action": target_main.BACKUP_ACTION,
        "payload": {
            "cluster_name": "lab",
            "execution_mode": "execute",
            "confirm_cluster_name": "lab",
        },
    }

    outcome = target_main.execute(command, lease_lost_event=event)

    assert outcome["status"] == "succeeded"
    assert captured["kwargs"]["lease_lost_event"] is event


def _fs(root: str, pod_path: str) -> Path:
    return Path(root) / pod_path.lstrip("/")


def test_backup_filename_reference_kept_for_sidecar_consistency():
    # Sanity: the contract name helper the sidecar record stores still matches
    # the executor's final name convention used in these lease tests.
    assert backup_filename("lab", now=NOW) == "lab-20261122T030000Z.db"
