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
    """Follow main so the address always serves the deployed commit."""

    run(["git", "fetch", "-q", "origin"], check=False)
    run(["git", "reset", "-q", "--hard", "origin/main"], check=False)


def main() -> None:
    # Keep the inherited environment: git and gh need HOME and PATH to work.
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    if (REPO_ROOT / ".git").exists():
        sync_checkout()

    commit = head_commit() if (REPO_ROOT / ".git").exists() else ""
    prepared = (
        DB_PATH.exists() and BUILT_COMMIT.exists() and BUILT_COMMIT.read_text().strip() == commit
    )
    if not prepared:
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
