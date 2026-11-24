"""Expose the running console through a quick tunnel.

The GitHub Codespaces port proxy asks every browser for a GitHub session, which
makes a shared link unusable. A Cloudflare quick tunnel gives a plain public
HTTPS address instead. The binary is fetched once into /tmp and reused, and the
tunnel is detached so it outlives the shell that started it.
"""

from __future__ import annotations

import os
import platform
import re
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

BINARY = Path("/tmp/cloudflared")
LOG = Path("/tmp/cloudflared.log")
TARGET = os.getenv("TUNNEL_TARGET", "http://127.0.0.1:8000")
URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
DOWNLOAD = (
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
)


def architecture() -> str:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "amd64"
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    raise RuntimeError(f"unsupported architecture: {machine}")


def ensure_binary() -> Path:
    if BINARY.exists() and os.access(BINARY, os.X_OK):
        return BINARY
    url = DOWNLOAD.replace("amd64", architecture())
    print(f"downloading cloudflared from {url}", flush=True)
    urllib.request.urlretrieve(url, BINARY)
    BINARY.chmod(0o755)
    return BINARY


def stop_stale() -> None:
    """A previous tunnel would keep the log's old URL, so start clean."""

    try:
        pid = int((Path("/tmp/cloudflared.pid")).read_text().strip())
        os.kill(pid, signal.SIGTERM)
        time.sleep(1.0)
    except (FileNotFoundError, ProcessLookupError, ValueError):
        pass


def start_tunnel(binary: Path) -> int:
    handle = LOG.open("w")
    process = subprocess.Popen(
        [str(binary), "tunnel", "--url", TARGET, "--no-autoupdate"],
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    Path("/tmp/cloudflared.pid").write_text(str(process.pid))
    return process.pid


def discover_url(attempts: int = 40, delay: float = 2.0) -> str:
    for _ in range(attempts):
        if LOG.exists():
            found = URL_PATTERN.findall(LOG.read_text(errors="replace"))
            if found:
                return found[0]
        time.sleep(delay)
    raise RuntimeError("cloudflared did not report a URL; see /tmp/cloudflared.log")


def probe(url: str, attempts: int = 30, delay: float = 2.0) -> str:
    """Verify the tunnel from inside the codespace network."""

    last = "no response"
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(f"{url}/health/ready", timeout=10) as response:
                return f"health={response.status}"
        except Exception as exc:  # noqa: BLE001 - report, never crash
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(delay)
    return f"health=failed ({last})"


def main() -> None:
    stop_stale()
    urllib.request.urlopen(f"{TARGET}/health/ready", timeout=15).read()
    print(f"origin reachable at {TARGET}", flush=True)
    binary = ensure_binary()
    pid = start_tunnel(binary)
    url = discover_url()
    print(url, flush=True)
    print(f"tunnel pid {pid} serving {TARGET}", flush=True)
    print(probe(url), flush=True)
    print((LOG.read_text(errors="replace").splitlines() or ["<no log>"])[-6:], flush=True)


if __name__ == "__main__":
    main()
