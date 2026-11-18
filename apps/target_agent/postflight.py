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
    if not isinstance(node, dict):
        raise PostflightError("POSTFLIGHT_NODE_DOCUMENT_INVALID")
    metadata = node.get("metadata", {})
    if not isinstance(metadata, dict):
        raise PostflightError("POSTFLIGHT_NODE_METADATA_INVALID")
    labels = metadata.get("labels", {})
    if not isinstance(labels, dict):
        raise PostflightError("POSTFLIGHT_NODE_LABELS_INVALID")
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
    if not isinstance(daemonset, dict):
        raise PostflightError("POSTFLIGHT_DAEMONSET_DOCUMENT_INVALID")
    status = daemonset.get("status", {})
    if not isinstance(status, dict):
        raise PostflightError("POSTFLIGHT_DAEMONSET_STATUS_INVALID")
    desired = status.get("desiredNumberScheduled", 0)
    ready = status.get("numberReady", 0)
    available = status.get("numberAvailable", ready)
    counters = (desired, ready, available)
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in counters
    ):
        raise PostflightError("POSTFLIGHT_DAEMONSET_COUNTER_INVALID")
    return desired > 0 and ready >= desired and available >= desired


def _find_ready_daemonset(
    daemonsets: list[dict[str, Any]],
    patterns: tuple[str, ...],
) -> str | None:
    for item in daemonsets:
        if not isinstance(item, dict):
            raise PostflightError("POSTFLIGHT_DAEMONSET_DOCUMENT_INVALID")
        metadata = item.get("metadata", {})
        if not isinstance(metadata, dict):
            raise PostflightError("POSTFLIGHT_DAEMONSET_METADATA_INVALID")
        labels = metadata.get("labels", {})
        if not isinstance(labels, dict):
            raise PostflightError("POSTFLIGHT_DAEMONSET_LABELS_INVALID")
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
    deployment_id: str,
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
    if not isinstance(ownership_evidence, dict):
        raise PostflightError("POSTFLIGHT_OWNERSHIP_EVIDENCE_INVALID")
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
    cluster_metadata = cluster.get("metadata", {})
    cluster_spec = cluster.get("spec", {})
    cluster_status = cluster.get("status", {})
    if not all(isinstance(value, dict) for value in (cluster_metadata, cluster_spec, cluster_status)):
        raise PostflightError("POSTFLIGHT_CAPI_CLUSTER_DOCUMENT_INVALID")
    if (
        cluster_metadata.get("name") != cluster_name
        or cluster_metadata.get("namespace") != namespace
        or cluster_status.get("infrastructureReady") is not True
        or cluster_status.get("controlPlaneReady") is not True
    ):
        raise PostflightError("POSTFLIGHT_CAPI_CLUSTER_NOT_READY")
    control_plane_ref = cluster_spec.get("controlPlaneRef", {})
    if not isinstance(control_plane_ref, dict):
        raise PostflightError("POSTFLIGHT_KUBEADM_CONTROL_PLANE_REFERENCE_INVALID")
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
    if not isinstance(control_plane_spec, dict) or not isinstance(control_plane_status, dict):
        raise PostflightError("POSTFLIGHT_KUBEADM_CONTROL_PLANE_DOCUMENT_INVALID")
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
    md_metadata = machine_deployment.get("metadata", {})
    if not isinstance(md_metadata, dict):
        raise PostflightError("POSTFLIGHT_MACHINE_DEPLOYMENT_METADATA_INVALID")
    md_labels = md_metadata.get("labels", {})
    md_annotations = md_metadata.get("annotations", {})
    md_spec = machine_deployment.get("spec", {})
    md_status = machine_deployment.get("status", {})
    if not all(isinstance(value, dict) for value in (md_metadata, md_labels, md_annotations, md_spec, md_status)):
        raise PostflightError("POSTFLIGHT_MACHINE_DEPLOYMENT_DOCUMENT_INVALID")
    if (
        md_metadata.get("name") != machine_deployment_name
        or md_metadata.get("namespace") != namespace
        or md_labels.get("app.kubernetes.io/managed-by") != "target-agent"
        or md_labels.get("ai-agent-stack.io/deployment-id") != deployment_id
        or md_annotations.get("ai-agent-stack.io/target-cluster-uid")
        != ownership_evidence.get("target_cluster_uid")
        or md_spec.get("clusterName") != cluster_name
        or not isinstance(md_spec.get("template"), dict)
        or not isinstance(md_spec.get("template", {}).get("spec"), dict)
        or md_spec.get("template", {}).get("spec", {}).get("version")
        != control_plane_spec.get("version")
    ):
        raise PostflightError("POSTFLIGHT_MACHINE_DEPLOYMENT_OWNERSHIP_MISMATCH")
    generation = md_metadata.get("generation", 0)
    replica_counts = (
        md_spec.get("replicas"),
        md_status.get("readyReplicas"),
        md_status.get("updatedReplicas"),
        md_status.get("availableReplicas"),
    )
    observed_generation = md_status.get("observedGeneration", 0)
    if (
        any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (*replica_counts, generation, observed_generation)
        )
        or md_spec.get("replicas") != desired_worker_count
        or md_status.get("readyReplicas") != desired_worker_count
        or md_status.get("updatedReplicas") != desired_worker_count
        or md_status.get("availableReplicas") != desired_worker_count
        or observed_generation < generation
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
    worker_machines = []
    for machine in machines:
        if not isinstance(machine, dict):
            raise PostflightError("POSTFLIGHT_CAPI_WORKER_MACHINE_INVALID")
        metadata = machine.get("metadata", {})
        if not isinstance(metadata, dict):
            raise PostflightError("POSTFLIGHT_CAPI_WORKER_MACHINE_METADATA_INVALID")
        labels = metadata.get("labels", {})
        if not isinstance(labels, dict):
            raise PostflightError("POSTFLIGHT_CAPI_WORKER_MACHINE_LABELS_INVALID")
        if (
            labels.get("cluster.x-k8s.io/deployment-name") == machine_deployment_name
            and labels.get("cluster.x-k8s.io/cluster-name") == cluster_name
        ):
            worker_machines.append(machine)
    if not isinstance(worker_machines, list) or len(worker_machines) != desired_worker_count:
        raise PostflightError("POSTFLIGHT_CAPI_WORKER_MACHINES_NOT_READY")
    worker_node_names = []
    for machine in worker_machines:
        if not isinstance(machine, dict):
            raise PostflightError("POSTFLIGHT_CAPI_WORKER_MACHINE_INVALID")
        status = machine.get("status", {})
        if not isinstance(status, dict):
            raise PostflightError("POSTFLIGHT_CAPI_WORKER_MACHINE_STATUS_INVALID")
        node_ref = status.get("nodeRef", {})
        node_name = node_ref.get("name") if isinstance(node_ref, dict) else None
        if (
            status.get("phase") != "Running"
            or status.get("infrastructureReady") is not True
            or status.get("bootstrapReady") is not True
            or not isinstance(node_name, str)
            or not node_name
        ):
            raise PostflightError("POSTFLIGHT_CAPI_WORKER_MACHINES_NOT_READY")
        worker_node_names.append(node_name)
    if len(set(worker_node_names)) != desired_worker_count:
        raise PostflightError("POSTFLIGHT_CAPI_WORKER_NODE_REFERENCE_DUPLICATE")
    if set(worker_ntp_bootstrap_sources) != set(worker_node_names):
        raise PostflightError("POSTFLIGHT_WORKER_NTP_NODE_SET_MISMATCH")

    nodes_document = _query_json(runner, ["get", "nodes", "-o", "json"], "nodes")
    nodes = nodes_document.get("items")
    if not isinstance(nodes, list):
        raise PostflightError("POSTFLIGHT_NODE_INVENTORY_INVALID")
    nodes_by_name = {}
    for node in nodes:
        if not isinstance(node, dict):
            raise PostflightError("POSTFLIGHT_NODE_DOCUMENT_INVALID")
        metadata = node.get("metadata", {})
        if not isinstance(metadata, dict):
            raise PostflightError("POSTFLIGHT_NODE_METADATA_INVALID")
        node_name = metadata.get("name")
        if not isinstance(node_name, str) or not node_name or node_name in nodes_by_name:
            raise PostflightError("POSTFLIGHT_NODE_IDENTITY_INVALID")
        node_status = node.get("status", {})
        if not isinstance(node_status, dict):
            raise PostflightError("POSTFLIGHT_NODE_STATUS_INVALID")
        nodes_by_name[node_name] = node
    control_plane_nodes = [
        node for node in nodes_by_name.values() if _control_plane_node(node)
    ]
    if len(control_plane_nodes) != 3 or any(
        not _ready_condition(node.get("status", {}).get("conditions"))
        for node in control_plane_nodes
    ):
        raise PostflightError("POSTFLIGHT_READY_CONTROL_PLANE_NODE_COUNT_MISMATCH")
    ready_workers = []
    for node_name in worker_node_names:
        node = nodes_by_name.get(node_name)
        node_status = node.get("status", {}) if isinstance(node, dict) else {}
        if node is not None and not isinstance(node_status, dict):
            raise PostflightError("POSTFLIGHT_NODE_STATUS_INVALID")
        if (
            node is None
            or _control_plane_node(node)
            or not _ready_condition(node_status.get("conditions"))
        ):
            raise PostflightError(f"POSTFLIGHT_WORKER_NODE_NOT_READY:{node_name}")
        ready_workers.append(node)

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
    ovs_daemonset = _find_ready_daemonset(daemonsets, ("ovs-cni",))
    ovs_runtime_daemonset = _find_ready_daemonset(
        daemonsets,
        ("ovs-vswitchd", "openvswitch"),
    )
    if not ovs_daemonset or not ovs_runtime_daemonset:
        raise PostflightError("POSTFLIGHT_OVS_DAEMONSET_NOT_READY")
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
    required_attachments = worker_network_contract.get("required_network_attachments")
    if not isinstance(required_attachments, list) or not required_attachments:
        raise PostflightError("POSTFLIGHT_REQUIRED_NETWORK_ATTACHMENTS_INVALID")
    observed_nads = {}
    for nad in nads:
        if not isinstance(nad, dict):
            raise PostflightError("POSTFLIGHT_NETWORK_ATTACHMENT_DOCUMENT_INVALID")
        metadata = nad.get("metadata", {})
        spec = nad.get("spec", {})
        if not isinstance(metadata, dict) or not isinstance(spec, dict):
            raise PostflightError("POSTFLIGHT_NETWORK_ATTACHMENT_METADATA_OR_SPEC_INVALID")
        nad_namespace = metadata.get("namespace")
        nad_name = metadata.get("name")
        if (
            not isinstance(nad_namespace, str)
            or not nad_namespace
            or not isinstance(nad_name, str)
            or not nad_name
        ):
            raise PostflightError("POSTFLIGHT_NETWORK_ATTACHMENT_IDENTITY_INVALID")
        key = (nad_namespace, nad_name)
        if key in observed_nads:
            raise PostflightError("POSTFLIGHT_NETWORK_ATTACHMENT_IDENTITY_DUPLICATE")
        observed_nads[key] = nad
    for requirement in required_attachments:
        if not isinstance(requirement, dict):
            raise PostflightError("POSTFLIGHT_REQUIRED_NETWORK_ATTACHMENT_INVALID")
        required_namespace = requirement.get("namespace")
        required_name = requirement.get("name")
        expected_type = requirement.get("cni_type")
        expected_resource = requirement.get("resource_name")
        if (
            not isinstance(required_namespace, str)
            or not required_namespace
            or not isinstance(required_name, str)
            or not required_name
            or not isinstance(expected_type, str)
            or expected_type not in {"sriov", "ovs"}
            or (
                expected_type == "sriov"
                and (not isinstance(expected_resource, str) or not expected_resource)
            )
            or (expected_type == "ovs" and expected_resource is not None)
        ):
            raise PostflightError("POSTFLIGHT_REQUIRED_NETWORK_ATTACHMENT_INVALID")
        nad = observed_nads.get((required_namespace, required_name))
        if nad is None:
            raise PostflightError(
                f"POSTFLIGHT_REQUIRED_NETWORK_ATTACHMENT_MISSING:{required_namespace}/{required_name}"
            )
        config = nad.get("spec", {}).get("config")
        if not isinstance(config, str):
            raise PostflightError("POSTFLIGHT_NETWORK_ATTACHMENT_CONFIG_INVALID")
        try:
            config_document = json.loads(config)
        except (TypeError, ValueError) as exc:
            raise PostflightError("POSTFLIGHT_NETWORK_ATTACHMENT_CONFIG_INVALID") from exc
        if not isinstance(config_document, dict):
            raise PostflightError("POSTFLIGHT_NETWORK_ATTACHMENT_CONFIG_INVALID")
        plugin_types = _plugin_types(config_document)
        if expected_type not in plugin_types:
            raise PostflightError(
                f"POSTFLIGHT_NETWORK_ATTACHMENT_CNI_TYPE_MISMATCH:{required_namespace}/{required_name}"
            )
        if expected_type == "sriov":
            resource_names = _sriov_resource_names(config_document)
            if expected_resource not in resource_names:
                raise PostflightError(
                    f"POSTFLIGHT_NETWORK_ATTACHMENT_RESOURCE_MISMATCH:{required_namespace}/{required_name}"
                )
            sriov_nads.append(nad)
            sriov_resource_names.add(expected_resource)
        else:
            ovs_nads.append(nad)
    if not sriov_nads or not sriov_resource_names:
        raise PostflightError("POSTFLIGHT_SRIOV_DATA_PLANE_NAD_MISSING")
    if not ovs_nads:
        raise PostflightError("POSTFLIGHT_OVS_DATA_PLANE_NAD_MISSING")

    dpdk_resources_by_worker = [
        {
            name
            for name, quantity in node.get("status", {}).get("allocatable", {}).items()
            if "dpdk" in name.lower() and _quantity_is_positive(quantity)
        }
        for node in ready_workers
    ]
    dpdk_resource_names = (
        set.intersection(*dpdk_resources_by_worker)
        if dpdk_resources_by_worker
        else set()
    )
    if not dpdk_resource_names:
        raise PostflightError("POSTFLIGHT_DPDK_DEVICE_RESOURCE_MISSING")
    for node in ready_workers:
        node_status = node.get("status", {})
        if not isinstance(node_status, dict):
            raise PostflightError("POSTFLIGHT_NODE_STATUS_INVALID")
        allocatable = node_status.get("allocatable", {})
        if not isinstance(allocatable, dict):
            raise PostflightError("POSTFLIGHT_NODE_ALLOCATABLE_INVALID")
        if not all(
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
        "worker_network_postflight_passed": False,
        "deployment_checkpoint_durable": True,
        "ready_control_plane_count": len(control_plane_nodes),
        "ready_worker_count": len(ready_workers),
        "worker_ntp_bootstrap_attested": True,
        "worker_chronyd_bootstrap_sync_verified": True,
        "worker_chronyd_live_tracking_verified": False,
        "data_plane_pod_packet_path_verified": False,
        "postflight_scope": "capi-metal3-worker-and-network-stack-readiness",
        "calico_daemonset": calico,
        "multus_daemonset": multus,
        "sriov_daemonset": sriov_daemonset,
        "ovs_daemonset": ovs_daemonset,
        "ovs_runtime_daemonset": ovs_runtime_daemonset,
        "sriov_nad_count": len(sriov_nads),
        "ovs_nad_count": len(ovs_nads),
        "sriov_resource_names": sorted(sriov_resource_names),
        "dpdk_resource_names": sorted(dpdk_resource_names),
        "worker_network_contract_validated": True,
        "target_metal3_ownership_verified": True,
        "target_ironic_inventory_verified": True,
    }
