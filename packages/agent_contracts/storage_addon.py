"""Rook-Ceph storage addon contract shared by the platform and Target Agent.

The addon owns three networks:

* ``internal``: Ceph cluster replication traffic (``cluster_network``);
* ``external``: Ceph public/client traffic (``public_network``);
* the worker control/data networks already validated by the Target Agent.

All four CIDRs must be distinct and non-overlapping. Control-plane traffic stays
on the ordinary control interface; CNF/application data traffic stays on the
data or SR-IOV network. Storage traffic never shares those subnets.

This module only validates configuration and renders Kubernetes documents. The
Target Agent owns the runner, apply order, readiness waits and lease checks.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import re
from typing import Any

_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
_INTERFACE_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")
_DEVICE_PATTERN = re.compile(r"^/dev/[A-Za-z0-9._/-]{1,120}$")
_CEPH_IMAGE_PATTERN = re.compile(r"^[A-Za-z0-9._:/-]+:[A-Za-z0-9._-]+$")
_ROOK_VERSION_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
_STORAGE_TYPES = frozenset({"rook-ceph"})


class StorageAddonError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _name(value: Any, code: str) -> str:
    if not isinstance(value, str) or len(value) > 63 or not _NAME_PATTERN.fullmatch(value):
        raise StorageAddonError(code)
    return value


def _cidr(value: Any, code: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    if not isinstance(value, str) or not value:
        raise StorageAddonError(code)
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError as exc:
        raise StorageAddonError(code) from exc
    if network.prefixlen < 8:
        raise StorageAddonError(code)
    return network


def _storage_network(
    value: Any,
    *,
    code_prefix: str,
    forbidden: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
    forbidden_interfaces: set[str],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StorageAddonError(f"{code_prefix}_INVALID")
    name = _name(value.get("name"), f"{code_prefix}_NAME_INVALID")
    interface = value.get("interface")
    if not isinstance(interface, str) or not _INTERFACE_PATTERN.fullmatch(interface):
        raise StorageAddonError(f"{code_prefix}_INTERFACE_INVALID")
    if interface in forbidden_interfaces:
        raise StorageAddonError(f"{code_prefix}_INTERFACE_COLLISION")
    cidr = _cidr(value.get("cidr"), f"{code_prefix}_CIDR_INVALID")
    for other in forbidden:
        if cidr.version == other.version and cidr.overlaps(other):
            raise StorageAddonError(f"{code_prefix}_CIDR_OVERLAP")
    mtu = value.get("mtu", 9000)
    if not isinstance(mtu, int) or isinstance(mtu, bool) or not 576 <= mtu <= 9216:
        raise StorageAddonError(f"{code_prefix}_MTU_INVALID")
    vlan = value.get("vlan")
    if vlan is not None and (
        not isinstance(vlan, int) or isinstance(vlan, bool) or not 1 <= vlan <= 4094
    ):
        raise StorageAddonError(f"{code_prefix}_VLAN_INVALID")
    return {
        "name": name,
        "interface": interface,
        "cidr": str(cidr),
        "mtu": mtu,
        "vlan": vlan,
        "ipam": value.get("ipam", "whereabouts"),
    }


def _storage_nodes(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise StorageAddonError("STORAGE_ADDON_NODES_REQUIRED")
    nodes: list[dict[str, Any]] = []
    names: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise StorageAddonError("STORAGE_ADDON_NODE_INVALID")
        name = _name(item.get("name"), "STORAGE_ADDON_NODE_NAME_INVALID")
        if name in names:
            raise StorageAddonError("STORAGE_ADDON_NODE_DUPLICATE")
        names.add(name)
        devices = item.get("devices")
        if not isinstance(devices, list) or not devices:
            raise StorageAddonError("STORAGE_ADDON_NODE_DEVICES_REQUIRED")
        normalized_devices = []
        for device in devices:
            if not isinstance(device, str) or not _DEVICE_PATTERN.fullmatch(device):
                raise StorageAddonError("STORAGE_ADDON_DEVICE_PATH_INVALID")
            if ".." in device:
                raise StorageAddonError("STORAGE_ADDON_DEVICE_PATH_INVALID")
            normalized_devices.append(device)
        normalized_devices = sorted(set(normalized_devices))
        nodes.append({"name": name, "devices": normalized_devices})
    return nodes


def plan_storage_addon(
    value: Any,
    *,
    worker_network_contract: Any = None,
) -> dict[str, Any]:
    """Validate and normalize a Rook-Ceph addon specification.

    ``enabled: false`` returns ``{"enabled": False}`` and every other field is
    ignored. Enabled plans fail closed on missing or ambiguous input.
    """

    if not isinstance(value, dict):
        raise StorageAddonError("STORAGE_ADDON_SPEC_INVALID")
    enabled = value.get("enabled")
    if not isinstance(enabled, bool):
        raise StorageAddonError("STORAGE_ADDON_ENABLED_REQUIRED")
    if not enabled:
        return {"enabled": False, "type": value.get("type", "rook-ceph")}
    addon_type = value.get("type", "rook-ceph")
    if addon_type not in _STORAGE_TYPES:
        raise StorageAddonError("STORAGE_ADDON_TYPE_UNSUPPORTED")

    contract_forbidden: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    contract_interfaces: set[str] = set()
    if worker_network_contract is not None:
        if not isinstance(worker_network_contract, dict):
            raise StorageAddonError("STORAGE_ADDON_WORKER_NETWORK_CONTRACT_INVALID")
        for cidr_field in ("control_plane_cidr", "data_plane_cidr"):
            contract_forbidden.append(
                _cidr(
                    worker_network_contract.get(cidr_field),
                    "STORAGE_ADDON_WORKER_NETWORK_CIDR_INVALID",
                )
            )
        for interface_field in ("control_plane_interface", "data_plane_interface"):
            interface = worker_network_contract.get(interface_field)
            if isinstance(interface, str) and interface:
                contract_interfaces.add(interface)

    network = value.get("network")
    if not isinstance(network, dict):
        raise StorageAddonError("STORAGE_ADDON_NETWORK_REQUIRED")
    internal = _storage_network(
        network.get("internal"),
        code_prefix="STORAGE_ADDON_INTERNAL",
        forbidden=contract_forbidden,
        forbidden_interfaces=contract_interfaces,
    )
    # The external network must also differ from the internal network.
    external = _storage_network(
        network.get("external"),
        code_prefix="STORAGE_ADDON_EXTERNAL",
        forbidden=[*contract_forbidden, ipaddress.ip_network(internal["cidr"])],
        forbidden_interfaces=contract_interfaces,
    )
    if internal["interface"] == external["interface"]:
        raise StorageAddonError("STORAGE_ADDON_INTERNAL_EXTERNAL_INTERFACE_COLLISION")
    if internal["name"] == external["name"]:
        raise StorageAddonError("STORAGE_ADDON_INTERNAL_EXTERNAL_NAME_COLLISION")

    namespace = _name(value.get("namespace", "rook-ceph"), "STORAGE_ADDON_NAMESPACE_INVALID")
    operator_namespace = _name(
        value.get("operator_namespace", "rook-ceph"),
        "STORAGE_ADDON_OPERATOR_NAMESPACE_INVALID",
    )
    if namespace != operator_namespace:
        raise StorageAddonError("STORAGE_ADDON_NAMESPACE_MISMATCH")
    cluster_name = _name(
        value.get("cluster_name", "rook-ceph"),
        "STORAGE_ADDON_CLUSTER_NAME_INVALID",
    )
    rook_version = value.get("rook_version")
    if not isinstance(rook_version, str) or not _ROOK_VERSION_PATTERN.fullmatch(rook_version):
        raise StorageAddonError("STORAGE_ADDON_ROOK_VERSION_INVALID")
    ceph_image = value.get("ceph_image")
    if not isinstance(ceph_image, str) or not _CEPH_IMAGE_PATTERN.fullmatch(ceph_image):
        raise StorageAddonError("STORAGE_ADDON_CEPH_IMAGE_INVALID")
    data_dir_host_path = value.get("data_dir_host_path", "/var/lib/rook")
    if (
        not isinstance(data_dir_host_path, str)
        or not data_dir_host_path.startswith("/")
        or ".." in data_dir_host_path
        or "\\" in data_dir_host_path
    ):
        raise StorageAddonError("STORAGE_ADDON_DATA_DIR_INVALID")

    storage = value.get("storage")
    if not isinstance(storage, dict):
        raise StorageAddonError("STORAGE_ADDON_STORAGE_REQUIRED")
    nodes = _storage_nodes(storage.get("nodes"))
    replication = storage.get("replication", 3)
    if (
        not isinstance(replication, int)
        or isinstance(replication, bool)
        or replication not in (1, 2, 3, 4, 5)
    ):
        raise StorageAddonError("STORAGE_ADDON_REPLICATION_INVALID")
    failure_domain = storage.get("failure_domain", "host")
    if failure_domain not in ("host", "rack", "zone"):
        raise StorageAddonError("STORAGE_ADDON_FAILURE_DOMAIN_INVALID")
    if len(nodes) < replication:
        raise StorageAddonError("STORAGE_ADDON_NODE_COUNT_BELOW_REPLICATION")

    mon_count = value.get("mon", {}).get("count", 3) if isinstance(value.get("mon"), dict) else 3
    mgr_count = value.get("mgr", {}).get("count", 2) if isinstance(value.get("mgr"), dict) else 2
    for field, count in (("MON", mon_count), ("MGR", mgr_count)):
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 9:
            raise StorageAddonError(f"STORAGE_ADDON_{field}_COUNT_INVALID")
    if mon_count > len(nodes):
        raise StorageAddonError("STORAGE_ADDON_MON_COUNT_EXCEEDS_NODES")
    if mon_count % 2 == 0:
        raise StorageAddonError("STORAGE_ADDON_MON_COUNT_MUST_BE_ODD")

    dashboard = value.get("dashboard", {})
    if not isinstance(dashboard, dict):
        raise StorageAddonError("STORAGE_ADDON_DASHBOARD_INVALID")
    dashboard_enabled = dashboard.get("enabled", True)
    dashboard_ssl = dashboard.get("ssl", True)
    if not isinstance(dashboard_enabled, bool) or not isinstance(dashboard_ssl, bool):
        raise StorageAddonError("STORAGE_ADDON_DASHBOARD_INVALID")

    storage_classes = value.get("storage_classes", {})
    if not isinstance(storage_classes, dict):
        raise StorageAddonError("STORAGE_ADDON_STORAGE_CLASSES_INVALID")
    block_enabled = storage_classes.get("block", True)
    filesystem_enabled = storage_classes.get("filesystem", False)
    if not isinstance(block_enabled, bool) or not isinstance(filesystem_enabled, bool):
        raise StorageAddonError("STORAGE_ADDON_STORAGE_CLASSES_INVALID")
    if not block_enabled and not filesystem_enabled:
        raise StorageAddonError("STORAGE_ADDON_STORAGE_CLASS_REQUIRED")
    block_pool = _name(
        storage_classes.get("block_pool", "rook-ceph-blockpool"),
        "STORAGE_ADDON_BLOCK_POOL_INVALID",
    )
    filesystem_name = _name(
        storage_classes.get("filesystem_name", "rook-ceph-filesystem"),
        "STORAGE_ADDON_FILESYSTEM_NAME_INVALID",
    )
    block_class = _name(
        storage_classes.get("block_class", "ceph-rbd"),
        "STORAGE_ADDON_BLOCK_CLASS_INVALID",
    )
    filesystem_class = _name(
        storage_classes.get("filesystem_class", "cephfs"),
        "STORAGE_ADDON_FILESYSTEM_CLASS_INVALID",
    )

    operator_install = value.get("operator_install", True)
    if not isinstance(operator_install, bool):
        raise StorageAddonError("STORAGE_ADDON_OPERATOR_INSTALL_INVALID")
    timeout_seconds = value.get("timeout_seconds", 3600)
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or not 300 <= timeout_seconds <= 7200
    ):
        raise StorageAddonError("STORAGE_ADDON_TIMEOUT_INVALID")

    return {
        "enabled": True,
        "type": "rook-ceph",
        "namespace": namespace,
        "operator_namespace": operator_namespace,
        "cluster_name": cluster_name,
        "rook_version": rook_version,
        "ceph_image": ceph_image,
        "data_dir_host_path": data_dir_host_path,
        "network": {"internal": internal, "external": external},
        "storage": {
            "nodes": nodes,
            "replication": replication,
            "failure_domain": failure_domain,
        },
        "mon": {"count": mon_count},
        "mgr": {"count": mgr_count},
        "dashboard": {"enabled": dashboard_enabled, "ssl": dashboard_ssl},
        "storage_classes": {
            "block": block_enabled,
            "filesystem": filesystem_enabled,
            "block_pool": block_pool,
            "block_class": block_class,
            "filesystem_name": filesystem_name,
            "filesystem_class": filesystem_class,
        },
        "operator_install": operator_install,
        "timeout_seconds": timeout_seconds,
    }


def _network_attachment(plan: dict[str, Any], role: str) -> dict[str, Any]:
    network = plan["network"][role]
    ipam = network["ipam"]
    if ipam not in ("whereabouts", "dhcp"):
        raise StorageAddonError("STORAGE_ADDON_IPAM_UNSUPPORTED")
    if ipam == "whereabouts":
        config = {
            "type": "whereabouts",
            "range": network["cidr"],
        }
    else:
        config = {"type": "dhcp"}
    plugin_config: dict[str, Any] = {
        "cniVersion": "0.3.1",
        "name": network["name"],
        "type": "macvlan",
        "master": network["interface"],
        "mode": "bridge",
        "mtu": network["mtu"],
        "ipam": config,
    }
    if network["vlan"] is not None:
        plugin_config["vlan"] = network["vlan"]
    return {
        "apiVersion": "k8s.io.cni.cncf.io/v1",
        "kind": "NetworkAttachmentDefinition",
        "metadata": {"name": network["name"], "namespace": plan["namespace"]},
        "spec": {"config": json.dumps(plugin_config, separators=(",", ":"))},
    }


def render_network_attachments(plan: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(plan, dict) or plan.get("enabled") is not True:
        raise StorageAddonError("STORAGE_ADDON_PLAN_INVALID")
    return [
        _network_attachment(plan, "internal"),
        _network_attachment(plan, "external"),
    ]


def render_ceph_cluster(plan: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, dict) or plan.get("enabled") is not True:
        raise StorageAddonError("STORAGE_ADDON_PLAN_INVALID")
    nodes = [
        {"name": node["name"], "devices": [{"name": device} for device in node["devices"]]}
        for node in plan["storage"]["nodes"]
    ]
    return {
        "apiVersion": "ceph.rook.io/v1",
        "kind": "CephCluster",
        "metadata": {"name": plan["cluster_name"], "namespace": plan["namespace"]},
        "spec": {
            "cephVersion": {
                "image": plan["ceph_image"],
                "allowUnsupported": False,
            },
            "dataDirHostPath": plan["data_dir_host_path"],
            "skipUpgradeChecks": False,
            "continueUpgradeAfterChecksEvenIfNotHealthy": False,
            "mon": {
                "count": plan["mon"]["count"],
                "allowMultiplePerNode": False,
            },
            "mgr": {
                "count": plan["mgr"]["count"],
                "modules": [{"name": "pg_autoscaler", "enabled": True}],
            },
            "dashboard": {
                "enabled": plan["dashboard"]["enabled"],
                "ssl": plan["dashboard"]["ssl"],
            },
            "network": {
                "provider": "multus",
                "selectors": {
                    "public": plan["network"]["external"]["name"],
                    "cluster": plan["network"]["internal"]["name"],
                },
                "addressRanges": {
                    "public": [plan["network"]["external"]["cidr"]],
                    "cluster": [plan["network"]["internal"]["cidr"]],
                },
            },
            "storage": {
                "useAllNodes": False,
                "useAllDevices": False,
                "nodes": nodes,
            },
            "disruptionManagement": {"managePodBudgets": True},
            "healthCheck": {"daemonHealth": {"mon": {"interval": "30s"}}},
        },
    }


def render_storage_resources(plan: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(plan, dict) or plan.get("enabled") is not True:
        raise StorageAddonError("STORAGE_ADDON_PLAN_INVALID")
    documents: list[dict[str, Any]] = []
    classes = plan["storage_classes"]
    if classes["block"]:
        documents.append(
            {
                "apiVersion": "ceph.rook.io/v1",
                "kind": "CephBlockPool",
                "metadata": {
                    "name": classes["block_pool"],
                    "namespace": plan["namespace"],
                },
                "spec": {
                    "failureDomain": plan["storage"]["failure_domain"],
                    "replicated": {
                        "size": plan["storage"]["replication"],
                        "requireSafeReplicaSize": True,
                    },
                },
            }
        )
        documents.append(
            {
                "apiVersion": "storage.k8s.io/v1",
                "kind": "StorageClass",
                "metadata": {"name": classes["block_class"]},
                "provisioner": "rook-ceph.rbd.csi.ceph.com",
                "parameters": {
                    "clusterID": plan["namespace"],
                    "pool": classes["block_pool"],
                    "imageFormat": "2",
                    "imageFeatures": "layering",
                    "csi.storage.k8s.io/provisioner-secret-name": "rook-csi-rbd-provisioner",
                    "csi.storage.k8s.io/provisioner-secret-namespace": plan["namespace"],
                    "csi.storage.k8s.io/controller-expand-secret-name": "rook-csi-rbd-provisioner",
                    "csi.storage.k8s.io/controller-expand-secret-namespace": plan["namespace"],
                    "csi.storage.k8s.io/node-stage-secret-name": "rook-csi-rbd-node",
                    "csi.storage.k8s.io/node-stage-secret-namespace": plan["namespace"],
                },
                "allowVolumeExpansion": True,
                "reclaimPolicy": "Delete",
                "volumeBindingMode": "Immediate",
            }
        )
    if classes["filesystem"]:
        documents.append(
            {
                "apiVersion": "ceph.rook.io/v1",
                "kind": "CephFilesystem",
                "metadata": {
                    "name": classes["filesystem_name"],
                    "namespace": plan["namespace"],
                },
                "spec": {
                    "metadataPool": {
                        "replicated": {
                            "size": plan["storage"]["replication"],
                            "requireSafeReplicaSize": True,
                        }
                    },
                    "dataPools": [
                        {
                            "failureDomain": plan["storage"]["failure_domain"],
                            "replicated": {
                                "size": plan["storage"]["replication"],
                                "requireSafeReplicaSize": True,
                            },
                        }
                    ],
                    "preserveFilesystemOnDelete": True,
                    "metadataServer": {
                        "activeCount": 1,
                        "activeStandby": True,
                    },
                },
            }
        )
        documents.append(
            {
                "apiVersion": "storage.k8s.io/v1",
                "kind": "StorageClass",
                "metadata": {"name": classes["filesystem_class"]},
                "provisioner": "rook-ceph.cephfs.csi.ceph.com",
                "parameters": {
                    "clusterID": plan["namespace"],
                    "fsName": classes["filesystem_name"],
                    "pool": f"{classes['filesystem_name']}-data0",
                    "csi.storage.k8s.io/provisioner-secret-name": "rook-csi-cephfs-provisioner",
                    "csi.storage.k8s.io/provisioner-secret-namespace": plan["namespace"],
                    "csi.storage.k8s.io/controller-expand-secret-name": (
                        "rook-csi-cephfs-provisioner"
                    ),
                    "csi.storage.k8s.io/controller-expand-secret-namespace": plan["namespace"],
                    "csi.storage.k8s.io/node-stage-secret-name": "rook-csi-cephfs-node",
                    "csi.storage.k8s.io/node-stage-secret-namespace": plan["namespace"],
                },
                "allowVolumeExpansion": True,
                "reclaimPolicy": "Delete",
                "volumeBindingMode": "Immediate",
            }
        )
    return documents


def validate_ceph_cluster_ready(document: Any) -> None:
    """Fail closed unless Rook reports a Ready cluster with a known health."""

    if not isinstance(document, dict):
        raise StorageAddonError("STORAGE_ADDON_CEPHCLUSTER_DOCUMENT_INVALID")
    status = document.get("status")
    if not isinstance(status, dict):
        raise StorageAddonError("STORAGE_ADDON_CEPHCLUSTER_STATUS_MISSING")
    if status.get("phase") != "Ready":
        raise StorageAddonError("STORAGE_ADDON_CEPHCLUSTER_NOT_READY")
    ceph = status.get("ceph")
    if ceph is not None:
        if not isinstance(ceph, dict):
            raise StorageAddonError("STORAGE_ADDON_CEPH_HEALTH_INVALID")
        health = ceph.get("health")
        if health not in ("HEALTH_OK", "HEALTH_WARN"):
            raise StorageAddonError("STORAGE_ADDON_CEPH_HEALTH_UNHEALTHY")


def clone_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(plan)
