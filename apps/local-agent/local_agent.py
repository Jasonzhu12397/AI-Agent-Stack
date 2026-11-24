"""Local runner: executes read-only probes on the machine it is started on.

The console can target a conversation at *the local computer*. That target is
served by this process: it enrolls as an ``AgentNode`` of type ``local_runner``,
heartbeats, leases the commands the platform queues for it and answers with real
host facts. Nothing here is a stub — the numbers come from the machine that
runs it, and the platform records the command, the lease and the result with
the same code path it uses for EPH and Target agents.

Usage (the token is printed once by ``manage.py enroll_local_runner``):

    python apps/local-agent/local_agent.py --base-url http://127.0.0.1:8000 \
        --node-id <uuid> --token-file .devdata/local-runner.token
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request

HEARTBEAT_SECONDS = 15
POLL_SECONDS = 2
HTTP_TIMEOUT = 10


def _uptime_seconds() -> float | None:
    try:
        with open("/proc/uptime", encoding="utf-8") as handle:
            return float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _load_average() -> list[float] | None:
    try:
        return [round(value, 2) for value in os.getloadavg()]
    except OSError:
        return None


def execute(command_type: str, payload: dict) -> dict:
    """Run one command and return its JSON result."""

    if command_type == "local.system_info":
        return {
            "hostname": socket.gethostname(),
            "os": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "cpu_count": os.cpu_count(),
            "python": platform.python_version(),
            "load_average": _load_average(),
            "uptime_seconds": _uptime_seconds(),
        }
    if command_type == "local.disk_usage":
        path = payload.get("path") or "/"
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("disk_usage path must be absolute")
        usage = shutil.disk_usage(path)
        return {
            "path": path,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        }
    raise ValueError(f"unsupported local command: {command_type}")


class Runner:
    def __init__(self, base_url: str, node_id: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.node_id = node_id
        self.token = token
        self.last_heartbeat = 0.0

    def post(self, path: str, body: dict, lease: str | None = None) -> dict:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
                **({"X-Agent-Lease": lease} if lease else {}),
            },
        )
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            payload = response.read()
        return json.loads(payload) if payload else {}

    def heartbeat(self, capabilities: list[str]) -> None:
        self.post(
            "/api/v1/agent-runtime/heartbeat/",
            {
                "node_id": self.node_id,
                "capabilities": capabilities,
                "agent_version": "local-agent-1.0",
            },
        )
        self.last_heartbeat = time.monotonic()

    def claim(self, capabilities: list[str]) -> dict | None:
        payload = self.post(
            "/api/v1/agent-runtime/commands/poll/",
            {"node_id": self.node_id, "capabilities": capabilities},
        )
        return payload.get("command")

    def report(self, command: dict, result: dict | None, error: dict | None) -> None:
        body = {
            "node_id": self.node_id,
            "outcome": "failed" if error else "succeeded",
        }
        if error:
            body["error"] = error
        else:
            body["result"] = result or {}
        self.post(
            f"/api/v1/agent-runtime/commands/{command['id']}/result/",
            body,
            lease=command.get("lease_token"),
        )

    def run_forever(self) -> None:
        capabilities = ["local.system_info", "local.disk_usage"]
        while True:
            if time.monotonic() - self.last_heartbeat > HEARTBEAT_SECONDS:
                try:
                    self.heartbeat(capabilities)
                except (urllib.error.URLError, OSError) as exc:
                    print(f"heartbeat failed: {exc}", file=sys.stderr, flush=True)
            try:
                command = self.claim(capabilities)
            except (urllib.error.URLError, OSError) as exc:
                print(f"poll failed: {exc}", file=sys.stderr, flush=True)
                time.sleep(POLL_SECONDS)
                continue
            if not command:
                time.sleep(POLL_SECONDS)
                continue
            print(f"running {command['command_type']}", flush=True)
            try:
                result = execute(command["command_type"], command.get("payload") or {})
                result_payload, error = result, None
            except Exception as exc:  # noqa: BLE001 - the runner reports any failure
                result_payload, error = None, {"code": "LOCAL_COMMAND_FAILED", "message": str(exc)}
            try:
                self.report(command, result_payload, error)
            except (urllib.error.URLError, OSError) as exc:
                print(f"result report failed: {exc}", file=sys.stderr, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Local runner for the console")
    parser.add_argument("--base-url", default=os.environ.get("CONSOLE_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--node-id", default=os.environ.get("LOCAL_RUNNER_NODE_ID", ""))
    parser.add_argument("--token-file", default=os.environ.get("LOCAL_RUNNER_TOKEN_FILE", ""))
    options = parser.parse_args()
    token = os.environ.get("LOCAL_RUNNER_TOKEN", "")
    if options.token_file:
        with open(options.token_file, encoding="utf-8") as handle:
            token = handle.read().strip()
    if not options.node_id or not token:
        parser.error("--node-id and a token (--token-file or LOCAL_RUNNER_TOKEN) are required")
    print(f"local runner {options.node_id} -> {options.base_url}", flush=True)
    Runner(options.base_url, options.node_id, token).run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
