"""Prepare a Codespace for the native Rust console.

Installs the Python tooling used by the development agents, builds the Vue
console, and compiles the Rust API. Every step is idempotent.
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
RUST_API = REPO_ROOT / "apps" / "rust-api"
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


def run(command: list[str], *, cwd: Path, env: dict | None = None) -> None:
    log("+ " + " ".join(command))
    subprocess.run(command, cwd=str(cwd), env={**os.environ, **(env or {})}, check=True)


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


def build_api() -> None:
    cargo = shutil.which("cargo")
    if cargo is None:
        raise RuntimeError("cargo is missing; install the Rust toolchain")
    run([cargo, "build", "--release", "--locked"], cwd=RUST_API)


def main() -> None:
    install_python()
    install_frontend()
    build_console()
    build_api()
    log("codespace prepared: run `python3 .devcontainer/start.py` to serve the console")


if __name__ == "__main__":
    main()
