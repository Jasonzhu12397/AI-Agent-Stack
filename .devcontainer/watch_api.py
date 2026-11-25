"""Keep the ASGI API alive after the one-shot launcher exits.

A Codespace deployment starts Daphne in its own session and returns. If Daphne
later exits, the forwarded port and the Cloudflare tunnel keep answering with a
proxy 502 while the console looks "deployed". This watchdog probes the local
readiness endpoint and restarts Daphne, so a crashed process becomes a short
outage instead of a permanently broken public address.
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

REPO_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_API = REPO_ROOT / "apps" / "platform-api"
CHECK_SECONDS = 10
FAILURES_BEFORE_RESTART = 3


def _alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health/ready", timeout=5
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def _replace_api_pid(pid_file: Path, old_pid: int, new_pid: int) -> None:
    try:
        pids = json.loads(pid_file.read_text())
    except (FileNotFoundError, ValueError):
        pids = []
    if not isinstance(pids, list):
        pids = []
    replaced = [new_pid if int(item) == old_pid else item for item in pids if item]
    if old_pid not in [int(item) for item in pids if item]:
        replaced.append(new_pid)
    pid_file.write_text(json.dumps(replaced))


def _start(port: int, log_path: Path) -> int:
    handle = log_path.open("ab")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "daphne",
            "-b",
            "0.0.0.0",
            "-p",
            str(port),
            "platform_api.asgi:application",
        ],
        cwd=str(PLATFORM_API),
        env=os.environ.copy(),
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return process.pid


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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-pid", type=int, required=True)
    parser.add_argument("--pid-file", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    api_pid = args.api_pid
    failures = 0
    while True:
        time.sleep(CHECK_SECONDS)
        if not _alive(api_pid) or not _ready(args.port):
            failures += 1
            print(
                f"api unhealthy (pid={api_pid} alive={_alive(api_pid)} "
                f"failures={failures})",
                flush=True,
            )
            if failures < FAILURES_BEFORE_RESTART:
                continue
            _stop(api_pid)
            new_pid = _start(args.port, args.log)
            _replace_api_pid(args.pid_file, api_pid, new_pid)
            print(f"restarted api pid {api_pid} -> {new_pid}", flush=True)
            api_pid = new_pid
            failures = 0
        else:
            failures = 0


if __name__ == "__main__":
    main()
