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
from .capi_renderer import render_bmh_manifests, render_capi_metal3_manifests
from .metal3_skill import (
    Metal3SkillError,
    apply_baremetalhosts,
    apply_capi_cluster_resources,
    observe_baremetalhosts,
    observe_capi_cluster,
    wait_for_baremetalhosts_ready,
    wait_for_capi_control_plane_ready,
)

LOG = logging.getLogger("eph-agent")
CAPABILITIES = [
    "eph.preflight",
    "eph.source.observe",
    "eph.metal3.bmh.apply",
    "eph.metal3.bmh.observe",
    "eph.metal3.bmh.wait_ready",
    "eph.capi.cluster.apply",
    "eph.capi.cluster.observe",
    "eph.capi.cluster.wait_ready",
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


def _run_kubectl(
    arguments: list[str],
    timeout: int = 60,
    max_output_bytes: int = 2 * 1024 * 1024,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["kubectl", *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={
                **os.environ,
                "KUBECONFIG": os.environ.get("CAPI_PIVOT_SOURCE_KUBECONFIG", ""),
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "error": type(exc).__name__}
    return {
        "available": True,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-max_output_bytes:],
        "stderr": completed.stderr[-4000:],
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
    if action == "eph.metal3.bmh.apply":
        checkpoint = command.get("deployment_checkpoint", {})
        if not isinstance(checkpoint, dict) or checkpoint.get("phase") != "eph_single_node_ready":
            return {
                "status": "blocked",
                "error_code": "METAL3_BMH_REGISTRATION_EPH_CHECKPOINT_REQUIRED",
                "output": {},
                "side_effects": False,
            }
        try:
            payload = command.get("payload", {})
            if not isinstance(payload, dict) or "hosts" not in payload:
                raise Metal3SkillError("METAL3_BMH_HOST_PLAN_REQUIRED")
            rendered = render_bmh_manifests(
                payload.get("hosts"),
                payload.get("namespace", "metal3"),
            )
            evidence = apply_baremetalhosts(
                {**payload, "manifests": rendered},
                _run_kubectl,
                lease_lost_event,
            )
            observed = observe_baremetalhosts(
                {"namespace": payload.get("namespace", "metal3")},
                _run_kubectl,
            )
            requested_names = {item["metadata"]["name"] for item in rendered}
            observed_names = {
                item["name"] for item in observed["hosts"]
                if item.get("name") in requested_names
            }
            if observed_names != requested_names:
                raise Metal3SkillError(
                    "METAL3_BMH_POST_APPLY_INVENTORY_MISMATCH",
                    side_effects=True,
                )
            evidence.update({
                "bmh_registration_applied": True,
                "bmh_secret_refs_verified": evidence["bmc_secret_references_verified"],
                "bmh_count": len(requested_names),
                "bmh_inventory_count": len(observed_names),
                "bmh_host_names": sorted(requested_names),
            })
        except Metal3SkillError as exc:
            return {
                "status": "failed",
                "error_code": exc.code,
                "output": {"resource_type": "METAL3_BMH"},
                "side_effects": exc.side_effects,
            }
        return {
            "status": "succeeded",
            "error_code": None,
            "output": evidence,
            "side_effects": True,
        }
    if action == "eph.metal3.bmh.observe":
        try:
            evidence = observe_baremetalhosts(command.get("payload", {}), _run_kubectl)
        except Metal3SkillError as exc:
            return {
                "status": "failed",
                "error_code": exc.code,
                "output": {},
                "side_effects": False,
            }
        return {
            "status": "succeeded",
            "error_code": None,
            "output": evidence,
            "side_effects": False,
        }
    if action == "eph.capi.cluster.apply":
        checkpoint = command.get("deployment_checkpoint", {})
        if not isinstance(checkpoint, dict) or checkpoint.get("phase") != "bmh_ready":
            return {
                "status": "blocked",
                "error_code": "CAPI_CLUSTER_BMH_READY_CHECKPOINT_REQUIRED",
                "output": {},
                "side_effects": False,
            }
        try:
            payload = command.get("payload", {})
            if not isinstance(payload, dict) or "cluster_spec" not in payload:
                raise Metal3SkillError("CAPI_CLUSTER_SPEC_REQUIRED")
            rendered = render_capi_metal3_manifests(payload.get("cluster_spec"))
            evidence = apply_capi_cluster_resources(
                {
                    "namespace": payload.get("cluster_spec", {}).get("namespace", "metal3"),
                    "manifests": rendered,
                },
                _run_kubectl,
                lease_lost_event,
            )
        except Metal3SkillError as exc:
            return {
                "status": "failed",
                "error_code": exc.code,
                "output": {"resource_type": "CAPI_CLUSTER"},
                "side_effects": exc.side_effects,
            }
        return {
            "status": "succeeded",
            "error_code": None,
            "output": evidence,
            "side_effects": True,
        }
    if action == "eph.capi.cluster.observe":
        try:
            evidence = observe_capi_cluster(command.get("payload", {}), _run_kubectl)
        except Metal3SkillError as exc:
            return {
                "status": "failed",
                "error_code": exc.code,
                "output": {},
                "side_effects": False,
            }
        ready = evidence["control_plane_ready"]
        return {
            "status": "succeeded" if ready else "blocked",
            "error_code": None if ready else "CAPI_CONTROL_PLANE_NOT_READY",
            "output": evidence,
            "side_effects": False,
        }
    if action == "eph.metal3.bmh.wait_ready":
        checkpoint = command.get("deployment_checkpoint", {})
        if not isinstance(checkpoint, dict) or checkpoint.get("phase") != "bmh_registered":
            return {
                "status": "blocked",
                "error_code": "METAL3_BMH_READY_REGISTRATION_CHECKPOINT_REQUIRED",
                "output": {},
                "side_effects": False,
            }
        try:
            evidence = wait_for_baremetalhosts_ready(
                command.get("payload", {}),
                _run_kubectl,
                lease_lost_event,
            )
        except Metal3SkillError as exc:
            return {
                "status": "failed",
                "error_code": exc.code,
                "output": {},
                "side_effects": False,
            }
        return {
            "status": "succeeded",
            "error_code": None,
            "output": evidence,
            "side_effects": False,
        }
    if action == "eph.capi.cluster.wait_ready":
        try:
            evidence = wait_for_capi_control_plane_ready(
                command.get("payload", {}),
                _run_kubectl,
                lease_lost_event,
            )
        except Metal3SkillError as exc:
            return {
                "status": "failed",
                "error_code": exc.code,
                "output": {},
                "side_effects": False,
            }
        return {
            "status": "succeeded",
            "error_code": None,
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
                        checkpoint_phase = {
                            "eph.preflight": "eph_single_node_ready",
                            "eph.metal3.bmh.apply": "bmh_registered",
                            "eph.metal3.bmh.wait_ready": "bmh_ready",
                        }.get(command.get("action"))
                        if (
                            checkpoint_phase is not None
                            and outcome["status"] == "succeeded"
                            and not guard.lost.is_set()
                        ):
                            try:
                                checkpoint = client.persist_checkpoint(
                                    command,
                                    checkpoint_phase,
                                    outcome["output"],
                                )
                                outcome["output"]["checkpoint"] = checkpoint
                                outcome["side_effects"] = True
                            except AgentTransportError:
                                outcome = {
                                    "status": "failed",
                                    "error_code": "EPH_CHECKPOINT_PERSIST_FAILED",
                                    "output": {"phase": checkpoint_phase},
                                    "side_effects": outcome.get("side_effects", False),
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
