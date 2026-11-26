"""Serve the console inside a Codespace.

Starts the Rust edge API on the forwarded port 8000 and the Django ASGI backend
on 8001. Every request the Rust edge owns natively is answered in Rust; everything
else is proxied to Django, which still renders the built console bundle. One
public port means one origin, so the browser keeps its session and CSRF cookies.

Every start kills what a previous start left behind, so restarting the
container never ends up with two servers fighting over a port.
"""

from __future__ import annotations

import json
import os
import re
import shutil
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
BACKEND_PORT = 8001
RUST_API = REPO_ROOT / "apps" / "rust-api"
EDGE_BINARY = RUST_API / "target" / "release" / "ai-agent-stack-api-edge"
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
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


def log(message: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(message, flush=True)
    with (DATA_DIR / "start.log").open("a") as handle:
        handle.write(message + "\n")


def _cargo() -> str | None:
    """Return a usable cargo path, installing Rust once when it is absent."""

    for candidate in (shutil.which("cargo"), str(Path.home() / ".cargo" / "bin" / "cargo")):
        if candidate and Path(candidate).exists():
            return candidate
    log("installing the Rust toolchain for the native edge")
    installer = subprocess.run(
        [
            "sh",
            "-c",
            "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | "
            "sh -s -- -y --profile minimal",
        ],
        capture_output=True,
        text=True,
    )
    if installer.returncode != 0:
        log(f"rust install failed: {installer.stderr[-400:]}")
        return None
    candidate = Path.home() / ".cargo" / "bin" / "cargo"
    return str(candidate) if candidate.exists() else None


def ensure_edge_binary() -> Path | None:
    """Build the native edge once; a missing toolchain keeps the old console alive."""

    if EDGE_BINARY.exists():
        return EDGE_BINARY
    cargo = _cargo()
    if cargo is None:
        return None
    log("building the native edge (this runs once per checkout)")
    build = subprocess.run(
        [cargo, "build", "--release", "--locked"],
        cwd=str(RUST_API),
        capture_output=True,
        text=True,
        env={**os.environ, "CARGO_NET_RETRY": "3"},
    )
    if build.returncode != 0 or not EDGE_BINARY.exists():
        log(f"native edge build failed: {build.stderr[-800:]}")
        return None
    return EDGE_BINARY


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


def edge_environment(env: dict, binary: Path | None) -> dict:
    return {
        **env,
        "RUST_API_LISTEN": f"0.0.0.0:{API_PORT}",
        "RUST_API_UPSTREAM": f"http://127.0.0.1:{BACKEND_PORT}",
        "RUST_API_NATIVE_IDENTITY": "true" if binary else "false",
        "RUST_API_SECURE_COOKIES": "false",
        "RUST_LOG": os.getenv("RUST_LOG", "info"),
        "DJANGO_SECRET_KEY": env["SECRET_KEY"],
    }


def main() -> None:
    stop_previous()
    env = runtime_env()
    binary = ensure_edge_binary()
    backend = spawn(
        [
            sys.executable,
            "-m",
            "daphne",
            "-b",
            "0.0.0.0",
            "-p",
            str(BACKEND_PORT),
            "platform_api.asgi:application",
        ],
        cwd=PLATFORM_API,
        log_name="api.log",
        env=env,
    )
    edge = None
    if binary is not None:
        edge = spawn(
            [str(binary)],
            cwd=RUST_API,
            log_name="edge.log",
            env=edge_environment(env, binary),
        )
    else:
        # Without a Rust toolchain the deployment stays usable: Django answers
        # the forwarded port exactly as before the edge existed.
        log("native edge unavailable; serving Django directly on the public port")
        edge = spawn(
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
            log_name="api-public.log",
            env=env,
        )
    # Queued workflow runs need an executor. Without the worker process a run
    # stays "queued" forever and the console can only report a queue id, which
    # looks like a broken console rather than an idle cluster.
    worker_env = {
        **env,
        "PYTHONPATH": f"{PLATFORM_API}:{REPO_ROOT}:{REPO_ROOT / 'apps' / 'worker'}",
        "WORKER_POLL_SECONDS": "1",
    }
    worker = spawn(
        [sys.executable, "-m", "worker_app.main"],
        cwd=REPO_ROOT / "apps" / "worker",
        log_name="worker.log",
        env=worker_env,
    )
    # The local runner lets a conversation be targeted at this machine. Enroll
    # it idempotently, then start the agent that leases its probe commands.
    subprocess.run(
        [sys.executable, "manage.py", "sync_plugin_catalog"],
        cwd=PLATFORM_API,
        env=env,
        check=False,
    )
    subprocess.run(
        [sys.executable, "manage.py", "install_builtin_connectors"],
        cwd=PLATFORM_API,
        env=env,
        check=False,
    )
    runner_output = subprocess.run(
        [
            sys.executable,
            "manage.py",
            "enroll_local_runner",
            "--name",
            "Local computer",
            "--token-file",
            str(DATA_DIR / "local-runner.token"),
        ],
        cwd=PLATFORM_API,
        env=env,
        capture_output=True,
        text=True,
    ).stdout.strip().splitlines()
    # The command prints the node id on success; diagnostics may precede it.
    runner_node = [
        line.strip() for line in runner_output if UUID_PATTERN.fullmatch(line.strip())
    ][-1:]
    local = None
    if runner_node:
        local = spawn(
            [
                sys.executable,
                str(REPO_ROOT / "apps" / "local-agent" / "local_agent.py"),
                "--base-url",
                f"http://127.0.0.1:{API_PORT}",
                "--node-id",
                runner_node[0],
                "--token-file",
                str(DATA_DIR / "local-runner.token"),
            ],
            cwd=REPO_ROOT / "apps" / "local-agent",
            log_name="local-agent.log",
            env=env,
        )
    # The launcher is one-shot, so a watchdog owns the API process after this
    # script exits. Without it a Daphne crash leaves the public tunnel serving
    # 502 forever even though the deployment reported success.
    watchdog = spawn(
        [
            sys.executable,
            str(Path(__file__).with_name("watch_api.py")),
            "--api-pid",
            str(backend),
            "--pid-file",
            str(PID_FILE),
            "--log",
            str(DATA_DIR / "api.log"),
            "--port",
            str(BACKEND_PORT),
        ],
        cwd=REPO_ROOT,
        log_name="watchdog.log",
        env=env,
    )
    edge_watchdog = None
    if binary is not None:
        edge_watchdog = spawn(
            [sys.executable, str(Path(__file__).with_name("watch_edge.py")),
             "--pid", str(edge),
             "--pid-file", str(PID_FILE),
             "--binary", str(binary),
             "--port", str(API_PORT),
             "--log", str(DATA_DIR / "edge.log")],
            cwd=RUST_API,
            log_name="edge-watchdog.log",
            env=edge_environment(env, binary),
        )
    # The edge serves the console bundle through Django, so the public port
    # stays single-origin while native routes run in Rust.
    pids = [backend, edge, worker] + ([local] if local else []) + [watchdog]
    if edge_watchdog is not None:
        pids.append(edge_watchdog)
    PID_FILE.write_text(json.dumps(pids))
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
