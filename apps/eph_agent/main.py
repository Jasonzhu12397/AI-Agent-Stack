from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from packages.agent_transport import AgentControlClient, AgentSettings, AgentTransportError, ConfigurationError

LOG = logging.getLogger("eph-agent")
CAPABILITIES = ["eph.preflight", "eph.source.observe"]


def _run_read_only(command: list[str], timeout: int = 20) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "KUBECONFIG": os.environ.get("CAPI_PIVOT_SOURCE_KUBECONFIG", "")},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "error": type(exc).__name__}
    return {
        "available": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-2000:],
    }


def execute(command: dict[str, Any]) -> dict[str, Any]:
    action = command.get("action")
    if action == "eph.preflight":
        source_kubeconfig = Path(os.environ.get("CAPI_PIVOT_SOURCE_KUBECONFIG", ""))
        target_kubeconfig = Path(os.environ.get("CAPI_PIVOT_TARGET_KUBECONFIG", ""))
        evidence = {
            "role": "eph",
            "single_node_cluster_expected": True,
            "source_kubeconfig_present": source_kubeconfig.is_file(),
            "target_kubeconfig_present": target_kubeconfig.is_file(),
            "clusterctl": _run_read_only(["clusterctl", "version"]),
            "kubectl": _run_read_only(["kubectl", "version", "--client"]),
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        ready = (
            evidence["source_kubeconfig_present"]
            and evidence["target_kubeconfig_present"]
            and evidence["clusterctl"]["available"]
            and evidence["kubectl"]["available"]
        )
        return {"status": "succeeded" if ready else "blocked", "error_code": None if ready else "EPH_PREFLIGHT_INCOMPLETE", "output": evidence, "side_effects": False}
    if action == "eph.source.observe":
        result = _run_read_only(["kubectl", "get", "nodes", "-o", "json"])
        return {"status": "succeeded" if result["available"] else "failed", "error_code": None if result["available"] else "EPH_SOURCE_OBSERVE_FAILED", "output": result, "side_effects": False}
    return {"status": "blocked", "error_code": "ACTION_NOT_IMPLEMENTED", "output": {"action": action}, "side_effects": False}


def _result(command: dict[str, Any], identity: UUID, outcome: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    payload = json.dumps(outcome["output"], sort_keys=True, separators=(",", ":")).encode()
    return {
        "schema_version": "v1",
        "command_id": command["command_id"],
        "agent_id": str(identity),
        "lease_id": command["lease"]["lease_id"],
        "lease_token": command["lease"]["token"],
        "status": outcome["status"],
        "started_at": now.isoformat(),
        "completed_at": now.isoformat(),
        "output": outcome["output"],
        "error_code": outcome["error_code"],
        "retryable": False,
        "side_effects": outcome["side_effects"],
        "evidence_digest": hashlib.sha256(payload).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="eph-agent")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--readiness-check", action="store_true")
    args = parser.parse_args()
    if args.healthcheck:
        print(json.dumps({"service": "eph-agent", "status": "healthy"}))
        return 0
    try:
        settings = AgentSettings.from_environment("eph")
        if args.readiness_check:
            print(json.dumps({"service": "eph-agent", "status": "ready"}))
            return 0
        client = AgentControlClient(settings)
    except (ConfigurationError, AgentTransportError) as exc:
        LOG.error("%s", exc)
        return 1
    stopped = False

    def stop_handler(_signum: int, _frame: Any) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    try:
        for sequence in iter(time.time_ns, -1):
            if stopped:
                break
            try:
                client.heartbeat(sequence, "ready", CAPABILITIES)
                command = client.claim()
                if command is not None:
                    outcome = execute(command)
                    client.report_result(_result(command, settings.agent_id, outcome))
            except (AgentTransportError, KeyError, TypeError, ValueError) as exc:
                LOG.error("EPH control cycle failed: %s", type(exc).__name__)
            for _ in range(max(1, int(settings.poll_seconds * 10))):
                if stopped:
                    break
                time.sleep(0.1)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    raise SystemExit(main())
