# ruff: noqa: I001
from types import SimpleNamespace

from core.cluster_deployment_views import _validated_spec


def _spec() -> dict:
    return {
        "cluster_name": "edge-lab",
        "kubernetes_version": "v1.32.2",
        "control_plane_replicas": 3,
        "worker_replicas": 3,
        "worker_provisioning": {
            "worker_network_contract": {
                "control_plane_interface": "bond0",
                "control_plane_cidr": "10.0.60.0/24",
                "data_plane_interface": "bond1",
                "data_plane_cidr": "10.0.61.0/24",
            }
        },
        "storage_addon": {
            "enabled": True,
            "type": "rook-ceph",
            "rook_version": "v1.17.4",
            "ceph_image": "quay.io/ceph/ceph:v19.2.2",
            "network": {
                "internal": {
                    "name": "ceph-cluster-net",
                    "interface": "eno5",
                    "cidr": "192.168.60.0/24",
                },
                "external": {
                    "name": "ceph-public-net",
                    "interface": "eno6",
                    "cidr": "192.168.61.0/24",
                },
            },
            "storage": {
                "nodes": [
                    {"name": "worker-0", "devices": ["/dev/nvme0n1"]},
                    {"name": "worker-1", "devices": ["/dev/nvme0n1"]},
                    {"name": "worker-2", "devices": ["/dev/nvme0n1"]},
                ],
                "replication": 3,
            },
        },
    }


def test_cluster_spec_accepts_valid_storage_addon() -> None:
    request = SimpleNamespace(data={"cluster_spec": _spec()})
    spec, error = _validated_spec(request)
    assert error is None
    assert spec["storage_addon"]["enabled"] is True
    assert spec["storage_addon"]["network"]["internal"]["cidr"] == "192.168.60.0/24"


def test_cluster_spec_rejects_storage_network_overlap() -> None:
    spec_value = _spec()
    spec_value["storage_addon"]["network"]["internal"]["cidr"] = "10.0.60.0/25"
    request = SimpleNamespace(data={"cluster_spec": spec_value})
    spec, error = _validated_spec(request)
    assert spec is None
    assert error == "storage_addon: STORAGE_ADDON_INTERNAL_CIDR_OVERLAP"


def _infra_profile() -> dict:
    return {
        "networks": [
            {
                "name": "ccd-control",
                "role": "control",
                "vlan": 3220,
                "mtu": 1500,
                "cidr": "10.0.60.0/24",
                "gateway_ipv4": "10.0.60.1",
                "ip_pools": [
                    {"start": "10.0.60.10", "end": "10.0.60.250", "prefix": 24}
                ],
                "bond": {
                    "name": "bond_control",
                    "interfaces": ["eno1", "eno2"],
                    "mode": "active-backup",
                },
                "host_roles": ["control_plane", "worker"],
            },
            {
                "name": "ccd-data",
                "role": "data",
                "vlan": 3300,
                "mtu": 9000,
                "cidr": "10.0.61.0/24",
                "gateway_ipv4": "10.0.61.1",
                "ip_pools": [
                    {"start": "10.0.61.10", "end": "10.0.61.250", "prefix": 24}
                ],
                "bond": {
                    "name": "bond_data",
                    "interfaces": ["ens1f0", "ens1f1"],
                    "mode": "802.3ad",
                },
                "host_roles": ["worker"],
            },
            {
                "name": "ceph-cluster-net",
                "role": "storage-internal",
                "vlan": 3608,
                "mtu": 9000,
                "cidr": "10.0.70.0/26",
                "gateway_ipv4": "10.0.70.1",
                "ip_pools": [
                    {"start": "10.0.70.2", "end": "10.0.70.62", "prefix": 26}
                ],
                "interface": "bond_storage",
                "host_roles": ["worker"],
            },
            {
                "name": "ceph-public-net",
                "role": "storage-external",
                "vlan": 3609,
                "mtu": 9000,
                "cidr": "10.0.69.0/24",
                "gateway_ipv4": "10.0.69.1",
                "ip_pools": [
                    {"start": "10.0.69.71", "end": "10.0.69.232", "prefix": 24}
                ],
                "interface": "bond_storage",
                "host_roles": ["storage"],
            },
        ]
    }


def test_cluster_spec_generates_role_cloud_init_and_ip_pools() -> None:
    spec_value = _spec()
    spec_value["infra_network_profile"] = _infra_profile()
    request = SimpleNamespace(data={"cluster_spec": spec_value})
    spec, error = _validated_spec(request)
    assert error is None
    assert spec["network_spec"]["control_plane_interface"] == "bond_control"
    assert spec["network_spec"]["data_plane_interface"] == "bond_data"
    assert spec["data_template"]["network_data"]["links"]["bonds"][0]["id"] == "bond_control"
    worker_bonds = {
        item["id"]
        for item in spec["worker_data_template"]["network_data"]["links"]["bonds"]
    }
    assert worker_bonds == {"bond_control", "bond_data"}
    assert spec["ip_pool_manifests"][0]["kind"] == "IPPool"
    assert spec["ip_pool_manifests"][0]["spec"]["clusterName"] == "edge-lab"
    assert spec["sriov_node_policy_manifests"] == []
