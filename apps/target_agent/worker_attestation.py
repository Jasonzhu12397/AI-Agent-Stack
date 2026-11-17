from __future__ import annotations

import re
from typing import Any

from .ntp_policy import NtpPolicyError, validate_ironic_internal_ntp_ip


class WorkerAttestationError(ValueError):
    pass


def _ready(document: dict[str, Any]) -> bool:
    return any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in document.get("status", {}).get("conditions", [])
    )


def _owned_by_machine(config: dict[str, Any], machine: dict[str, Any]) -> bool:
    metadata = config.get("metadata", {})
    machine_metadata = machine.get("metadata", {})
    return any(
        owner.get("kind") == "Machine"
        and owner.get("name") == machine_metadata.get("name")
        and owner.get("uid") == machine_metadata.get("uid")
        and owner.get("controller") is True
        for owner in metadata.get("ownerReferences", [])
    )


def verify_worker_readiness(
    deployment: dict[str, Any],
    machines_document: dict[str, Any],
    kubeadmconfigs_document: dict[str, Any],
    nodes_document: dict[str, Any],
    ironic_internal_ntp_ip: str,
) -> dict[str, Any]:
    try:
        ntp_ip = validate_ironic_internal_ntp_ip(ironic_internal_ntp_ip)
    except (NtpPolicyError, TypeError, ValueError) as exc:
        raise WorkerAttestationError("WORKER_ATTESTATION_INTERNAL_NTP_IP_INVALID") from exc

    metadata = deployment.get("metadata", {})
    deployment_name = metadata.get("name")
    namespace = metadata.get("namespace")
    spec = deployment.get("spec", {})
    selector = spec.get("selector", {}).get("matchLabels", {})
    replicas = spec.get("replicas", 1)
    if (
        not isinstance(deployment_name, str)
        or not deployment_name
        or not isinstance(namespace, str)
        or not namespace
        or not isinstance(selector, dict)
        or not selector
        or not isinstance(replicas, int)
        or isinstance(replicas, bool)
        or replicas < 1
    ):
        raise WorkerAttestationError("WORKER_ATTESTATION_DEPLOYMENT_SPEC_INVALID")

    configs = {}
    for config in kubeadmconfigs_document.get("items", []):
        name = config.get("metadata", {}).get("name")
        if isinstance(name, str):
            configs[name] = config

    nodes = {}
    for node in nodes_document.get("items", []):
        name = node.get("metadata", {}).get("name")
        if isinstance(name, str):
            nodes[name] = node

    matched = []
    for machine in machines_document.get("items", []):
        machine_metadata = machine.get("metadata", {})
        labels = machine_metadata.get("labels", {})
        if machine_metadata.get("namespace") != namespace:
            continue
        if labels.get("cluster.x-k8s.io/deployment-name") != deployment_name:
            continue
        if any(labels.get(key) != value for key, value in selector.items()):
            continue
        if machine_metadata.get("deletionTimestamp"):
            raise WorkerAttestationError("WORKER_ATTESTATION_MACHINE_DELETING")
        if not _ready(machine) or machine.get("status", {}).get("bootstrapReady") is not True:
            raise WorkerAttestationError("WORKER_ATTESTATION_MACHINE_NOT_READY")

        node_ref = machine.get("status", {}).get("nodeRef", {})
        node_name = node_ref.get("name")
        if not isinstance(node_name, str) or not node_name:
            raise WorkerAttestationError("WORKER_ATTESTATION_NODE_REFERENCE_MISSING")
        node = nodes.get(node_name)
        if node is None or not _ready(node):
            raise WorkerAttestationError("WORKER_ATTESTATION_KUBERNETES_NODE_NOT_READY")

        bootstrap_ref = machine.get("spec", {}).get("bootstrap", {}).get("configRef", {})
        config_name = bootstrap_ref.get("name")
        if bootstrap_ref.get("kind") != "KubeadmConfig" or not isinstance(config_name, str):
            raise WorkerAttestationError("WORKER_ATTESTATION_BOOTSTRAP_REFERENCE_INVALID")
        config = configs.get(config_name)
        if config is None or not _owned_by_machine(config, machine):
            raise WorkerAttestationError("WORKER_ATTESTATION_KUBEADMCONFIG_OWNER_MISMATCH")
        if config.get("status", {}).get("ready") is not True:
            raise WorkerAttestationError("WORKER_ATTESTATION_KUBEADMCONFIG_NOT_READY")

        commands = config.get("spec", {}).get("preKubeadmCommands", [])
        if not isinstance(commands, list) or not commands or not all(
            isinstance(command, str) for command in commands
        ):
            raise WorkerAttestationError("WORKER_ATTESTATION_NTP_BOOTSTRAP_COMMAND_MISSING")
        script = "\n".join(commands)
        sources = re.findall(r"(?m)^\s*server\s+(\S+)", script)
        if sources != [ntp_ip] or re.search(r"(?m)^\s*pool\s+", script):
            raise WorkerAttestationError("WORKER_ATTESTATION_NTP_SOURCE_MISMATCH")
        required_markers = (
            "chronyc -n sources",
            "chronyc tracking",
            "Leap status",
            "systemctl restart",
            "$2 == server",
            "selected = 1",
        )
        if not all(marker in script for marker in required_markers):
            raise WorkerAttestationError("WORKER_ATTESTATION_CHRONY_SYNC_GATE_MISSING")

        matched.append({
            "machine": machine_metadata.get("name"),
            "machine_uid": machine_metadata.get("uid"),
            "node": node_name,
            "kubeadmconfig": config_name,
            "ironic_internal_ntp_ip": ntp_ip,
            "ntp_bootstrap_sync_gate_verified": True,
        })

    if len(matched) != replicas:
        raise WorkerAttestationError("WORKER_ATTESTATION_REPLICA_COUNT_MISMATCH")
    if len({item["node"] for item in matched}) != replicas:
        raise WorkerAttestationError("WORKER_ATTESTATION_NODE_REFERENCE_DUPLICATE")

    return {
        "deployment": deployment_name,
        "namespace": namespace,
        "desired_worker_count": replicas,
        "ready_worker_count": len(matched),
        "verified_workers": matched,
        "worker_ntp_bootstrap_sources": {
            item["node"]: [ntp_ip] for item in matched
        },
        "workers_ready": True,
        "side_effects": False,
    }
