from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from packages.agent_transport import (
    AgentControlClient,
    AgentSettings,
    AgentTransportError,
    CommandLeaseGuard,
    ConfigurationError,
)

from .capi_pivot import execute_capi_pivot

LOG = logging.getLogger("eph-agent")
CAPABILITIES = [
    "eph.preflight",
    "eph.source.observe",
    "eph.capi.pivot.dry_run",
    "eph.capi.pivot",
]


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


def execute(
    command: dict[str, Any],
    lease_lost_event: Any = None,
) -> dict[str, Any]:
    action = command.get("action")
    if action == "eph.preflight":
        source_kubeconfig = Path(os.environ.get("CAPI_PIVOT_SOURCE_KUBECONFIG", ""))
        nodes = _run_read_only(["kubectl", "get", "nodes", "-o", "json"])
        node_count = None
        ready_nodes = []
        if nodes["available"]:
            try:
                node_document = json.loads(nodes["stdout"])
                node_items = node_document.get("items", [])
                node_count = len(node_items)
                ready_nodes = [
                    item.get("metadata", {}).get("name")
                    for item in node_items
                    if any(
                        condition.get("type") == "Ready" and condition.get("status") == "True"
                        for condition in item.get("status", {}).get("conditions", [])
                    )
                ]
            except (ValueError, TypeError, AttributeError):
                node_count = None
        evidence = {
            "role": "eph",
            "eph_ready": False,
            "eph_node_count": node_count,
            "eph_source_preflight_passed": False,
            "single_node_cluster_expected": True,
            "source_kubeconfig_present": source_kubeconfig.is_file(),
            "node_count": node_count,
            "ready_nodes": ready_nodes,
            "node_observation_available": nodes["available"],
            "clusterctl": _run_read_only(["clusterctl", "version"]),
            "kubectl": _run_read_only(["kubectl", "version", "--client"]),
            "observed_at": datetime.now(UTC).isoformat(),
        }
        ready = (
            evidence["source_kubeconfig_present"]
            and evidence["node_observation_available"]
            and evidence["node_count"] == 1
            and len(evidence["ready_nodes"]) == 1
            and evidence["clusterctl"]["available"]
            and evidence["kubectl"]["available"]
        )
        evidence["eph_ready"] = ready
        evidence["eph_source_preflight_passed"] = ready
        return {
            "status": "succeeded" if ready else "blocked",
            "error_code": None if ready else "EPH_PREFLIGHT_INCOMPLETE",
            "output": evidence,
            "side_effects": False,
        }
    if action in ("eph.capi.pivot.dry_run", "eph.capi.pivot"):
        return execute_capi_pivot(command, lease_lost_event)
    if action == "eph.source.observe":
        result = _run_read_only(["kubectl", "get", "nodes", "-o", "json"])
        return {
            "status": "succeeded" if result["available"] else "failed",
            "error_code": None if result["available"] else "EPH_SOURCE_OBSERVE_FAILED",
            "output": result,
            "side_effects": False,
        }
    return {
        "status": "blocked",
        "error_code": "ACTION_NOT_IMPLEMENTED",
        "output": {"action": action},
        "side_effects": False,
    }


def _result(
    command: dict[str, Any],
    identity: UUID,
    outcome: dict[str, Any],
    started_at: datetime,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    payload = json.dumps(outcome["output"], sort_keys=True, separators=(",", ":")).encode()
    return {
        "schema_version": "v1",
        "command_id": command["command_id"],
        "agent_id": str(identity),
        "lease_id": command["lease"]["lease_id"],
        "lease_token": command["lease"]["token"],
        "status": outcome["status"],
        "started_at": started_at.isoformat(),
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
        client = AgentControlClient(settings)
        if args.readiness_check:
            preflight = execute({"action": "eph.preflight"})
            ready = preflight["status"] == "succeeded" and client.check_ready()
            client.close()
            print(json.dumps({"service": "eph-agent", "status": "ready" if ready else "not_ready"}))
            return 0 if ready else 1
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
                    started_at = datetime.now(UTC)
                    guard = CommandLeaseGuard(client, command)
                    guard.start()
                    try:
                        outcome = execute(command, guard.lost)
                        if guard.lost.is_set():
                            outcome = {
                                "status": "failed",
                                "error_code": "COMMAND_LEASE_LOST_DURING_EXECUTION",
                                "output": outcome.get("output", {}),
                                "side_effects": outcome.get("side_effects", False),
                            }
                        if (
                            command.get("action") == "eph.preflight"
                            and outcome["status"] == "succeeded"
                            and not guard.lost.is_set()
                        ):
                            try:
                                checkpoint = client.persist_checkpoint(
                                    command,
                                    "eph_single_node_ready",
                                    outcome["output"],
                                )
                                outcome["output"]["checkpoint"] = checkpoint
                                outcome["side_effects"] = True
                            except AgentTransportError:
                                outcome = {
                                    "status": "failed",
                                    "error_code": "EPH_CHECKPOINT_PERSIST_FAILED",
                                    "output": {"phase": "eph_single_node_ready"},
                                    "side_effects": False,
                                }
                        client.report_result(
                            _result(command, settings.agent_id, outcome, started_at)
                        )
                    finally:
                        guard.close()
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
