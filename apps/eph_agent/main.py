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

from packages.agent_contracts.host_network_observe import HostNetworkObserveError
from packages.agent_contracts.orchestration import (
    EPH_ACTIONS,
    EPH_CHECKPOINT_PHASES,
)
from packages.agent_contracts.outcomes import is_retryable_outcome
from packages.agent_transport import (
    AgentControlClient,
    AgentSettings,
    AgentTransportError,
    CommandLeaseGuard,
    ConfigurationError,
)

from .bootstrap_renderer import (
    render_agent_deployment,
    render_cloud_init_seed,
    render_kubeadm_init,
)
from .capi_renderer import render_bmh_manifests, render_capi_metal3_manifests
from .control_stack import plan_control_stack
from .control_stack_adapter import (
    ControlStackAdapterError,
    apply_control_stack,
    apply_workload_resource,
    wait_for_control_stack_ready,
)
from .control_stack_manifests import load_component_manifests
from .fleet_planning import PlanningError, plan_fleet_request
from .ironic_cr import (
    IRONIC_CRD_NAME,
    IronicRenderError,
    contains_ironic_cr,
    render_ironic_cr,
)
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
CAPABILITIES = sorted(EPH_ACTIONS)


def _run_read_only(command: list[str], timeout: int = 20) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "KUBECONFIG": os.environ.get("EPH_KUBECONFIG", "")},
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
                "KUBECONFIG": os.environ.get("EPH_KUBECONFIG", ""),
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


def _artifact_digest(document: Any) -> str:
    """Stable digest so a re-render of the same spec is byte-for-byte identical."""

    encoded = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def execute(
    command: dict[str, Any],
    lease_lost_event: Any = None,
) -> dict[str, Any]:
    action = command.get("action")
    if action == "host.network.observe":
        from packages.agent_contracts.host_network_probe import collect_local_host_networks

        try:
            observation = collect_local_host_networks(
                lambda argv: _run_read_only(argv, timeout=20)
            )
        except HostNetworkObserveError as exc:
            return {
                "status": "failed",
                "error_code": exc.code,
                "output": {},
                "side_effects": False,
            }
        return {
            "status": "succeeded",
            "error_code": None,
            "output": observation,
            "side_effects": False,
        }
    if action == "eph.preflight":
        source_kubeconfig = Path(os.environ.get("EPH_KUBECONFIG", ""))
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
        payload = command.get("payload", {})
        fleet_evidence: dict[str, Any] = {}
        if isinstance(payload, dict) and "fleet" in payload:
            try:
                fleet_evidence = plan_fleet_request(payload)
            except PlanningError as exc:
                return {
                    "status": "blocked",
                    "error_code": "EPH_FLEET_PLAN_INVALID",
                    "output": {
                        "role": "eph",
                        "fleet_plan_failed": True,
                        "reason": str(exc),
                    },
                    "side_effects": False,
                }
        evidence = {
            **fleet_evidence,
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
    if action == "eph.bootstrap.render":
        payload = command.get("payload", {})
        try:
            if not isinstance(payload, dict):
                raise Metal3SkillError("COMMAND_PAYLOAD_INVALID")
            spec = payload.get("spec", payload)
            artifacts = {
                "kubeadm_init": render_kubeadm_init(spec),
                "cloud_init_seed": render_cloud_init_seed(spec),
                "agent_deployment": render_agent_deployment(spec),
            }
        except Metal3SkillError as exc:
            return {
                "status": "failed",
                "error_code": exc.code,
                "output": {"resource_type": "EPH_BOOTSTRAP"},
                "side_effects": False,
            }
        return {
            "status": "succeeded",
            "error_code": None,
            "output": {
                "resource_type": "EPH_BOOTSTRAP",
                "artifact_count": len(artifacts),
                "artifact_digests": {
                    name: _artifact_digest(document)
                    for name, document in artifacts.items()
                },
            },
            "side_effects": False,
        }
    if action == "eph.controlstack.apply":
        checkpoint = command.get("deployment_checkpoint", {})
        if not isinstance(checkpoint, dict) or checkpoint.get("phase") != "eph_single_node_ready":
            return {
                "status": "blocked",
                "error_code": "CONTROL_STACK_CHECKPOINT_REQUIRED",
                "output": {},
                "side_effects": False,
            }
        try:
            payload = command.get("payload", {})
            if not isinstance(payload, dict):
                raise ControlStackAdapterError("COMMAND_PAYLOAD_INVALID")
            spec = payload.get("spec", payload)
            manifests = payload.get("manifests")
            plan = plan_control_stack(spec)
            offline = load_component_manifests([item.name for item in plan])
            if isinstance(manifests, dict):
                merged = {**offline, **manifests}
            else:
                merged = offline
            manifests = merged
            ironic_documents = manifests.get("ironic")
            manifest_has_ironic_cr = contains_ironic_cr(ironic_documents)
            rendered_ironic = render_ironic_cr(spec)
            if manifest_has_ironic_cr and rendered_ironic is not None:
                raise IronicRenderError("IRONIC_CR_MULTIPLE_SOURCES")
            if not manifest_has_ironic_cr and rendered_ironic is None:
                raise IronicRenderError("IRONIC_WORKLOAD_CR_REQUIRED")
            applied = apply_control_stack(
                plan,
                _run_kubectl,
                lease_lost_event,
                manifests,
            )
            if rendered_ironic is not None:
                apply_workload_resource(
                    rendered_ironic,
                    _run_kubectl,
                    lease_lost_event,
                    crd_name=IRONIC_CRD_NAME,
                )
            readiness = wait_for_control_stack_ready(plan, _run_kubectl, lease_lost_event)
            evidence = {**applied, "readiness": readiness}
        except IronicRenderError as exc:
            return {
                "status": "failed",
                "error_code": exc.code,
                "output": {"resource_type": "CONTROL_STACK"},
                "side_effects": False,
            }
        except Metal3SkillError as exc:
            return {
                "status": "failed",
                "error_code": exc.code,
                "output": {"resource_type": "CONTROL_STACK"},
                "side_effects": getattr(exc, "side_effects", False),
            }
        return {
            "status": "succeeded",
            "error_code": None,
            "output": evidence,
            "side_effects": applied.get("side_effects", False),
        }
    if action == "eph.metal3.bmh.apply":
        checkpoint = command.get("deployment_checkpoint", {})
        if not isinstance(checkpoint, dict) or checkpoint.get("phase") != "control_stack_ready":
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
            try:
                observed = observe_baremetalhosts(
                    {"namespace": payload.get("namespace", "metal3")},
                    _run_kubectl,
                )
            except Metal3SkillError as exc:
                exc.side_effects = True
                raise
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
        checkpoint = command.get("deployment_checkpoint", {})
        if (
            not isinstance(checkpoint, dict)
            or checkpoint.get("phase") != "target_control_plane_applied"
        ):
            return {
                "status": "blocked",
                "error_code": "CAPI_CONTROL_PLANE_APPLIED_CHECKPOINT_REQUIRED",
                "output": {},
                "side_effects": False,
            }
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
        "retryable": is_retryable_outcome(outcome),
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
                        checkpoint_phase = EPH_CHECKPOINT_PHASES.get(
                            command.get("action")
                        )
                        checkpoint_phase = (
                            checkpoint_phase.value
                            if checkpoint_phase is not None
                            else None
                        )
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
