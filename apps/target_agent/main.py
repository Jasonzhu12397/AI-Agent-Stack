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
from itertools import count
from typing import Any
from uuid import UUID

from packages.agent_transport import AgentControlClient, AgentSettings, AgentTransportError, ConfigurationError
from .leader_election import KubernetesLeaseElector, LeaseElectionError

LOG = logging.getLogger("target-agent")
CAPABILITIES = ["target.cluster.observe", "target.capi.observe", "target.metal3.observe"]


def _kubectl(arguments: list[str], timeout: int = 30) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["kubectl", *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "KUBECONFIG": os.environ.get("KUBECONFIG", "")},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "error": type(exc).__name__}
    return {
        "available": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-8000:],
        "stderr": completed.stderr[-2000:],
    }


def execute(command: dict[str, Any]) -> dict[str, Any]:
    action = command.get("action")
    if action == "target.cluster.observe":
        result = _kubectl(["get", "nodes", "-o", "json"])
        return {"status": "succeeded" if result["available"] else "failed", "error_code": None if result["available"] else "TARGET_CLUSTER_OBSERVE_FAILED", "output": result, "side_effects": False}
    if action == "target.capi.observe":
        result = _kubectl(["get", "clusters,machines,machinedeployments,kubeadmcontrolplanes", "-A", "-o", "json"])
        return {"status": "succeeded" if result["available"] else "failed", "error_code": None if result["available"] else "TARGET_CAPI_OBSERVE_FAILED", "output": result, "side_effects": False}
    if action == "target.metal3.observe":
        result = _kubectl(["get", "baremetalhosts,metal3machines,metal3clusters", "-A", "-o", "json"])
        return {"status": "succeeded" if result["available"] else "failed", "error_code": None if result["available"] else "TARGET_METAL3_OBSERVE_FAILED", "output": result, "side_effects": False}
    return {"status": "blocked", "error_code": "ACTION_NOT_IMPLEMENTED", "output": {"action": action, "reason": "No approved mutation adapter is registered"}, "side_effects": False}


def _result(command: dict[str, Any], identity: UUID, outcome: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    payload = json.dumps(outcome["output"], sort_keys=True, separators=(",", ":")).encode()
    return {
        "schema_version": "v1",
        "command_id": command["command_id"],
        "agent_id": str(identity),
        "lease_id": command["lease"]["lease_id"],
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
    parser = argparse.ArgumentParser(prog="target-agent")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--readiness-check", action="store_true")
    args = parser.parse_args()
    if args.healthcheck:
        print(json.dumps({"service": "target-agent", "status": "healthy"}))
        return 0
    try:
        settings = AgentSettings.from_environment("target")
        if args.readiness_check:
            check = _kubectl(["get", "nodes", "-o", "json"])
            if not check["available"]:
                return 1
            print(json.dumps({"service": "target-agent", "status": "ready"}))
            return 0
        client = AgentControlClient(settings)
        elector = KubernetesLeaseElector(
            identity=settings.instance_id,
            namespace=os.environ.get("AGENT_LEASE_NAMESPACE", "ai-agent-system"),
            lease_name=os.environ.get("AGENT_LEASE_NAME", "target-agent-leader"),
            duration_seconds=int(os.environ.get("AGENT_LEASE_DURATION_SECONDS", "30")),
        )
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
        for sequence in count(1):
            if stopped:
                break
            try:
                if elector.try_acquire_or_renew():
                    client.heartbeat(sequence, "ready", CAPABILITIES)
                    command = client.claim()
                    if command is not None:
                        outcome = execute(command)
                        client.report_result(_result(command, settings.agent_id, outcome))
                else:
                    LOG.debug("This Target Agent replica is not the elected leader")
            except (AgentTransportError, LeaseElectionError, KeyError, TypeError, ValueError) as exc:
                LOG.error("Target control cycle failed: %s", type(exc).__name__)
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
