from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from packages.agent_transport import (
    AgentControlClient,
    AgentSettings,
    AgentTransportError,
    CommandLeaseGuard,
    ConfigurationError,
)

from .handoff import (
    HandoffError,
    HandoffPhase,
    evaluate_transition,
    validate_phase_evidence,
)
from .ironic_client import (
    IronicObservationError,
    get_ironic_node,
    ironic_api_configured,
)
from .leader_election import (
    KubernetesLeaseElector,
    LeaderLeaseGuard,
    LeaseElectionError,
)
from .metal3_ownership import Metal3OwnershipError, verify_metal3_ownership
from .ntp_attestation import NtpAttestationError, verify_control_plane_ntp_bootstrap
from .ntp_policy import (
    NtpPolicyError,
    probe_ironic_internal_ntp_endpoint,
    render_control_plane_chrony_config,
    render_control_plane_chrony_pre_kubeadm_command,
    render_worker_chrony_config,
    render_worker_chrony_pre_kubeadm_command,
    validate_control_plane_ntp_profile,
    validate_ironic_internal_ntp_ip,
    validate_worker_ntp_sources,
)
from .worker_attestation import WorkerAttestationError, verify_worker_readiness

LOG = logging.getLogger("target-agent")
CAPABILITIES = [
    "target.cluster.observe",
    "target.capi.observe",
    "target.metal3.observe",
    "target.metal3.verify_ownership",
    "target.worker.ntp.validate",
    "target.ntp.control_plane.render",
    "target.ntp.control_plane.verify_bootstrap",
    "target.ntp.internal.probe",
    "target.handoff.evaluate",
]


def _configure_in_cluster_kubeconfig() -> None:
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "")
    port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    token_file = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    ca_file = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    if not host or not os.path.isfile(token_file) or not os.path.isfile(ca_file):
        raise RuntimeError("In-cluster Kubernetes credentials are unavailable")
    config = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{
            "name": "target",
            "cluster": {
                "server": f"https://{host}:{port}",
                "certificate-authority": ca_file,
            },
        }],
        "users": [{"name": "target-agent", "user": {"tokenFile": token_file}}],
        "contexts": [{
            "name": "target",
            "context": {"cluster": "target", "user": "target-agent"},
        }],
        "current-context": "target",
    }
    descriptor, path = tempfile.mkstemp(prefix="target-agent-", suffix=".kubeconfig", dir="/tmp")
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(config, stream)
    os.environ["KUBECONFIG"] = path


def _kubectl(
    arguments: list[str],
    timeout: int = 30,
    max_output_bytes: int = 512 * 1024,
) -> dict[str, Any]:
    allowed_environment = (
        "PATH",
        "HOME",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "XDG_CACHE_HOME",
    )
    environment = {
        key: os.environ[key]
        for key in allowed_environment
        if key in os.environ
    }
    environment["KUBECONFIG"] = os.environ.get("KUBECONFIG", "")
    try:
        completed = subprocess.run(
            ["kubectl", *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "error": type(exc).__name__}
    output_size = len(completed.stdout.encode("utf-8"))
    if output_size > max_output_bytes:
        return {
            "available": False,
            "error": "KUBECTL_OUTPUT_LIMIT_EXCEEDED",
            "output_bytes": output_size,
        }
    return {
        "available": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr[-2000:],
    }


def _require_configured_ironic_internal_ntp_ip(requested: Any) -> str:
    configured = os.environ.get("IRONIC_INTERNAL_NTP_IP", "")
    if not configured:
        raise NtpPolicyError("IRONIC_INTERNAL_NTP_IP_CONFIGURATION_REQUIRED")
    expected = validate_ironic_internal_ntp_ip(configured)
    supplied = validate_ironic_internal_ntp_ip(str(requested or ""))
    if supplied != expected:
        raise NtpPolicyError("IRONIC_INTERNAL_NTP_IP_CONFIGURATION_MISMATCH")
    return expected


def _observe_target_metal3_ownership(payload: dict[str, Any]) -> dict[str, Any]:
    namespace = payload.get("namespace")
    cluster_name = payload.get("cluster_name")
    if (
        not isinstance(namespace, str)
        or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", namespace)
        or not isinstance(cluster_name, str)
        or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", cluster_name)
    ):
        raise Metal3OwnershipError("TARGET_METAL3_NAMESPACE_OR_CLUSTER_INVALID")
    resources = (
        (
            "cluster",
            [
                "get",
                "cluster.cluster.x-k8s.io",
                cluster_name,
                "--namespace",
                namespace,
                "-o",
                "json",
            ],
        ),
        (
            "machines",
            ["get", "machines.cluster.x-k8s.io", "--namespace", namespace, "-o", "json"],
        ),
        (
            "metal3machines",
            [
                "get",
                "metal3machines.infrastructure.cluster.x-k8s.io",
                "--namespace",
                namespace,
                "-o",
                "json",
            ],
        ),
        (
            "bmhs",
            ["get", "baremetalhosts.metal3.io", "--namespace", namespace, "-o", "json"],
        ),
    )
    documents = {}
    for name, arguments in resources:
        result = _kubectl(arguments, timeout=60, max_output_bytes=32 * 1024 * 1024)
        if not result["available"]:
            raise Metal3OwnershipError(f"TARGET_METAL3_OBSERVATION_FAILED:{name}")
        try:
            documents[name] = json.loads(result["stdout"])
        except ValueError as exc:
            raise Metal3OwnershipError(f"TARGET_METAL3_JSON_INVALID:{name}") from exc
    ownership = verify_metal3_ownership(
        documents["cluster"],
        documents["machines"],
        documents["metal3machines"],
        documents["bmhs"],
        payload.get("source_bmh_identities")
        or payload.get("evidence", {}).get("source_bmh_identities"),
    )
    if not ironic_api_configured():
        raise Metal3OwnershipError("TARGET_IRONIC_API_CONFIGURATION_REQUIRED")
    ironic_nodes = []
    for host in documents["bmhs"].get("items", []):
        host_name = host.get("metadata", {}).get("name", "unknown")
        ironic_uuid = host.get("status", {}).get("provisioning", {}).get("ID")
        if not isinstance(ironic_uuid, str) or not ironic_uuid:
            raise Metal3OwnershipError(f"TARGET_BMH_IRONIC_UUID_MISSING:{host_name}")
        try:
            ironic_node = get_ironic_node(ironic_uuid)
        except IronicObservationError as exc:
            raise Metal3OwnershipError(
                f"TARGET_IRONIC_NODE_OBSERVATION_FAILED:{host_name}:{exc}"
            ) from exc
        if ironic_node["maintenance"]:
            raise Metal3OwnershipError(f"TARGET_IRONIC_NODE_IN_MAINTENANCE:{host_name}")
        if ironic_node["target_provision_state"] is not None:
            raise Metal3OwnershipError(f"TARGET_IRONIC_NODE_TRANSITIONING:{host_name}")
        if ironic_node["provision_state"] not in {"active", "available"}:
            raise Metal3OwnershipError(f"TARGET_IRONIC_NODE_NOT_STABLE:{host_name}")
        if ironic_node["last_error"]:
            raise Metal3OwnershipError(f"TARGET_IRONIC_NODE_HAS_ERROR:{host_name}")
        ironic_nodes.append({
            "uuid": ironic_node["uuid"],
            "provision_state": ironic_node["provision_state"],
            "maintenance": ironic_node["maintenance"],
        })
    ownership["target_ironic_inventory_verified"] = True
    ownership["ironic_node_count"] = len(ironic_nodes)
    ownership["ironic_nodes"] = ironic_nodes
    return ownership


def _observe_ready_master_count() -> int:
    result = _kubectl(["get", "nodes", "-o", "json"])
    if not result["available"]:
        raise HandoffError("TARGET_CONTROL_PLANE_NODE_OBSERVATION_FAILED")
    try:
        document = json.loads(result["stdout"])
    except ValueError as exc:
        raise HandoffError("TARGET_CONTROL_PLANE_NODE_JSON_INVALID") from exc
    ready_masters = 0
    for node in document.get("items", []):
        labels = node.get("metadata", {}).get("labels", {})
        is_master = (
            "node-role.kubernetes.io/control-plane" in labels
            or "node-role.kubernetes.io/master" in labels
        )
        is_ready = any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in node.get("status", {}).get("conditions", [])
        )
        if is_master and is_ready:
            ready_masters += 1
    return ready_masters


def _observe_target_capi_pivot(payload: dict[str, Any]) -> dict[str, Any]:
    namespace = payload.get("namespace")
    cluster_name = payload.get("cluster_name")
    source_uid = payload.get("source_cluster_uid")
    stable_identity = payload.get("stable_cluster_identity")
    if not all(isinstance(value, str) and value for value in (
        namespace, cluster_name, source_uid, stable_identity
    )):
        raise HandoffError("TARGET_CAPI_PIVOT_IDENTITY_INPUT_INVALID")
    result = _kubectl([
        "get",
        "cluster.cluster.x-k8s.io",
        cluster_name,
        "--namespace",
        namespace,
        "-o",
        "json",
    ])
    if not result["available"]:
        raise HandoffError("TARGET_CAPI_PIVOT_CLUSTER_NOT_FOUND")
    try:
        cluster = json.loads(result["stdout"])
    except ValueError as exc:
        raise HandoffError("TARGET_CAPI_PIVOT_CLUSTER_JSON_INVALID") from exc
    metadata = cluster.get("metadata", {})
    annotations = metadata.get("annotations", {})
    target_uid = metadata.get("uid")
    generation = metadata.get("generation", 0)
    status = cluster.get("status", {})
    ready = any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in status.get("conditions", [])
    )
    if not isinstance(target_uid, str) or not target_uid or target_uid == source_uid:
        raise HandoffError("TARGET_CAPI_PIVOT_TARGET_UID_INVALID")
    if annotations.get("ai-agent-stack.io/source-cluster-uid") != source_uid:
        raise HandoffError("TARGET_CAPI_PIVOT_SOURCE_UID_ANNOTATION_MISMATCH")
    if annotations.get("ai-agent-stack.io/stable-cluster-identity") != stable_identity:
        raise HandoffError("TARGET_CAPI_PIVOT_STABLE_IDENTITY_MISMATCH")
    if not ready or status.get("observedGeneration", 0) < generation:
        raise HandoffError("TARGET_CAPI_PIVOT_CLUSTER_NOT_READY")
    return {
        "target_cluster_uid": target_uid,
        "target_source_cluster_uid_annotation": annotations[
            "ai-agent-stack.io/source-cluster-uid"
        ],
        "source_stable_cluster_identity": stable_identity,
        "target_stable_cluster_identity": annotations[
            "ai-agent-stack.io/stable-cluster-identity"
        ],
        "target_cluster_ownership_verified": True,
        "target_capi_cluster_ready": True,
        "capi_pivot_succeeded": True,
    }



def _ntp_policy_from_environment() -> tuple[list[str], list[str]]:
    approved_sources = [
        item.strip()
        for item in os.environ.get("APPROVED_EXTERNAL_NTP_SOURCES", "").split(",")
        if item.strip()
    ]
    worker_networks = [
        item.strip()
        for item in os.environ.get("WORKER_NETWORK_CIDRS", "").split(",")
        if item.strip()
    ]
    if not approved_sources or not worker_networks:
        raise NtpAttestationError("TARGET_NTP_TRUSTED_POLICY_CONFIGURATION_REQUIRED")
    return approved_sources, worker_networks


def _observe_control_plane_ntp_bootstrap(payload: dict[str, Any]) -> dict[str, Any]:
    namespace = payload.get("namespace")
    if (
        not isinstance(namespace, str)
        or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", namespace)
    ):
        raise NtpAttestationError("TARGET_NTP_NAMESPACE_INVALID")
    resources = (
        (
            "machines",
            [
                "get",
                "machines.cluster.x-k8s.io",
                "--namespace",
                namespace,
                "-o",
                "json",
            ],
        ),
        (
            "kubeadmconfigs",
            [
                "get",
                "kubeadmconfigs.bootstrap.cluster.x-k8s.io",
                "--namespace",
                namespace,
                "-o",
                "json",
            ],
        ),
    )
    documents = {}
    for name, arguments in resources:
        result = _kubectl(arguments, timeout=60, max_output_bytes=4 * 1024 * 1024)
        if not result["available"]:
            raise NtpAttestationError(f"TARGET_NTP_BOOTSTRAP_OBSERVATION_FAILED:{name}")
        try:
            documents[name] = json.loads(result["stdout"])
        except ValueError as exc:
            raise NtpAttestationError(f"TARGET_NTP_BOOTSTRAP_JSON_INVALID:{name}") from exc
    approved_sources, worker_networks = _ntp_policy_from_environment()
    return verify_control_plane_ntp_bootstrap(
        documents["machines"],
        documents["kubeadmconfigs"],
        approved_sources,
        approved_sources,
        worker_networks,
    )



def _observe_worker_readiness(payload: dict[str, Any]) -> dict[str, Any]:
    namespace = payload.get("namespace")
    deployment_name = payload.get("machine_deployment_name")
    if (
        not isinstance(namespace, str)
        or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", namespace)
        or not isinstance(deployment_name, str)
        or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", deployment_name)
    ):
        raise WorkerAttestationError("WORKER_ATTESTATION_NAMESPACE_OR_DEPLOYMENT_INVALID")
    ntp_ip = os.environ.get("IRONIC_INTERNAL_NTP_IP", "")
    if not ntp_ip:
        raise WorkerAttestationError("IRONIC_INTERNAL_NTP_IP_CONFIGURATION_REQUIRED")
    queries = (
        ("deployment", [
            "get", "machinedeployment.cluster.x-k8s.io", deployment_name,
            "--namespace", namespace, "-o", "json",
        ]),
        ("machines", [
            "get", "machines.cluster.x-k8s.io", "--namespace", namespace, "-o", "json",
        ]),
        ("kubeadmconfigs", [
            "get", "kubeadmconfigs.bootstrap.cluster.x-k8s.io",
            "--namespace", namespace, "-o", "json",
        ]),
        ("nodes", ["get", "nodes", "-o", "json"]),
    )
    documents = {}
    for name, arguments in queries:
        result = _kubectl(arguments, timeout=60, max_output_bytes=16 * 1024 * 1024)
        if not result["available"]:
            raise WorkerAttestationError(f"WORKER_ATTESTATION_OBSERVATION_FAILED:{name}")
        try:
            documents[name] = json.loads(result["stdout"])
        except ValueError as exc:
            raise WorkerAttestationError(
                f"WORKER_ATTESTATION_INVALID_JSON:{name}"
            ) from exc
    deployment_metadata = documents["deployment"].get("metadata", {})
    if (
        deployment_metadata.get("name") != deployment_name
        or deployment_metadata.get("namespace") != namespace
    ):
        raise WorkerAttestationError("WORKER_ATTESTATION_DEPLOYMENT_IDENTITY_MISMATCH")
    return verify_worker_readiness(
        documents["deployment"],
        documents["machines"],
        documents["kubeadmconfigs"],
        documents["nodes"],
        ntp_ip,
    )


def execute(
    command: dict[str, Any],
    lease_lost_event: Any = None,
) -> dict[str, Any]:
    action = command.get("action")
    if action == "target.cluster.observe":
        result = _kubectl(["get", "nodes", "-o", "json"])
        return {
            "status": "succeeded" if result["available"] else "failed",
            "error_code": None if result["available"] else "TARGET_CLUSTER_OBSERVE_FAILED",
            "output": result,
            "side_effects": False,
        }
    if action == "target.capi.observe":
        result = _kubectl([
            "get",
            "clusters,machines,machinedeployments,kubeadmcontrolplanes",
            "-A",
            "-o",
            "json",
        ])
        return {
            "status": "succeeded" if result["available"] else "failed",
            "error_code": None if result["available"] else "TARGET_CAPI_OBSERVE_FAILED",
            "output": result,
            "side_effects": False,
        }
    if action == "target.metal3.verify_ownership":
        try:
            evidence = _observe_target_metal3_ownership(command.get("payload", {}))
            return {
                "status": "succeeded",
                "error_code": None,
                "output": evidence,
                "side_effects": False,
            }
        except (Metal3OwnershipError, TypeError, AttributeError) as exc:
            return {
                "status": "blocked",
                "error_code": str(exc) or "TARGET_METAL3_OWNERSHIP_NOT_VERIFIED",
                "output": {},
                "side_effects": False,
            }
    if action == "target.metal3.observe":
        result = _kubectl([
            "get",
            "baremetalhosts,metal3machines,metal3clusters",
            "-A",
            "-o",
            "json",
        ])
        return {
            "status": "succeeded" if result["available"] else "failed",
            "error_code": None if result["available"] else "TARGET_METAL3_OBSERVE_FAILED",
            "output": result,
            "side_effects": False,
        }
    if action == "target.handoff.evaluate":
        payload = command.get("payload", {})
        checkpoint = command.get("deployment_checkpoint")
        try:
            if not isinstance(checkpoint, dict):
                raise HandoffError("HANDOFF_CHECKPOINT_MISSING")
            next_phase = str(payload.get("next_phase", ""))
            evidence = payload.get("evidence", {})
            if next_phase == HandoffPhase.TARGET_CONTROL_PLANE_READY.value:
                ntp_attestation = _observe_control_plane_ntp_bootstrap(payload)
                ready_master_count = _observe_ready_master_count()
                evidence = {
                    **evidence,
                    **ntp_attestation,
                    "ready_master_count": ready_master_count,
                    "target_control_plane_ready": ready_master_count >= 3,
                }
                configured_ntp_ip = _require_configured_ironic_internal_ntp_ip(
                    evidence.get("ironic_internal_ntp_ip")
                )
                ntp_probe = probe_ironic_internal_ntp_endpoint(configured_ntp_ip)
                evidence = {
                    **evidence,
                    "ironic_internal_ntp_service_reachable": True,
                    "ironic_internal_ntp_probe": ntp_probe,
                }
            if next_phase == HandoffPhase.WORKER_PROVISIONING.value:
                configured_ntp_ip = _require_configured_ironic_internal_ntp_ip(
                    evidence.get("ironic_internal_ntp_ip")
                )
                ntp_probe = probe_ironic_internal_ntp_endpoint(configured_ntp_ip)
                evidence = {
                    **evidence,
                    "ironic_internal_ntp_ip": configured_ntp_ip,
                    "ironic_internal_ntp_service_reachable": True,
                    "ironic_internal_ntp_probe": ntp_probe,
                }
            if next_phase == HandoffPhase.WORKERS_READY.value:
                configured_ntp_ip = _require_configured_ironic_internal_ntp_ip(
                    evidence.get("ironic_internal_ntp_ip")
                )
                observed_workers = _observe_worker_readiness(payload)
                if observed_workers.get("ironic_internal_ntp_ip") != configured_ntp_ip:
                    raise HandoffError("HANDOFF_WORKER_NTP_SOURCE_MISMATCH")
                evidence = {
                    **evidence,
                    **observed_workers,
                    "ironic_internal_ntp_ip": configured_ntp_ip,
                }
            if next_phase == HandoffPhase.EPH_CLEANUP_VERIFIED.value:
                raise HandoffError("EPH_CLEANUP_EXECUTOR_NOT_REGISTERED")
            if next_phase == HandoffPhase.CAPI_PIVOT_VERIFIED.value:
                observed_pivot = _observe_target_capi_pivot({
                    **payload,
                    "source_cluster_uid": evidence.get("source_cluster_uid"),
                })
                evidence = {**evidence, **observed_pivot}
            if next_phase == HandoffPhase.BMH_REGISTRY_MIGRATED.value:
                observed_ownership = _observe_target_metal3_ownership(payload)
                evidence = {**evidence, **observed_ownership}
            current_phase = checkpoint.get("phase")
            if current_phase == "not_started":
                raise HandoffError("HANDOFF_EPH_CHECKPOINT_REQUIRED")
            if current_phase == next_phase:
                validated = validate_phase_evidence(HandoffPhase(next_phase), evidence)
                state_digest = hashlib.sha256(
                    json.dumps(
                        validated,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode("utf-8")
                ).hexdigest()
                stored_state = checkpoint.get("state") or {}
                if stored_state.get("state_digest") != state_digest:
                    raise HandoffError(
                        "HANDOFF_PHASE_ALREADY_RECORDED_WITH_DIFFERENT_EVIDENCE"
                    )
                return {
                    "status": "succeeded",
                    "error_code": None,
                    "output": {
                        "accepted": True,
                        "from_phase": current_phase,
                        "to_phase": next_phase,
                        "evidence": validated,
                        "already_applied": True,
                    },
                    "side_effects": False,
                }
            if next_phase == "eph_single_node_ready":
                raise HandoffError("HANDOFF_EPH_PHASE_OWNED_BY_EPH_AGENT")
            transition = evaluate_transition(
                None if current_phase == "not_started" else current_phase,
                next_phase,
                evidence,
            )
            transition["already_applied"] = False
            return {
                "status": "succeeded",
                "error_code": None,
                "output": transition,
                "side_effects": False,
            }
        except (HandoffError, AttributeError, TypeError, ValueError) as exc:
            return {
                "status": "blocked",
                "error_code": str(exc) or "HANDOFF_EVIDENCE_REJECTED",
                "output": {},
                "side_effects": False,
            }
    if action == "target.ntp.internal.probe":
        payload = command.get("payload", {})
        try:
            ntp_ip = _require_configured_ironic_internal_ntp_ip(
                payload.get("ironic_internal_ntp_ip")
            )
            probe = probe_ironic_internal_ntp_endpoint(
                ntp_ip,
                float(payload.get("timeout_seconds", 3.0)),
            )
            return {
                "status": "succeeded",
                "error_code": None,
                "output": probe,
                "side_effects": False,
            }
        except (NtpPolicyError, TypeError, ValueError, AttributeError) as exc:
            return {
                "status": "blocked",
                "error_code": str(exc),
                "output": {},
                "side_effects": False,
            }
    if action == "target.ntp.control_plane.verify_bootstrap":
        try:
            evidence = _observe_control_plane_ntp_bootstrap(command.get("payload", {}))
            return {
                "status": "succeeded",
                "error_code": None,
                "output": evidence,
                "side_effects": False,
            }
        except (NtpAttestationError, TypeError, AttributeError) as exc:
            return {
                "status": "blocked",
                "error_code": str(exc),
                "output": {},
                "side_effects": False,
            }
    if action == "target.ntp.control_plane.render":
        payload = command.get("payload", {})
        try:
            approved, worker_networks = _ntp_policy_from_environment()
            sources, networks = validate_control_plane_ntp_profile(
                approved,
                approved,
                worker_networks,
            )
            return {
                "status": "succeeded",
                "error_code": None,
                "output": {
                    "profile_rendered": True,
                    "chrony_config": render_control_plane_chrony_config(
                        sources,
                        networks,
                    ),
                    "pre_kubeadm_command": render_control_plane_chrony_pre_kubeadm_command(
                        sources,
                        networks,
                    ),
                    "approved_external_sources": list(sources),
                    "worker_networks": list(networks),
                    "worker_external_fallback_allowed": False,
                    "host_configuration_applied": False,
                },
                "side_effects": False,
            }
        except (NtpPolicyError, TypeError, ValueError, AttributeError) as exc:
            return {
                "status": "blocked",
                "error_code": str(exc),
                "output": {},
                "side_effects": False,
            }
    if action == "target.worker.ntp.validate":
        try:
            ntp_ip = _require_configured_ironic_internal_ntp_ip(
                command.get("payload", {}).get("ironic_internal_ip")
            )
            sources = validate_worker_ntp_sources(
                ntp_ip,
                command.get("payload", {}).get("ntp_sources", []),
            )
            return {
                "status": "succeeded",
                "error_code": None,
                "output": {
                    "validated_ntp_sources": list(sources),
                    "chrony_config": render_worker_chrony_config(sources[0]),
                    "pre_kubeadm_command": render_worker_chrony_pre_kubeadm_command(
                        sources[0]
                    ),
                    "external_ntp_fallback_allowed": False,
                },
                "side_effects": False,
            }
        except (NtpPolicyError, TypeError) as exc:
            return {
                "status": "blocked",
                "error_code": str(exc) or "WORKER_NTP_POLICY_REJECTED",
                "output": {},
                "side_effects": False,
            }
    return {
        "status": "blocked",
        "error_code": "ACTION_NOT_IMPLEMENTED",
        "output": {
            "action": action,
            "reason": "No approved mutation adapter is registered",
        },
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
    parser = argparse.ArgumentParser(prog="target-agent")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--readiness-check", action="store_true")
    args = parser.parse_args()
    if args.healthcheck:
        print(json.dumps({"service": "target-agent", "status": "healthy"}))
        return 0
    try:
        settings = AgentSettings.from_environment("target")
        _configure_in_cluster_kubeconfig()
        client = AgentControlClient(settings)
        if args.readiness_check:
            check = _kubectl(["get", "nodes", "-o", "json"])
            KubernetesLeaseElector(
                identity=settings.instance_id,
                namespace=os.environ.get("AGENT_LEASE_NAMESPACE", "ai-agent-system"),
                lease_name=os.environ.get("AGENT_LEASE_NAME", "target-agent-leader"),
                duration_seconds=int(os.environ.get("AGENT_LEASE_DURATION_SECONDS", "30")),
            )
            ready = check["available"] and client.check_ready()
            client.close()
            print(json.dumps({
                "service": "target-agent",
                "status": "ready" if ready else "not_ready",
            }))
            return 0 if ready else 1
        elector = KubernetesLeaseElector(
            identity=settings.instance_id,
            namespace=os.environ.get("AGENT_LEASE_NAMESPACE", "ai-agent-system"),
            lease_name=os.environ.get("AGENT_LEASE_NAME", "target-agent-leader"),
            duration_seconds=int(os.environ.get("AGENT_LEASE_DURATION_SECONDS", "30")),
        )
    except (ConfigurationError, AgentTransportError, LeaseElectionError, RuntimeError) as exc:
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
                if elector.try_acquire_or_renew():
                    client.heartbeat(sequence, "ready", CAPABILITIES)
                    command = client.claim()
                    if command is not None:
                        started_at = datetime.now(UTC)
                        leader_guard = LeaderLeaseGuard(elector)
                        leader_guard.start()
                        command_guard = CommandLeaseGuard(client, command)
                        command_guard.start()
                        try:
                            outcome = execute(command, command_guard.lost)
                            if leader_guard.lost.is_set():
                                LOG.warning(
                                    "Leadership lost while command %s was executing; "
                                    "result submission is suppressed",
                                    command["command_id"],
                                )
                                continue
                            if command_guard.lost.is_set():
                                outcome = {
                                    "status": "failed",
                                    "error_code": "COMMAND_LEASE_LOST_DURING_EXECUTION",
                                    "output": outcome.get("output", {}),
                                    "side_effects": outcome.get("side_effects", False),
                                }
                            if (
                                command.get("action") == "target.handoff.evaluate"
                                and outcome["status"] == "succeeded"
                                and not outcome["output"].get("already_applied")
                                and not command_guard.lost.is_set()
                                and not leader_guard.lost.is_set()
                            ):
                                try:
                                    checkpoint_result = client.persist_checkpoint(
                                        command,
                                        outcome["output"]["to_phase"],
                                        outcome["output"]["evidence"],
                                    )
                                    outcome["output"]["checkpoint"] = checkpoint_result
                                    outcome["side_effects"] = True
                                except AgentTransportError:
                                    outcome = {
                                        "status": "failed",
                                        "error_code": "HANDOFF_CHECKPOINT_PERSIST_FAILED",
                                        "output": {"phase": outcome["output"]["to_phase"]},
                                        "side_effects": False,
                                    }
                            if not leader_guard.lost.is_set():
                                client.report_result(
                                    _result(command, settings.agent_id, outcome, started_at)
                                )
                        finally:
                            command_guard.close()
                            leader_guard.close()
                else:
                    LOG.debug("This Target Agent replica is not the elected leader")
            except (
                AgentTransportError,
                LeaseElectionError,
                KeyError,
                TypeError,
                ValueError,
            ) as exc:
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
