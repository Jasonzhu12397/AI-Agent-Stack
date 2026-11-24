"""Serve the console inside a Codespace.

Starts the ASGI API on 8000 bound to 0.0.0.0 so the forwarded port works. The
API also serves the built web console, so a deployment needs exactly one public
port and one origin: no CORS, no second runtime.

Every start kills what a previous start left behind, so restarting the
container never ends up with two servers fighting over a port.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_API = REPO_ROOT / "apps" / "platform-api"
WEB_STUDIO = REPO_ROOT / "apps" / "web-studio"
DATA_DIR = REPO_ROOT / ".devdata"
PID_FILE = DATA_DIR / "servers.json"

API_PORT = 8000
WEB_PORT = API_PORT


def runtime_env() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return {
        "DJANGO_SETTINGS_MODULE": "platform_api.settings",
        "APP_ENV": "development",
        "DEBUG": "true",
        "SECRET_KEY": os.getenv("SECRET_KEY", "codespace-demo-secret-key-not-for-production-use"),
        "DATABASE_URL": f"sqlite:///{DATA_DIR / 'console.db'}",
        "PYTHONPATH": f"{PLATFORM_API}:{REPO_ROOT}",
        "ALLOWED_HOSTS": "*",
        # Any origin that may front this container: the Codespaces port proxy,
        # the Cloudflare quick tunnel used when the proxy gate blocks XHRs, and
        # the local dev server.
        "CSRF_TRUSTED_ORIGINS": (
            "https://*.app.github.dev,https://*.trycloudflare.com,"
            "http://localhost:5173,http://127.0.0.1:5173"
        ),
        # Relative API calls go through the Vite proxy, so one origin serves all.
        "VITE_API_BASE_URL": "",
    }


def stop_previous() -> None:
    if not PID_FILE.exists():
        return
    import json

    try:
        pids = json.loads(PID_FILE.read_text())
    except ValueError:
        return
    for pid in pids:
        if not pid:  # a zero pid would signal the whole process group
            continue
        try:
            # Servers were spawned in their own session, so killing the group
            # also stops the node process npm would otherwise leave behind.
            os.killpg(int(pid), signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            continue
    time.sleep(1.0)
    PID_FILE.unlink(missing_ok=True)


def spawn(command: list[str], *, cwd: Path, log_name: str, env: dict) -> int:
    log_path = DATA_DIR / log_name
    handle = log_path.open("ab")
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env={**os.environ, **env},
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    print(f"started {' '.join(command[:2])} (pid {process.pid}) -> {log_path}", flush=True)
    return process.pid


def main() -> None:
    stop_previous()
    env = runtime_env()
    api = spawn(
        [
            sys.executable,
            "-m",
            "daphne",
            "-b",
            "0.0.0.0",
            "-p",
            str(API_PORT),
            "platform_api.asgi:application",
        ],
        cwd=PLATFORM_API,
        log_name="api.log",
        env=env,
    )
    # The API serves the built console bundle, so there is no second runtime.
    PID_FILE.write_text(json.dumps([api]))
    print(f"console ready: http://0.0.0.0:{API_PORT}/", flush=True)
    # Publish the self-check so the deployment is verifiable without shell access.
    subprocess.run(
        [sys.executable, str(Path(__file__).with_name("healthcheck.py"))],
        cwd=str(REPO_ROOT),
        env={**os.environ, **env},
        check=False,
    )


if __name__ == "__main__":
    main()
