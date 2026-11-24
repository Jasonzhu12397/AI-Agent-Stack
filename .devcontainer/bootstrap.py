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
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
PLATFORM_API = REPO_ROOT / "apps" / "platform-api"
WEB_STUDIO = REPO_ROOT / "apps" / "web-studio"
DATA_DIR = REPO_ROOT / ".devdata"


LOG_PATH = DATA_DIR / "bootstrap.log"


def log(message: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = f"{stamp} {message}"
    print(line, flush=True)
    with LOG_PATH.open("a") as handle:
        handle.write(line + "\n")


def report(stage: str, ok: bool = False) -> None:
    """Publish progress so a failing setup is diagnosable from outside."""

    try:
        sys.path.insert(0, str(HERE))
        import healthcheck

        healthcheck.publish(
            {
                "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "stage": stage,
                "ok": ok,
                "bootstrap_log": log_tail(),
            }
        )
    except Exception as exc:  # noqa: BLE001 - reporting must never break setup
        log(f"report failed: {type(exc).__name__}: {exc}")


def log_tail(lines: int = 15) -> list[str]:
    if not LOG_PATH.exists():
        return []
    return LOG_PATH.read_text(errors="replace").splitlines()[-lines:]


def run(command: list[str], *, cwd: Path, env: dict | None = None) -> None:
    log("+ " + " ".join(command))
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


def build_console() -> None:
    npm = shutil.which("npm")
    if npm is None:
        raise RuntimeError("npm is missing; the node feature did not install")
    run([npm, "exec", "--", "vite", "build"], cwd=WEB_STUDIO)


def migrate() -> None:
    run([sys.executable, "manage.py", "migrate", "--noinput"], cwd=PLATFORM_API, env=runtime_env())


def seed() -> None:
    env = runtime_env()
    env["PYTHONPATH"] = f"{PLATFORM_API}:{REPO_ROOT}"
    run([sys.executable, str(Path(__file__).with_name("seed_demo.py"))], cwd=REPO_ROOT, env=env)


def main() -> None:
    report("starting")
    install_python()
    report("python-deps")
    install_frontend()
    report("frontend-deps")
    build_console()
    report("built-console")
    migrate()
    report("migrated")
    seed()
    report("seeded")
    log("codespace prepared: run `python3 .devcontainer/start.py` to serve the console")


if __name__ == "__main__":
    main()
