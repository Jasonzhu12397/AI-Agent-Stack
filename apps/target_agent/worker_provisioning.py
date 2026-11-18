from __future__ import annotations

import copy
import ipaddress
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from .ntp_policy import (
    NtpPolicyError,
    render_worker_chrony_pre_kubeadm_command,
    validate_ironic_internal_ntp_ip,
)

MAX_WORKERS = 2500
FIELD_MANAGER = "ai-agent-target-agent"
MANAGED_BY_LABEL = "ai-agent-stack.io/managed-by"
DEPLOYMENT_ID_LABEL = "ai-agent-stack.io/deployment-id"
TARGET_CLUSTER_UID_ANNOTATION = "ai-agent-stack.io/target-cluster-uid"
CLUSTER_NAME_LABEL = "cluster.x-k8s.io/cluster-name"

RESOURCE_TYPES = {
    "MachineDeployment": {
        "group": "cluster.x-k8s.io",
        "plural": "machinedeployments",
        "crd": "machinedeployments.cluster.x-k8s.io",
    },
    "KubeadmConfigTemplate": {
        "group": "bootstrap.cluster.x-k8s.io",
        "plural": "kubeadmconfigtemplates",
        "crd": "kubeadmconfigtemplates.bootstrap.cluster.x-k8s.io",
    },
    "Metal3MachineTemplate": {
        "group": "infrastructure.cluster.x-k8s.io",
        "plural": "metal3machinetemplates",
        "crd": "metal3machinetemplates.infrastructure.cluster.x-k8s.io",
    },
    "Metal3DataTemplate": {
        "group": "infrastructure.cluster.x-k8s.io",
        "plural": "metal3datatemplates",
        "crd": "metal3datatemplates.infrastructure.cluster.x-k8s.io",
    },
}
RESOURCE_ORDER = (
    "KubeadmConfigTemplate",
    "Metal3DataTemplate",
    "Metal3MachineTemplate",
    "MachineDeployment",
)


class WorkerProvisioningError(ValueError):
    def __init__(self, message: str, side_effects: bool = False) -> None:
        super().__init__(message)
        self.side_effects = side_effects


def _name(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 63
        or not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", value)
    ):
        raise WorkerProvisioningError(f"WORKER_PROVISIONING_{field}_INVALID")
    return value


def _run(
    runner: Callable[..., dict[str, Any]],
    arguments: list[str],
    timeout: int = 60,
    max_output_bytes: int = 8 * 1024 * 1024,
) -> dict[str, Any]:
    result = runner(
        arguments,
        timeout=timeout,
        max_output_bytes=max_output_bytes,
    )
    if not isinstance(result, dict) or result.get("available") is not True:
        raise WorkerProvisioningError("WORKER_PROVISIONING_KUBERNETES_API_FAILED")
    return result


def _not_found(result: dict[str, Any]) -> bool:
    error = result.get("stderr", "").lower()
    return "notfound" in error or "not found" in error


def _load_manifests(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise WorkerProvisioningError("WORKER_PROVISIONING_RESOURCE_PLAN_REQUIRED")
    documents = []
    for item in value:
        if not isinstance(item, dict):
            raise WorkerProvisioningError("WORKER_PROVISIONING_RESOURCE_INVALID")
        documents.append(copy.deepcopy(item))
    return documents


def _crd_version_is_served(
    runner: Callable[..., dict[str, Any]],
    kind: str,
    api_version: str,
) -> None:
    info = RESOURCE_TYPES[kind]
    result = _run(runner, ["get", "crd", info["crd"], "-o", "json"])
    try:
        document = json.loads(result.get("stdout", ""))
    except (TypeError, ValueError) as exc:
        raise WorkerProvisioningError("WORKER_PROVISIONING_CRD_JSON_INVALID") from exc
    spec = document.get("spec", {})
    versions = spec.get("versions", [])
    group, version = api_version.split("/", 1)
    if group != info["group"]:
        raise WorkerProvisioningError("WORKER_PROVISIONING_API_GROUP_MISMATCH")
    served = [
        item for item in versions
        if isinstance(item, dict) and item.get("name") == version and item.get("served") is True
    ]
    if not served:
        raise WorkerProvisioningError(f"WORKER_PROVISIONING_CRD_VERSION_NOT_SERVED:{kind}")
    schema = served[0].get("schema", {}).get("openAPIV3Schema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise WorkerProvisioningError(f"WORKER_PROVISIONING_CRD_SCHEMA_MISSING:{kind}")


def _cluster_uid(
    runner: Callable[..., dict[str, Any]],
    namespace: str,
    cluster_name: str,
    proof: dict[str, Any],
) -> tuple[str, str]:
    result = _run(
        runner,
        [
            "get",
            "clusters.cluster.x-k8s.io",
            cluster_name,
            "--namespace",
            namespace,
            "-o",
            "json",
        ],
    )
    try:
        cluster = json.loads(result.get("stdout", ""))
    except (TypeError, ValueError) as exc:
        raise WorkerProvisioningError(
            "WORKER_PROVISIONING_TARGET_CLUSTER_JSON_INVALID"
        ) from exc
    metadata = cluster.get("metadata", {})
    uid = metadata.get("uid")
    annotations = metadata.get("annotations", {})
    if uid != proof.get("target_cluster_uid"):
        raise WorkerProvisioningError("WORKER_PROVISIONING_TARGET_CLUSTER_UID_MISMATCH")
    if annotations.get("ai-agent-stack.io/source-cluster-uid") != proof.get("source_cluster_uid"):
        raise WorkerProvisioningError("WORKER_PROVISIONING_SOURCE_CLUSTER_ANNOTATION_MISMATCH")
    control_plane_ref = cluster.get("spec", {}).get("controlPlaneRef", {})
    if (
        control_plane_ref.get("apiGroup") != "controlplane.cluster.x-k8s.io"
        or control_plane_ref.get("kind") != "KubeadmControlPlane"
    ):
        raise WorkerProvisioningError("WORKER_PROVISIONING_KUBEADM_CONTROL_PLANE_REF_REQUIRED")
    control_plane_name = _name(control_plane_ref.get("name"), "CONTROL_PLANE_NAME")
    control_plane_result = _run(
        runner,
        [
            "get",
            "kubeadmcontrolplanes.controlplane.cluster.x-k8s.io",
            control_plane_name,
            "--namespace",
            namespace,
            "-o",
            "json",
        ],
    )
    try:
        control_plane = json.loads(control_plane_result.get("stdout", ""))
    except (TypeError, ValueError) as exc:
        raise WorkerProvisioningError(
            "WORKER_PROVISIONING_CONTROL_PLANE_JSON_INVALID"
        ) from exc
    version = control_plane.get("spec", {}).get("version")
    if not isinstance(version, str) or not version:
        raise WorkerProvisioningError("WORKER_PROVISIONING_CONTROL_PLANE_VERSION_REQUIRED")
    return uid, version


def _network_contract(
    contract: Any,
    data_template: dict[str, Any],
) -> None:
    if not isinstance(contract, dict):
        raise WorkerProvisioningError("WORKER_PROVISIONING_NETWORK_CONTRACT_REQUIRED")
    control_interface = contract.get("control_plane_interface")
    data_interface = contract.get("data_plane_interface")
    if (
        not isinstance(control_interface, str)
        or not control_interface
        or not isinstance(data_interface, str)
        or not data_interface
        or control_interface == data_interface
    ):
        raise WorkerProvisioningError("WORKER_PROVISIONING_NETWORK_INTERFACES_INVALID")
    try:
        control_network = ipaddress.ip_network(
            contract.get("control_plane_cidr"),
            strict=False,
        )
        data_network = ipaddress.ip_network(
            contract.get("data_plane_cidr"),
            strict=False,
        )
    except (TypeError, ValueError) as exc:
        raise WorkerProvisioningError("WORKER_PROVISIONING_NETWORK_CIDR_INVALID") from exc
    if control_network.version != data_network.version or control_network.overlaps(data_network):
        raise WorkerProvisioningError("WORKER_PROVISIONING_CONTROL_DATA_NETWORK_OVERLAP")
    mtu = contract.get("data_plane_mtu")
    if not isinstance(mtu, int) or isinstance(mtu, bool) or not 576 <= mtu <= 9216:
        raise WorkerProvisioningError("WORKER_PROVISIONING_DATA_PLANE_MTU_INVALID")
    required_features = (
        "calico_enabled",
        "multus_enabled",
        "ovs_enabled",
        "sriov_cni_enabled",
        "sriov_dpdk_enabled",
    )
    if any(contract.get(feature) is not True for feature in required_features):
        raise WorkerProvisioningError("WORKER_PROVISIONING_DATA_PLANE_FEATURES_INCOMPLETE")
    if contract.get("pod_traffic_network") != "data":
        raise WorkerProvisioningError("WORKER_PROVISIONING_POD_TRAFFIC_MUST_USE_DATA_PLANE")

    network_data = data_template.get("spec", {}).get("networkData")
    if not isinstance(network_data, dict):
        raise WorkerProvisioningError("WORKER_PROVISIONING_METAL3_NETWORK_DATA_REQUIRED")
    links = network_data.get("links", {})
    ethernets = links.get("ethernets", []) if isinstance(links, dict) else []
    if isinstance(ethernets, dict):
        interface_ids = set(ethernets)
    elif isinstance(ethernets, list):
        interface_ids = {
            item.get("id")
            for item in ethernets
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
    else:
        interface_ids = set()
    if not {control_interface, data_interface}.issubset(interface_ids):
        raise WorkerProvisioningError("WORKER_PROVISIONING_NETWORK_INTERFACE_NOT_IN_DATA_TEMPLATE")
    networks = network_data.get("networks")
    if not isinstance(networks, dict) or not networks:
        raise WorkerProvisioningError("WORKER_PROVISIONING_NETWORK_SUBNETS_REQUIRED")
    interface_bindings: dict[str, tuple[str, dict[str, Any]]] = {}
    for network_name, entries in networks.items():
        if not isinstance(network_name, str) or not isinstance(entries, list):
            raise WorkerProvisioningError("WORKER_PROVISIONING_NETWORK_SUBNETS_INVALID")
        for entry in entries:
            if not isinstance(entry, dict):
                raise WorkerProvisioningError("WORKER_PROVISIONING_NETWORK_ENTRY_INVALID")
            link = entry.get("link")
            if not isinstance(link, str) or not link:
                raise WorkerProvisioningError("WORKER_PROVISIONING_NETWORK_LINK_INVALID")
            if link in interface_bindings:
                raise WorkerProvisioningError(
                    "WORKER_PROVISIONING_NETWORK_INTERFACE_MAPPED_MULTIPLE_TIMES"
                )
            interface_bindings[link] = (network_name, entry)
    control_binding = interface_bindings.get(control_interface)
    data_binding = interface_bindings.get(data_interface)
    if control_binding is None or data_binding is None:
        raise WorkerProvisioningError("WORKER_PROVISIONING_NETWORK_LINK_MAPPING_INCOMPLETE")
    if control_binding[0] == data_binding[0]:
        raise WorkerProvisioningError("WORKER_PROVISIONING_CONTROL_DATA_NETWORK_NOT_SEPARATED")
    if not isinstance(data_binding[1].get("ipAddressFromIPPool"), str):
        raise WorkerProvisioningError("WORKER_PROVISIONING_DATA_INTERFACE_IP_POOL_REQUIRED")


def _ip_pool_references(value: Any) -> set[str]:
    references = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"ipAddressFromIPPool", "gatewayFromIPPool"}:
                if isinstance(item, str) and item:
                    references.add(item)
            references.update(_ip_pool_references(item))
    elif isinstance(value, list):
        for item in value:
            references.update(_ip_pool_references(item))
    return references


def _verify_ip_pools(
    runner: Callable[..., dict[str, Any]],
    namespace: str,
    cluster_name: str,
    data_template: dict[str, Any],
) -> list[str]:
    references = sorted(
        _ip_pool_references(data_template.get("spec", {}).get("networkData", {}))
    )
    if not references:
        return []
    result = _run(
        runner,
        [
            "get",
            "ippools.ipam.metal3.io",
            "--namespace",
            namespace,
            "-o",
            "json",
        ],
    )
    try:
        document = json.loads(result.get("stdout", ""))
    except (TypeError, ValueError) as exc:
        raise WorkerProvisioningError("WORKER_PROVISIONING_IP_POOL_INVENTORY_INVALID") from exc
    items = document.get("items")
    if not isinstance(items, list):
        raise WorkerProvisioningError("WORKER_PROVISIONING_IP_POOL_INVENTORY_INVALID")
    pools = {
        item.get("metadata", {}).get("name"): item
        for item in items
        if isinstance(item, dict)
    }
    for name in references:
        pool = pools.get(name)
        if not pool:
            raise WorkerProvisioningError(f"WORKER_PROVISIONING_IP_POOL_NOT_FOUND:{name}")
        if pool.get("spec", {}).get("clusterName") != cluster_name:
            raise WorkerProvisioningError(
                f"WORKER_PROVISIONING_IP_POOL_CLUSTER_MISMATCH:{name}"
            )
    return references


def _ensure_worker_ntp(
    kubeadm_template: dict[str, Any],
    ironic_internal_ntp_ip: str,
) -> None:
    spec = kubeadm_template.get("spec", {}).get("template", {}).get("spec")
    if not isinstance(spec, dict):
        raise WorkerProvisioningError("WORKER_PROVISIONING_KUBEADM_TEMPLATE_SPEC_INVALID")
    commands = spec.setdefault("preKubeadmCommands", [])
    if not isinstance(commands, list) or not all(isinstance(item, str) for item in commands):
        raise WorkerProvisioningError("WORKER_PROVISIONING_PRE_KUBEADM_COMMANDS_INVALID")
    ntp_commands = [
        command for command in commands
        if "chronyc -n sources" in command or "chronyc tracking" in command
    ]
    if ntp_commands:
        script = "\n".join(ntp_commands)
        sources = re.findall(r"(?m)^\s*server\s+(\S+)", script)
        required = (
            "chronyc -n sources",
            "chronyc tracking",
            "Leap status",
            "systemctl restart",
            "$2 == server",
            "selected = 1",
        )
        if (
            sources != [ironic_internal_ntp_ip]
            or re.search(r"(?m)^\s*pool\s+", script)
            or not all(marker in script for marker in required)
        ):
            raise WorkerProvisioningError("WORKER_PROVISIONING_WORKER_NTP_SCRIPT_INVALID")
        return
    try:
        commands.append(render_worker_chrony_pre_kubeadm_command(ironic_internal_ntp_ip))
    except NtpPolicyError as exc:
        raise WorkerProvisioningError("WORKER_PROVISIONING_WORKER_NTP_IP_INVALID") from exc


def _validate_references(
    documents: list[dict[str, Any]],
    cluster_name: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    by_kind: dict[str, dict[str, Any]] = {}
    for document in documents:
        kind = document.get("kind")
        if kind not in RESOURCE_TYPES or kind in by_kind:
            raise WorkerProvisioningError(
                "WORKER_PROVISIONING_RESOURCE_KIND_UNSUPPORTED_OR_DUPLICATE"
            )
        by_kind[kind] = document
    required = (
        "MachineDeployment",
        "KubeadmConfigTemplate",
        "Metal3MachineTemplate",
        "Metal3DataTemplate",
    )
    if any(kind not in by_kind for kind in required):
        raise WorkerProvisioningError("WORKER_PROVISIONING_REQUIRED_RESOURCE_MISSING")

    machine_deployment = by_kind["MachineDeployment"]
    md_spec = machine_deployment.get("spec", {})
    if md_spec.get("clusterName") != cluster_name:
        raise WorkerProvisioningError("WORKER_PROVISIONING_MACHINE_DEPLOYMENT_CLUSTER_MISMATCH")
    template_spec = md_spec.get("template", {}).get("spec", {})
    bootstrap_ref = template_spec.get("bootstrap", {}).get("configRef", {})
    infrastructure_ref = template_spec.get("infrastructureRef", {})
    kubeadm_template = by_kind["KubeadmConfigTemplate"]
    metal3_template = by_kind["Metal3MachineTemplate"]
    data_template = by_kind["Metal3DataTemplate"]

    if (
        bootstrap_ref.get("kind") != "KubeadmConfigTemplate"
        or bootstrap_ref.get("apiGroup") != RESOURCE_TYPES["KubeadmConfigTemplate"]["group"]
        or bootstrap_ref.get("name") != kubeadm_template.get("metadata", {}).get("name")
    ):
        raise WorkerProvisioningError("WORKER_PROVISIONING_BOOTSTRAP_TEMPLATE_REFERENCE_INVALID")
    if (
        infrastructure_ref.get("kind") != "Metal3MachineTemplate"
        or infrastructure_ref.get("apiGroup") != RESOURCE_TYPES["Metal3MachineTemplate"]["group"]
        or infrastructure_ref.get("name") != metal3_template.get("metadata", {}).get("name")
    ):
        raise WorkerProvisioningError(
            "WORKER_PROVISIONING_INFRASTRUCTURE_TEMPLATE_REFERENCE_INVALID"
        )

    data_template_name = (
        metal3_template.get("spec", {}).get("template", {}).get("spec", {})
        .get("dataTemplate", {}).get("name")
    )
    if data_template_name != data_template.get("metadata", {}).get("name"):
        raise WorkerProvisioningError("WORKER_PROVISIONING_DATA_TEMPLATE_REFERENCE_INVALID")
    if data_template.get("spec", {}).get("clusterName") != cluster_name:
        raise WorkerProvisioningError("WORKER_PROVISIONING_DATA_TEMPLATE_CLUSTER_MISMATCH")
    return machine_deployment, kubeadm_template, metal3_template, data_template


def _prepare_manifests(
    documents: list[dict[str, Any]],
    namespace: str,
    cluster_name: str,
    deployment_id: str,
    target_cluster_uid: str,
    ironic_internal_ntp_ip: str,
    network_contract: dict[str, Any],
) -> list[dict[str, Any]]:
    ordered = []
    by_kind = {item["kind"]: item for item in documents}
    for kind in RESOURCE_ORDER:
        document = by_kind[kind]
        api_version = document.get("apiVersion")
        if not isinstance(api_version, str) or "/" not in api_version:
            raise WorkerProvisioningError("WORKER_PROVISIONING_API_VERSION_INVALID")
        metadata = document.get("metadata")
        if not isinstance(metadata, dict):
            raise WorkerProvisioningError("WORKER_PROVISIONING_METADATA_REQUIRED")
        if metadata.get("namespace") != namespace:
            raise WorkerProvisioningError("WORKER_PROVISIONING_NAMESPACE_MISMATCH")
        _name(metadata.get("name"), "RESOURCE_NAME")
        labels = metadata.setdefault("labels", {})
        annotations = metadata.setdefault("annotations", {})
        if not isinstance(labels, dict) or not isinstance(annotations, dict):
            raise WorkerProvisioningError("WORKER_PROVISIONING_METADATA_LABELS_INVALID")
        if labels.get(MANAGED_BY_LABEL) not in (None, "target-agent"):
            raise WorkerProvisioningError("WORKER_PROVISIONING_RESOURCE_OWNER_CONFLICT")
        if labels.get(DEPLOYMENT_ID_LABEL) not in (None, deployment_id):
            raise WorkerProvisioningError("WORKER_PROVISIONING_DEPLOYMENT_OWNER_CONFLICT")
        if annotations.get(TARGET_CLUSTER_UID_ANNOTATION) not in (None, target_cluster_uid):
            raise WorkerProvisioningError("WORKER_PROVISIONING_TARGET_UID_OWNER_CONFLICT")
        labels[MANAGED_BY_LABEL] = "target-agent"
        labels[DEPLOYMENT_ID_LABEL] = deployment_id
        labels[CLUSTER_NAME_LABEL] = cluster_name
        annotations[TARGET_CLUSTER_UID_ANNOTATION] = target_cluster_uid
        ordered.append(document)

    md = by_kind["MachineDeployment"]
    md_spec = md.get("spec", {})
    replicas = md_spec.get("replicas")
    if (
        not isinstance(replicas, int)
        or isinstance(replicas, bool)
        or not 1 <= replicas <= MAX_WORKERS
    ):
        raise WorkerProvisioningError("WORKER_PROVISIONING_REPLICA_COUNT_INVALID")
    selector = md_spec.get("selector", {}).get("matchLabels")
    template_metadata = md_spec.get("template", {}).get("metadata", {})
    template_labels = template_metadata.get("labels")
    if not isinstance(selector, dict) or not selector or not isinstance(template_labels, dict):
        raise WorkerProvisioningError("WORKER_PROVISIONING_MACHINE_SELECTOR_INVALID")
    if selector.get(CLUSTER_NAME_LABEL) != cluster_name:
        raise WorkerProvisioningError("WORKER_PROVISIONING_MACHINE_SELECTOR_CLUSTER_LABEL_REQUIRED")
    if any(template_labels.get(key) != value for key, value in selector.items()):
        raise WorkerProvisioningError("WORKER_PROVISIONING_MACHINE_SELECTOR_TEMPLATE_MISMATCH")
    if template_labels.get(CLUSTER_NAME_LABEL) != cluster_name:
        raise WorkerProvisioningError("WORKER_PROVISIONING_MACHINE_TEMPLATE_CLUSTER_LABEL_REQUIRED")
    _ensure_worker_ntp(by_kind["KubeadmConfigTemplate"], ironic_internal_ntp_ip)
    _network_contract(network_contract, by_kind["Metal3DataTemplate"])
    return ordered


def _verify_existing_ownership(
    runner: Callable[..., dict[str, Any]],
    documents: list[dict[str, Any]],
    namespace: str,
    deployment_id: str,
    target_cluster_uid: str,
) -> dict[str, dict[str, Any]]:
    existing_resources = {}
    for document in documents:
        kind = document["kind"]
        info = RESOURCE_TYPES[kind]
        name = document["metadata"]["name"]
        result = runner(
            [
                "get",
                info["plural"],
                name,
                "--namespace",
                namespace,
                "-o",
                "json",
            ],
            timeout=30,
            max_output_bytes=2 * 1024 * 1024,
        )
        if result.get("available") is not True:
            if _not_found(result):
                continue
            raise WorkerProvisioningError("WORKER_PROVISIONING_EXISTING_RESOURCE_LOOKUP_FAILED")
        try:
            existing = json.loads(result.get("stdout", ""))
        except (TypeError, ValueError) as exc:
            raise WorkerProvisioningError(
                "WORKER_PROVISIONING_EXISTING_RESOURCE_JSON_INVALID"
            ) from exc
        metadata = existing.get("metadata", {})
        labels = metadata.get("labels", {})
        annotations = metadata.get("annotations", {})
        if (
            labels.get(MANAGED_BY_LABEL) != "target-agent"
            or labels.get(DEPLOYMENT_ID_LABEL) != deployment_id
            or annotations.get(TARGET_CLUSTER_UID_ANNOTATION) != target_cluster_uid
        ):
            raise WorkerProvisioningError("WORKER_PROVISIONING_EXISTING_RESOURCE_NOT_OWNED")
        existing_resources[kind] = existing
    return existing_resources


def _available_bmh_count(
    runner: Callable[..., dict[str, Any]],
    namespace: str,
) -> int:
    result = _run(
        runner,
        [
            "get",
            "baremetalhosts.metal3.io",
            "--namespace",
            namespace,
            "-o",
            "json",
        ],
    )
    try:
        document = json.loads(result.get("stdout", ""))
    except (TypeError, ValueError) as exc:
        raise WorkerProvisioningError("WORKER_PROVISIONING_BMH_INVENTORY_INVALID") from exc
    hosts = document.get("items")
    if not isinstance(hosts, list):
        raise WorkerProvisioningError("WORKER_PROVISIONING_BMH_INVENTORY_INVALID")
    available = 0
    for host in hosts:
        spec = host.get("spec", {})
        status = host.get("status", {})
        provisioning = status.get("provisioning", {})
        if (
            spec.get("online") is not False
            and spec.get("consumerRef") is None
            and provisioning.get("state") in ("ready", "available")
            and status.get("operationalStatus") == "OK"
            and not status.get("errorMessage")
        ):
            available += 1
    return available


def execute_worker_provisioning(
    payload: dict[str, Any],
    deployment_context: dict[str, Any] | None,
    runner: Callable[..., dict[str, Any]],
    lease_lost_event: Any = None,
    environ: Any = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(deployment_context, dict):
        raise WorkerProvisioningError("WORKER_PROVISIONING_TRUSTED_CONTEXT_REQUIRED")
    proof = deployment_context.get("eph_pivot_attestation")
    if not isinstance(proof, dict):
        raise WorkerProvisioningError("WORKER_PROVISIONING_PIVOT_ATTESTATION_REQUIRED")
    namespace = _name(payload.get("target_namespace"), "NAMESPACE")
    cluster_name = _name(payload.get("target_cluster_name"), "CLUSTER_NAME")
    deployment_id = _name(payload.get("deployment_id"), "DEPLOYMENT_ID")
    try:
        ironic_ntp_ip = validate_ironic_internal_ntp_ip(
            os.environ.get("IRONIC_INTERNAL_NTP_IP", "")
            if environ is None
            else environ.get("IRONIC_INTERNAL_NTP_IP", "")
        )
    except (NtpPolicyError, TypeError, ValueError) as exc:
        raise WorkerProvisioningError(
            "WORKER_PROVISIONING_IRONIC_INTERNAL_NTP_IP_REQUIRED"
        ) from exc

    target_uid, control_plane_version = _cluster_uid(
        runner,
        namespace,
        cluster_name,
        proof,
    )
    documents = _load_manifests(payload.get("worker_resources"))
    machine_deployment, _kubeadm, _metal3, _data = _validate_references(
        documents,
        cluster_name,
    )
    worker_version = (
        machine_deployment.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("version")
    )
    if worker_version != control_plane_version:
        raise WorkerProvisioningError("WORKER_PROVISIONING_KUBERNETES_VERSION_MISMATCH")
    ordered = _prepare_manifests(
        documents,
        namespace,
        cluster_name,
        deployment_id,
        target_uid,
        ironic_ntp_ip,
        payload.get("worker_network_contract"),
    )
    for document in ordered:
        _crd_version_is_served(runner, document["kind"], document["apiVersion"])
    ip_pool_references = _verify_ip_pools(
        runner,
        namespace,
        cluster_name,
        _data,
    )
    existing_resources = _verify_existing_ownership(
        runner,
        ordered,
        namespace,
        deployment_id,
        target_uid,
    )
    md_name = machine_deployment["metadata"]["name"]
    desired_replicas = machine_deployment["spec"]["replicas"]
    existing_machine_deployment = existing_resources.get("MachineDeployment")
    existing_replicas = (
        existing_machine_deployment.get("spec", {}).get("replicas", 0)
        if existing_machine_deployment
        else 0
    )
    if desired_replicas < existing_replicas:
        raise WorkerProvisioningError("WORKER_PROVISIONING_SCALE_DOWN_REQUIRES_DRAIN_EXECUTOR")
    additional_workers = desired_replicas - existing_replicas
    available_bmh_count = (
        _available_bmh_count(runner, namespace) if additional_workers else 0
    )
    if available_bmh_count < additional_workers:
        raise WorkerProvisioningError("WORKER_PROVISIONING_AVAILABLE_BMH_CAPACITY_INSUFFICIENT")

    applied_output = []
    with tempfile.TemporaryDirectory(prefix="target-worker-plan-", dir="/tmp") as workdir:
        manifest_path = Path(workdir) / "worker-plan.yaml"
        manifest_path.write_text(
            yaml.safe_dump_all(ordered, sort_keys=False),
            encoding="utf-8",
        )
        os.chmod(manifest_path, 0o600)
        dry_run = _run(
            runner,
            [
                "apply",
                "--server-side",
                f"--field-manager={FIELD_MANAGER}",
                "--dry-run=server",
                "-f",
                str(manifest_path),
            ],
            timeout=180,
        )
        if lease_lost_event is not None and lease_lost_event.is_set():
            raise WorkerProvisioningError("WORKER_PROVISIONING_COMMAND_LEASE_LOST")
        for index, document in enumerate(ordered):
            if lease_lost_event is not None and lease_lost_event.is_set():
                raise WorkerProvisioningError(
                    "WORKER_PROVISIONING_COMMAND_LEASE_LOST",
                    side_effects=bool(applied_output),
                )
            resource_path = Path(workdir) / f"resource-{index}.yaml"
            resource_path.write_text(
                yaml.safe_dump(document, sort_keys=False),
                encoding="utf-8",
            )
            os.chmod(resource_path, 0o600)
            try:
                applied = _run(
                    runner,
                    [
                        "apply",
                        "--server-side",
                        f"--field-manager={FIELD_MANAGER}",
                        "-f",
                        str(resource_path),
                    ],
                    timeout=180,
                )
            except WorkerProvisioningError as exc:
                exc.side_effects = True
                raise
            applied_output.append(applied.get("stdout", ""))
        if lease_lost_event is not None and lease_lost_event.is_set():
            raise WorkerProvisioningError(
                "WORKER_PROVISIONING_COMMAND_LEASE_LOST",
                side_effects=bool(applied_output),
            )

    md_name = machine_deployment["metadata"]["name"]
    observed = None
    for _attempt in range(20):
        if lease_lost_event is not None and lease_lost_event.is_set():
            raise WorkerProvisioningError(
                "WORKER_PROVISIONING_COMMAND_LEASE_LOST",
                side_effects=bool(applied_output),
            )
        try:
            md_result = _run(
                runner,
                [
                    "get",
                    "machinedeployments",
                    md_name,
                    "--namespace",
                    namespace,
                    "-o",
                    "json",
                ],
                timeout=60,
                max_output_bytes=4 * 1024 * 1024,
            )
        except WorkerProvisioningError as exc:
            exc.side_effects = bool(applied_output)
            raise
        try:
            observed = json.loads(md_result.get("stdout", ""))
        except (TypeError, ValueError) as exc:
            raise WorkerProvisioningError(
                "WORKER_PROVISIONING_MACHINE_DEPLOYMENT_JSON_INVALID",
                side_effects=bool(applied_output),
            ) from exc
        observed_metadata = observed.get("metadata", {})
        generation = observed_metadata.get("generation", 0)
        status = observed.get("status", {})
        if status.get("observedGeneration", 0) >= generation:
            break
        time.sleep(3)
    else:
        raise WorkerProvisioningError(
            "WORKER_PROVISIONING_CONTROLLER_NOT_OBSERVED",
            side_effects=bool(applied_output),
        )
    observed_spec = observed.get("spec", {})
    desired = observed_spec.get("replicas")
    if not isinstance(desired, int) or desired < 1:
        raise WorkerProvisioningError(
            "WORKER_PROVISIONING_MACHINE_DEPLOYMENT_SPEC_INVALID",
            side_effects=bool(applied_output),
        )
    return {
        "worker_provisioning_resources_applied": True,
        "worker_provisioning_server_dry_run_passed": True,
        "worker_provisioning_controller_observed": True,
        "machine_deployment": md_name,
        "available_bmh_count_before_provisioning": available_bmh_count,
        "new_worker_capacity_verified": available_bmh_count >= additional_workers,
        "machine_deployment_namespace": namespace,
        "desired_worker_count": desired,
        "worker_template_resources": [
            {
                "kind": item["kind"],
                "name": item["metadata"]["name"],
                "api_version": item["apiVersion"],
            }
            for item in ordered
        ],
        "ironic_internal_ntp_ip": ironic_ntp_ip,
        "worker_network_contract_validated": True,
        "worker_ip_pools_verified": ip_pool_references,
        "target_cluster_uid": target_uid,
        "deployment_id": deployment_id,
        "target_cluster_name": cluster_name,
        "target_namespace": namespace,
        "worker_network_contract": copy.deepcopy(payload["worker_network_contract"]),
        "applied_stdout": "\n".join(applied_output)[-4000:],
        "dry_run_stdout": dry_run.get("stdout", "")[-4000:],
        "side_effects": True,
    }
