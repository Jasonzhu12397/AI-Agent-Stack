"""Bring the console up whenever a codespace is attached.

Codespace lifecycle commands only run once a client connects, so this script is
the entry point that actually guarantees the environment: it prepares the
environment if it is missing, then (re)starts the servers and reports health.
Every step is idempotent, which makes repeated attaches cheap.
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


def main() -> None:
    # Keep the inherited environment: git and gh need HOME and PATH to work.
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    if not DB_PATH.exists():
        print("preparing environment (first run)", flush=True)
        subprocess.run(
            [sys.executable, str(HERE / "bootstrap.py")],
            cwd=str(REPO_ROOT),
            env=env,
            check=True,
        )
    subprocess.run(
        [sys.executable, str(HERE / "start.py")], cwd=str(REPO_ROOT), env=env, check=True
    )


if __name__ == "__main__":
    main()
