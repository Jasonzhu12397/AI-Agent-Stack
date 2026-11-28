"""Serve the native console inside a Codespace.

The Vue dev server owns the forwarded port so the browser keeps one origin.
Every /api and /health request is proxied to the Rust API on localhost:8001,
which is the only backend: there is no Python HTTP process left.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RUST_API = REPO_ROOT / "apps" / "rust-api"
WEB_STUDIO = REPO_ROOT / "apps" / "web-studio"
DATA_DIR = REPO_ROOT / ".devdata"
PID_FILE = DATA_DIR / "servers.json"
API_PORT = 8001
WEB_PORT = 8000
BINARY = RUST_API / "target" / "release" / "ai-agent-stack-api"


def log(message: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(message, flush=True)
    with (DATA_DIR / "start.log").open("a") as handle:
        handle.write(message + "\n")


def stop_previous() -> None:
    if not PID_FILE.exists():
        return
    try:
        pids = json.loads(PID_FILE.read_text())
    except ValueError:
        return
    for pid in pids:
        if not pid:
            continue
        try:
            os.killpg(int(pid), signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            continue
    time.sleep(1.0)
    PID_FILE.unlink(missing_ok=True)


def spawn(command: list[str], *, cwd: Path, log_name: str, env: dict) -> int:
    handle = (DATA_DIR / log_name).open("ab")
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env={**os.environ, **env},
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log(f"started {' '.join(command[:2])} (pid {process.pid})")
    return process.pid


def ensure_binary() -> Path:
    if BINARY.exists():
        return BINARY
    cargo = shutil.which("cargo")
    if cargo is None:
        raise RuntimeError("cargo is missing; run .devcontainer/bootstrap.py first")
    log("building the Rust API")
    subprocess.run(
        [cargo, "build", "--release", "--locked"],
        cwd=str(RUST_API),
        check=True,
    )
    return BINARY


def main() -> None:
    stop_previous()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    binary = ensure_binary()
    api_env = {
        "DATABASE_URL": f"sqlite:///{DATA_DIR / 'console.db'}?mode=rwc",
        "MEDIA_ROOT": str(DATA_DIR / "media"),
        "SECRET_KEY": os.getenv("SECRET_KEY", "codespace-demo-secret-key-not-for-production-use"),
        "RUST_API_LISTEN": f"127.0.0.1:{API_PORT}",
        "RUST_API_NATIVE_IDENTITY": "true",
        "RUST_API_SECURE_COOKIES": "false",
        "RUST_LOG": os.getenv("RUST_LOG", "info"),
    }
    api = spawn([str(binary)], cwd=RUST_API, log_name="api.log", env=api_env)
    web_env = {
        "VITE_API_PROXY_TARGET": f"http://127.0.0.1:{API_PORT}",
        "VITE_API_BASE_URL": "",
    }
    web = spawn(
        ["npm", "run", "dev", "--", "--port", str(WEB_PORT), "--strictPort"],
        cwd=WEB_STUDIO,
        log_name="web.log",
        env=web_env,
    )
    PID_FILE.write_text(json.dumps([api, web]))
    log(f"console ready: http://0.0.0.0:{WEB_PORT}/")
    log(f"Rust API: http://127.0.0.1:{API_PORT}/")


if __name__ == "__main__":
    main()
