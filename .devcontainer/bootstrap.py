"""Prepare a Codespace so the web console opens with data already in place.

Runs once after the container is created: Python dependencies, the frontend
bundler, the SQLite database and a demo tenant. Every step is idempotent, so
re-running it after a pull only fills what is missing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_API = REPO_ROOT / "apps" / "platform-api"
WEB_STUDIO = REPO_ROOT / "apps" / "web-studio"
DATA_DIR = REPO_ROOT / ".devdata"


def run(command: list[str], *, cwd: Path, env: dict | None = None) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=str(cwd), env={**os.environ, **(env or {})}, check=True)


def runtime_env() -> dict:
    """Environment every management command needs in a Codespace."""

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return {
        "DJANGO_SETTINGS_MODULE": "platform_api.settings",
        "APP_ENV": "development",
        "DEBUG": "true",
        "SECRET_KEY": os.getenv("SECRET_KEY", "codespace-demo-secret-key-not-for-production-use"),
        "DATABASE_URL": f"sqlite:///{DATA_DIR / 'console.db'}",
        "PYTHONPATH": f"{PLATFORM_API}:{REPO_ROOT}",
        "ALLOWED_HOSTS": "*",
        "CSRF_TRUSTED_ORIGINS": (
            "https://*.app.github.dev,http://localhost:5173,http://127.0.0.1:5173"
        ),
    }


def install_python() -> None:
    run([sys.executable, "-m", "pip", "install", "--upgrade", "pip"], cwd=REPO_ROOT)
    run([sys.executable, "-m", "pip", "install", "-e", ".[dev]"], cwd=REPO_ROOT)


def install_frontend() -> None:
    npm = shutil.which("npm")
    if npm is None:
        raise RuntimeError("npm is missing; the node feature did not install")
    run([npm, "install", "--no-audit", "--no-fund"], cwd=WEB_STUDIO)


def migrate() -> None:
    run([sys.executable, "manage.py", "migrate", "--noinput"], cwd=PLATFORM_API, env=runtime_env())


def seed() -> None:
    env = runtime_env()
    env["PYTHONPATH"] = f"{PLATFORM_API}:{REPO_ROOT}"
    run([sys.executable, str(Path(__file__).with_name("seed_demo.py"))], cwd=REPO_ROOT, env=env)


def main() -> None:
    install_python()
    install_frontend()
    migrate()
    seed()
    print("codespace prepared: run `python3 .devcontainer/start.py` to serve the console")


if __name__ == "__main__":
    main()
