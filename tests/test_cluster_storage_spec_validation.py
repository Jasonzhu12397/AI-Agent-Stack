# ruff: noqa: I001
from types import SimpleNamespace

import pytest
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
