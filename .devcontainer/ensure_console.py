"""Bring the console up whenever a codespace is attached.

Codespace lifecycle commands only run once a client connects, so this script is
the entry point that actually guarantees the environment: it syncs the checkout
to the deployed branch, prepares the environment when the commit changed, then
(re)starts the servers and reports health. Every step is idempotent, so repeated
attaches are cheap.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
DATA_DIR = REPO_ROOT / ".devdata"
DB_PATH = DATA_DIR / "console.db"
BUILT_COMMIT = DATA_DIR / "built-commit"


def run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=str(REPO_ROOT), check=check, capture_output=True)


def head_commit() -> str:
    result = run(["git", "rev-parse", "HEAD"])
    return result.stdout.decode().strip()


def sync_checkout() -> None:
    """Follow main so the address always serves the deployed commit.

    A silent fetch failure used to leave the checkout on an older commit while
    the marker still matched, so the console served a stale bundle. The retry
    makes a transient network error visible instead of invisible.
    """

    for attempt in range(2):
        fetched = run(["git", "fetch", "-q", "origin"], check=False)
        if fetched.returncode == 0:
            break
        print(f"fetch attempt {attempt + 1} failed: {fetched.stderr.decode()[-400:]}", flush=True)
    reset = run(["git", "reset", "-q", "--hard", "origin/main"], check=False)
    if reset.returncode != 0:
        print(f"reset to origin/main failed: {reset.stderr.decode()[-400:]}", flush=True)
    print(f"console checkout at {head_commit()}", flush=True)


def main() -> None:
    # Keep the inherited environment: git and gh need HOME and PATH to work.
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    if (REPO_ROOT / ".git").exists():
        sync_checkout()

    commit = head_commit() if (REPO_ROOT / ".git").exists() else ""
    # A deploy is a deliberate act, so the environment is prepared every time
    # instead of trusting a marker: a marker left over from a commit whose
    # bundle was never rebuilt is exactly how a stale console happens.
    print("preparing environment", flush=True)
    subprocess.run(
        [sys.executable, str(HERE / "bootstrap.py")],
        cwd=str(REPO_ROOT),
        env=env,
        check=True,
    )
    BUILT_COMMIT.parent.mkdir(parents=True, exist_ok=True)
    BUILT_COMMIT.write_text(commit + "\n")

    subprocess.run(
        [sys.executable, str(HERE / "start.py")], cwd=str(REPO_ROOT), env=env, check=True
    )


if __name__ == "__main__":
    main()
