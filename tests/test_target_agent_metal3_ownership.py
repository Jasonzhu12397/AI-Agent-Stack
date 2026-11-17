import pytest

from apps.target_agent.metal3_ownership import (
    Metal3OwnershipError,
    verify_metal3_ownership,
)


def _owner(kind, name, uid):
    return {
        "apiVersion": "cluster.x-k8s.io/v1beta1",
        "kind": kind,
        "name": name,
        "uid": uid,
        "controller": True,
    }


def test_metal3_ownership_verifies_cluster_machine_m3m_and_bmh_chain():
    cluster = {
        "metadata": {
            "name": "target-cluster",
            "namespace": "metal3",
            "uid": "target-cluster-uid",
        }
    }
    machines = []
    metal3machines = []
    bmhs = []
    source_identities = []
    for index in range(1, 4):
        machine_name = f"target-control-plane-{index}"
        machine_uid = f"machine-uid-{index}"
        m3m_name = f"metal3-{index}"
        m3m_uid = f"metal3-uid-{index}"
        host_name = f"host-{index}"
        hardware_identity = f"system-uuid:system-{index}"
        machines.append({
            "metadata": {
                "name": machine_name,
                "uid": machine_uid,
                "ownerReferences": [_owner("Cluster", "target-cluster", "target-cluster-uid")],
            },
            "spec": {
                "infrastructureRef": {
                    "apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1",
                    "kind": "Metal3Machine",
                    "name": m3m_name,
                    "namespace": "metal3",
                }
            },
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        })
        metal3machines.append({
            "metadata": {
                "name": m3m_name,
                "namespace": "metal3",
                "uid": m3m_uid,
                "ownerReferences": [_owner("Machine", machine_name, machine_uid)],
            },
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        })
        bmhs.append({
            "metadata": {"name": host_name, "namespace": "metal3"},
            "spec": {
                "bootMACAddress": f"aa:bb:cc:dd:ee:0{index}",
                "consumerRef": {
                    "apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1",
                    "kind": "Metal3Machine",
                    "name": m3m_name,
                    "namespace": "metal3",
                    "uid": m3m_uid,
                },
            },
            "status": {
                "hardware": {"systemUUID": f"system-{index}"},
                "operationalStatus": "OK",
                "provisioning": {"state": "provisioned"},
            },
        })
        source_identities.append({
            "namespace": "metal3",
            "name": host_name,
            "hardware_identity": hardware_identity,
        })
    result = verify_metal3_ownership(
        cluster,
        {"items": machines},
        {"items": metal3machines},
        {"items": bmhs},
        source_identities,
    )
    assert result["target_metal3_ownership_verified"] is True
    assert result["source_target_bmh_identity_sets_equal"] is True


def test_metal3_ownership_rejects_machine_with_wrong_cluster_owner():
    cluster = {
        "metadata": {
            "name": "target-cluster",
            "namespace": "metal3",
            "uid": "target-cluster-uid",
        }
    }
    machine = {
        "metadata": {
            "name": "machine-1",
            "uid": "machine-uid-1",
            "ownerReferences": [_owner("Cluster", "other-cluster", "other-uid")],
        }
    }
    with pytest.raises(Metal3OwnershipError, match="MACHINE_CLUSTER_OWNER_MISMATCH"):
        verify_metal3_ownership(
            cluster,
            {"items": [machine, machine, machine]},
            {"items": [{}]},
            {"items": [{}]},
            [{"namespace": "metal3", "name": "host-1", "hardware_identity": "system-uuid:abc"}],
        )
