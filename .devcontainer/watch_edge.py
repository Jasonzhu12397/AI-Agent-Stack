"""Keep the native Rust edge alive after the one-shot launcher exits.

The edge owns the public port, so a silent crash would leave the forwarded port
pointing at nothing while Django keeps running on its private port. This
watchdog probes the edge's native liveness route and restarts the binary, which
also keeps the pid file current for the next launcher run.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CHECK_SECONDS = 10
FAILURES_BEFORE_RESTART = 3


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _live(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health/live", timeout=5
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def _replace(pid_file: Path, old_pid: int, new_pid: int) -> None:
    try:
        pids = json.loads(pid_file.read_text())
    except (FileNotFoundError, ValueError):
        pids = []
    if not isinstance(pids, list):
        pids = []
    pids = [new_pid if int(item) == old_pid else item for item in pids if item]
    if old_pid not in [int(item) for item in json.loads(pid_file.read_text()) if item]:
        pids.append(new_pid)
    pid_file.write_text(json.dumps(pids))


def _stop(pid: int) -> None:
    if not _alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(20):
        if not _alive(pid):
            return
        time.sleep(0.25)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _start(binary: Path, log_path: Path) -> int:
    handle = log_path.open("ab")
    process = subprocess.Popen(
        [str(binary)],
        cwd=str(binary.parent.parent.parent),
        env=os.environ.copy(),
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return process.pid


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--pid-file", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    pid = args.pid
    failures = 0
    while True:
        time.sleep(CHECK_SECONDS)
        if _alive(pid) and _live(args.port):
            failures = 0
            continue
        failures += 1
        print(f"edge unhealthy (pid={pid} alive={_alive(pid)} failures={failures})", flush=True)
        if failures < FAILURES_BEFORE_RESTART:
            continue
        _stop(pid)
        new_pid = _start(args.binary, args.log)
        _replace(args.pid_file, pid, new_pid)
        print(f"restarted edge pid {pid} -> {new_pid}", flush=True)
        pid = new_pid
        failures = 0


if __name__ == "__main__":
    main()
