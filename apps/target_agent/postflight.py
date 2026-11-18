from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Callable
from typing import Any


class PostflightError(ValueError):
    pass


def _query_json(
    runner: Callable[..., dict[str, Any]],
    arguments: list[str],
    label: str,
) -> dict[str, Any]:
    result = runner(arguments, timeout=60, max_output_bytes=24 * 1024 * 1024)
    if not isinstance(result, dict) or result.get("available") is not True:
        raise PostflightError(f"POSTFLIGHT_API_QUERY_FAILED:{label}")
    try:
        document = json.loads(result.get("stdout", ""))
    except (TypeError, ValueError) as exc:
        raise PostflightError(f"POSTFLIGHT_API_JSON_INVALID:{label}") from exc
    if not isinstance(document, dict):
        raise PostflightError(f"POSTFLIGHT_API_DOCUMENT_INVALID:{label}")
    return document


def _ready_condition(conditions: Any) -> bool:
    if not isinstance(conditions, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("type") == "Ready"
        and item.get("status") == "True"
        for item in conditions
    )


def _control_plane_node(node: dict[str, Any]) -> bool:
    labels = node.get("metadata", {}).get("labels", {})
    return (
        "node-role.kubernetes.io/control-plane" in labels
        or "node-role.kubernetes.io/master" in labels
    )


def _plugin_types(value: Any) -> set[str]:
    found = set()
    if isinstance(value, dict):
        plugin_type = value.get("type")
        if isinstance(plugin_type, str):
            found.add(plugin_type.lower())
        for item in value.values():
            found.update(_plugin_types(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_plugin_types(item))
    return found


def _sriov_resource_names(value: Any) -> set[str]:
    found = set()
    if isinstance(value, dict):
        plugin_type = value.get("type")
        resource_name = value.get("resourceName")
        if (
            isinstance(plugin_type, str)
            and "sriov" in plugin_type.lower()
            and isinstance(resource_name, str)
            and resource_name
        ):
            found.add(resource_name)
        for item in value.values():
            found.update(_sriov_resource_names(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_sriov_resource_names(item))
    return found


def _daemonset_ready(daemonset: dict[str, Any]) -> bool:
    status = daemonset.get("status", {})
    desired = status.get("desiredNumberScheduled", 0)
    ready = status.get("numberReady", 0)
    available = status.get("numberAvailable", ready)
    return (
        isinstance(desired, int)
        and desired > 0
        and isinstance(ready, int)
        and isinstance(available, int)
        and ready >= desired
        and available >= desired
    )


def _find_ready_daemonset(
    daemonsets: list[dict[str, Any]],
    patterns: tuple[str, ...],
) -> str | None:
    for item in daemonsets:
        metadata = item.get("metadata", {})
        labels = metadata.get("labels", {})
        identity = " ".join(
            [
                str(metadata.get("name", "")),
                *(str(key) for key in labels),
                *(str(value) for value in labels.values()),
            ]
        ).lower()
        if any(pattern in identity for pattern in patterns) and _daemonset_ready(item):
            return f"{metadata.get('namespace', '')}/{metadata.get('name', '')}"
    return None


def _quantity_is_positive(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    if isinstance(value, str):
        match = re.fullmatch(r"([0-9]+)", value.strip())
        return bool(match and int(match.group(1)) > 0)
    return False


def verify_target_postflight(
    *,
    cluster_name: str,
    namespace: str,
    machine_deployment_name: str,
    desired_worker_count: int,
    ironic_internal_ntp_ip: str,
    worker_ntp_bootstrap_sources: dict[str, list[str]],
    worker_network_contract: dict[str, Any],
    ownership_evidence: dict[str, Any],
    deployment_checkpoint_durable: bool,
    runner: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(cluster_name, str) or not cluster_name:
        raise PostflightError("POSTFLIGHT_TARGET_CLUSTER_NAME_REQUIRED")
    if not isinstance(namespace, str) or not namespace:
        raise PostflightError("POSTFLIGHT_TARGET_NAMESPACE_REQUIRED")
    if not isinstance(machine_deployment_name, str) or not machine_deployment_name:
        raise PostflightError("POSTFLIGHT_MACHINE_DEPLOYMENT_NAME_REQUIRED")
    if (
        not isinstance(desired_worker_count, int)
        or isinstance(desired_worker_count, bool)
        or desired_worker_count < 1
    ):
        raise PostflightError("POSTFLIGHT_DESIRED_WORKER_COUNT_INVALID")
    if not isinstance(worker_network_contract, dict):
        raise PostflightError("POSTFLIGHT_WORKER_NETWORK_CONTRACT_REQUIRED")
    if deployment_checkpoint_durable is not True:
        raise PostflightError("POSTFLIGHT_DURABLE_WORKER_CHECKPOINT_REQUIRED")
    if (
        ownership_evidence.get("target_metal3_ownership_verified") is not True
        or ownership_evidence.get("target_metal3_reconciliation_healthy") is not True
        or ownership_evidence.get("target_ironic_inventory_verified") is not True
    ):
        raise PostflightError("POSTFLIGHT_METAL3_IRONIC_OWNERSHIP_NOT_HEALTHY")
    if (
        not isinstance(worker_ntp_bootstrap_sources, dict)
        or len(worker_ntp_bootstrap_sources) != desired_worker_count
    ):
        raise PostflightError("POSTFLIGHT_WORKER_NTP_EVIDENCE_INCOMPLETE")
    for worker_name, sources in worker_ntp_bootstrap_sources.items():
        if (
            not isinstance(worker_name, str)
            or not worker_name
            or sources != [ironic_internal_ntp_ip]
        ):
            raise PostflightError(f"POSTFLIGHT_WORKER_NTP_SOURCE_MISMATCH:{worker_name}")

    contract_flags = (
        "calico_enabled",
        "multus_enabled",
        "ovs_enabled",
        "sriov_cni_enabled",
        "sriov_dpdk_enabled",
    )
    if any(worker_network_contract.get(flag) is not True for flag in contract_flags):
        raise PostflightError("POSTFLIGHT_NETWORK_CONTRACT_COMPONENT_DISABLED")
    if worker_network_contract.get("pod_traffic_network") != "data":
        raise PostflightError("POSTFLIGHT_POD_TRAFFIC_NOT_BOUND_TO_DATA_PLANE")
    if worker_network_contract.get("control_plane_interface") == worker_network_contract.get(
        "data_plane_interface"
    ):
        raise PostflightError("POSTFLIGHT_CONTROL_DATA_INTERFACE_COLLISION")
    try:
        control_cidr = ipaddress.ip_network(
            worker_network_contract.get("control_plane_cidr"),
            strict=False,
        )
        data_cidr = ipaddress.ip_network(
            worker_network_contract.get("data_plane_cidr"),
            strict=False,
        )
    except (TypeError, ValueError) as exc:
        raise PostflightError("POSTFLIGHT_NETWORK_CIDR_INVALID") from exc
    if control_cidr.version != data_cidr.version or control_cidr.overlaps(data_cidr):
        raise PostflightError("POSTFLIGHT_CONTROL_DATA_CIDR_OVERLAP")

    cluster = _query_json(
        runner,
        ["get", "clusters.cluster.x-k8s.io", cluster_name, "--namespace", namespace, "-o", "json"],
        "target-cluster",
    )
    cluster_status = cluster.get("status", {})
    if (
        cluster.get("metadata", {}).get("name") != cluster_name
        or cluster.get("metadata", {}).get("namespace") != namespace
        or cluster_status.get("infrastructureReady") is not True
        or cluster_status.get("controlPlaneReady") is not True
    ):
        raise PostflightError("POSTFLIGHT_CAPI_CLUSTER_NOT_READY")
    control_plane_ref = cluster.get("spec", {}).get("controlPlaneRef", {})
    if (
        control_plane_ref.get("apiGroup") != "controlplane.cluster.x-k8s.io"
        or control_plane_ref.get("kind") != "KubeadmControlPlane"
        or not isinstance(control_plane_ref.get("name"), str)
    ):
        raise PostflightError("POSTFLIGHT_KUBEADM_CONTROL_PLANE_REFERENCE_INVALID")
    control_plane = _query_json(
        runner,
        [
            "get",
            "kubeadmcontrolplanes.controlplane.cluster.x-k8s.io",
            control_plane_ref["name"],
            "--namespace",
            namespace,
            "-o",
            "json",
        ],
        "kubeadm-control-plane",
    )
    control_plane_spec = control_plane.get("spec", {})
    control_plane_status = control_plane.get("status", {})
    if (
        control_plane_spec.get("replicas") != 3
        or control_plane_status.get("readyReplicas") != 3
        or control_plane_status.get("initialized") is not True
    ):
        raise PostflightError("POSTFLIGHT_THREE_CONTROL_PLANES_NOT_READY")

    machine_deployment = _query_json(
        runner,
        [
            "get",
            "machinedeployments.cluster.x-k8s.io",
            machine_deployment_name,
            "--namespace",
            namespace,
            "-o",
            "json",
        ],
        "worker-machine-deployment",
    )
    md_spec = machine_deployment.get("spec", {})
    md_status = machine_deployment.get("status", {})
    generation = machine_deployment.get("metadata", {}).get("generation", 0)
    if (
        md_spec.get("replicas") != desired_worker_count
        or md_status.get("readyReplicas") != desired_worker_count
        or md_status.get("updatedReplicas") != desired_worker_count
        or md_status.get("availableReplicas") != desired_worker_count
        or md_status.get("observedGeneration", 0) < generation
    ):
        raise PostflightError("POSTFLIGHT_WORKER_MACHINE_DEPLOYMENT_NOT_READY")

    machines_document = _query_json(
        runner,
        ["get", "machines.cluster.x-k8s.io", "--namespace", namespace, "-o", "json"],
        "worker-machines",
    )
    machines = machines_document.get("items")
    if not isinstance(machines, list):
        raise PostflightError("POSTFLIGHT_MACHINE_INVENTORY_INVALID")
    worker_machines = [
        machine
        for machine in machines
        if machine.get("metadata", {}).get("labels", {}).get(
            "cluster.x-k8s.io/deployment-name"
        ) == machine_deployment_name
        and machine.get("metadata", {}).get("labels", {}).get(
            "cluster.x-k8s.io/cluster-name"
        ) == cluster_name
    ]
    if len(worker_machines) != desired_worker_count or any(
        machine.get("status", {}).get("phase") != "Running"
        or machine.get("status", {}).get("infrastructureReady") is not True
        or machine.get("status", {}).get("bootstrapReady") is not True
        for machine in worker_machines
    ):
        raise PostflightError("POSTFLIGHT_CAPI_WORKER_MACHINES_NOT_READY")

    nodes_document = _query_json(runner, ["get", "nodes", "-o", "json"], "nodes")
    nodes = nodes_document.get("items")
    if not isinstance(nodes, list):
        raise PostflightError("POSTFLIGHT_NODE_INVENTORY_INVALID")
    worker_nodes = [
        node for node in nodes
        if isinstance(node, dict) and not _control_plane_node(node)
    ]
    ready_workers = [
        node for node in worker_nodes
        if _ready_condition(node.get("status", {}).get("conditions"))
    ]
    if len(ready_workers) != desired_worker_count:
        raise PostflightError("POSTFLIGHT_READY_WORKER_NODE_COUNT_MISMATCH")

    daemonset_document = _query_json(
        runner,
        ["get", "daemonsets.apps", "--all-namespaces", "-o", "json"],
        "daemonsets",
    )
    daemonsets = daemonset_document.get("items")
    if not isinstance(daemonsets, list):
        raise PostflightError("POSTFLIGHT_DAEMONSET_INVENTORY_INVALID")
    calico = _find_ready_daemonset(daemonsets, ("calico-node",))
    multus = _find_ready_daemonset(daemonsets, ("multus",))
    if not calico:
        raise PostflightError("POSTFLIGHT_CALICO_DAEMONSET_NOT_READY")
    if not multus:
        raise PostflightError("POSTFLIGHT_MULTUS_DAEMONSET_NOT_READY")
    sriov_daemonset = _find_ready_daemonset(
        daemonsets,
        ("sriov-cni", "sriov-network", "sriov-config-daemon"),
    )
    if not sriov_daemonset:
        raise PostflightError("POSTFLIGHT_SRIOV_CNI_DAEMONSET_NOT_READY")

    nad_document = _query_json(
        runner,
        [
            "get",
            "network-attachment-definitions.k8s.cni.cncf.io",
            "--all-namespaces",
            "-o",
            "json",
        ],
        "network-attachment-definitions",
    )
    nads = nad_document.get("items")
    if not isinstance(nads, list):
        raise PostflightError("POSTFLIGHT_NETWORK_ATTACHMENT_INVENTORY_INVALID")
    sriov_nads = []
    ovs_nads = []
    sriov_resource_names = set()
    for nad in nads:
        config = nad.get("spec", {}).get("config")
        if not isinstance(config, str):
            continue
        try:
            config_document = json.loads(config)
        except ValueError:
            continue
        plugin_types = _plugin_types(config_document)
        if any("sriov" in item for item in plugin_types):
            sriov_nads.append(nad)
            sriov_resource_names.update(_sriov_resource_names(config_document))
        if any("ovs" in item for item in plugin_types):
            ovs_nads.append(nad)
    if not sriov_nads or not sriov_resource_names:
        raise PostflightError("POSTFLIGHT_SRIOV_DATA_PLANE_NAD_MISSING")
    if not ovs_nads:
        raise PostflightError("POSTFLIGHT_OVS_DATA_PLANE_NAD_MISSING")

    dpdk_resource_names = {
        name
        for node in ready_workers
        for name, quantity in node.get("status", {}).get("allocatable", {}).items()
        if "dpdk" in name.lower() and _quantity_is_positive(quantity)
    }
    if not dpdk_resource_names:
        raise PostflightError("POSTFLIGHT_DPDK_DEVICE_RESOURCE_MISSING")
    for node in ready_workers:
        allocatable = node.get("status", {}).get("allocatable", {})
        if not any(
            _quantity_is_positive(allocatable.get(name))
            for name in sriov_resource_names
        ):
            raise PostflightError(
                f"POSTFLIGHT_SRIOV_RESOURCE_NOT_ADVERTISED:{node.get('metadata', {}).get('name')}"
            )
        if not any(
            _quantity_is_positive(allocatable.get(name))
            for name in dpdk_resource_names
        ):
            raise PostflightError(
                f"POSTFLIGHT_DPDK_RESOURCE_NOT_ADVERTISED:{node.get('metadata', {}).get('name')}"
            )

    return {
        "target_postflight_passed": True,
        "worker_network_postflight_passed": True,
        "deployment_checkpoint_durable": True,
        "ready_control_plane_count": control_plane_status.get("readyReplicas"),
        "ready_worker_count": len(ready_workers),
        "worker_ntp_bootstrap_attested": True,
        "calico_daemonset": calico,
        "multus_daemonset": multus,
        "sriov_daemonset": sriov_daemonset,
        "sriov_nad_count": len(sriov_nads),
        "ovs_nad_count": len(ovs_nads),
        "sriov_resource_names": sorted(sriov_resource_names),
        "dpdk_resource_names": sorted(dpdk_resource_names),
        "worker_network_contract_validated": True,
        "target_metal3_ownership_verified": True,
        "target_ironic_inventory_verified": True,
    }
