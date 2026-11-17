import hashlib
import json

import pytest

from apps.target_agent import main as target_main
from apps.target_agent.metal3_ownership import (
    Metal3OwnershipError,
    verify_metal3_ownership,
)


def ownership_documents():
    cluster = {
        "metadata": {
            "name": "target-cluster",
            "namespace": "metal3-system",
            "uid": "target-cluster-uid",
            "annotations": {
                "ai-agent-stack.io/source-cluster-uid": "source-cluster-uid",
            },
        }
    }
    machines = []
    metal3machines = []
    bmhs = []
    source_identities = []
    for index in range(3):
        machine_name = f"control-plane-{index}"
        machine_uid = f"machine-uid-{index}"
        metal3machine_uid = f"metal3machine-uid-{index}"
        host_name = f"bmh-{index}"
        system_uuid = f"system-uuid-{index}"
        hardware_identity = (
            "system-uuid:sha256:"
            + hashlib.sha256(system_uuid.encode()).hexdigest()
        )
        machines.append({
            "metadata": {
                "name": machine_name,
                "uid": machine_uid,
                "ownerReferences": [{
                    "kind": "Cluster",
                    "name": "target-cluster",
                    "uid": "target-cluster-uid",
                    "controller": True,
                }],
            },
            "spec": {
                "infrastructureRef": {
                    "apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1",
                    "kind": "Metal3Machine",
                    "name": machine_name,
                    "namespace": "metal3-system",
                },
            },
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        })
        metal3machines.append({
            "metadata": {
                "name": machine_name,
                "namespace": "metal3-system",
                "uid": metal3machine_uid,
                "ownerReferences": [{
                    "kind": "Machine",
                    "name": machine_name,
                    "uid": machine_uid,
                    "controller": True,
                }],
            },
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
            },
        })
        bmhs.append({
            "metadata": {
                "name": host_name,
                "namespace": "metal3-system",
                "uid": f"bmh-uid-{index}",
            },
            "spec": {
                "bootMACAddress": f"02:00:00:00:00:{index:02x}",
                "consumerRef": {
                    "apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1",
                    "kind": "Metal3Machine",
                    "name": machine_name,
                    "namespace": "metal3-system",
                    "uid": metal3machine_uid,
                },
            },
            "status": {
                "hardware": {"systemUUID": system_uuid},
                "operationalStatus": "OK",
                "provisioning": {
                    "state": "provisioned",
                    "ID": f"00000000-0000-4000-8000-{index:012d}",
                },
            },
        })
        source_identities.append({
            "namespace": "metal3-system",
            "name": host_name,
            "hardware_identity": hardware_identity,
        })
    return (
        cluster,
        {"items": machines},
        {"items": metal3machines},
        {"items": bmhs},
        source_identities,
    )


def trusted_deployment_context(source):
    normalized = sorted(
        source,
        key=lambda item: (
            item["namespace"],
            item["name"],
            item["hardware_identity"],
        ),
    )
    digest = hashlib.sha256(
        json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "eph_pivot_attestation": {
            "source_cluster_uid": "source-cluster-uid",
            "target_cluster_uid": "target-cluster-uid",
            "source_bmh_identities": normalized,
            "source_bmh_digest": digest,
            "target_bmh_digest": digest,
            "source_agent_id": "eph-agent-test",
            "command_id": "eph-pivot-command-test",
            "attested_at": "2026-11-17T00:00:00Z",
        },
    }


def observe(cluster=None, machines=None, metal3machines=None, bmhs=None, source=None):
    values = ownership_documents()
    return verify_metal3_ownership(
        values[0] if cluster is None else cluster,
        values[1] if machines is None else machines,
        values[2] if metal3machines is None else metal3machines,
        values[3] if bmhs is None else bmhs,
        values[4] if source is None else source,
    )


def test_target_metal3_ownership_verifies_cluster_machine_metal3machine_bmh_chain():
    result = observe()
    assert result["target_metal3_ownership_verified"] is True
    assert result["target_metal3_reconciliation_healthy"] is True
    assert result["machine_count"] == 3
    assert result["metal3machine_count"] == 3
    assert result["source_target_bmh_identity_sets_equal"] is True
    assert result["side_effects"] is False


def test_target_metal3_ownership_rejects_machine_without_cluster_owner():
    _, machines, metal3machines, bmhs, source = ownership_documents()
    machines["items"][0]["metadata"]["ownerReferences"] = []
    with pytest.raises(Metal3OwnershipError, match="CLUSTER_OWNER_MISMATCH"):
        observe(machines=machines, metal3machines=metal3machines, bmhs=bmhs, source=source)


def test_target_metal3_ownership_rejects_machine_without_ready_condition():
    _, machines, metal3machines, bmhs, source = ownership_documents()
    machines["items"][0]["status"]["conditions"][0]["status"] = "False"
    with pytest.raises(Metal3OwnershipError, match="MACHINE_NOT_READY"):
        observe(machines=machines, metal3machines=metal3machines, bmhs=bmhs, source=source)


def test_target_metal3_ownership_rejects_metal3machine_owner_uid_mismatch():
    _, machines, metal3machines, bmhs, source = ownership_documents()
    metal3machines["items"][0]["metadata"]["ownerReferences"][0]["uid"] = "stale-uid"
    with pytest.raises(Metal3OwnershipError, match="MACHINE_UID_MISMATCH"):
        observe(machines=machines, metal3machines=metal3machines, bmhs=bmhs, source=source)


def test_target_metal3_ownership_rejects_unhealthy_bmh():
    _, machines, metal3machines, bmhs, source = ownership_documents()
    bmhs["items"][0]["status"]["operationalStatus"] = "error"
    with pytest.raises(Metal3OwnershipError, match="NOT_OPERATIONAL"):
        observe(machines=machines, metal3machines=metal3machines, bmhs=bmhs, source=source)


def test_target_metal3_ownership_rejects_bmh_consumer_reference_mismatch():
    _, machines, metal3machines, bmhs, source = ownership_documents()
    bmhs["items"][0]["spec"]["consumerRef"]["uid"] = "stale-uid"
    with pytest.raises(Metal3OwnershipError, match="CONSUMER_REFERENCE_MISMATCH"):
        observe(machines=machines, metal3machines=metal3machines, bmhs=bmhs, source=source)


def test_target_metal3_ownership_rejects_source_target_hardware_identity_mismatch():
    _, machines, metal3machines, bmhs, source = ownership_documents()
    source[0]["hardware_identity"] = "system-uuid:sha256:wrong"
    with pytest.raises(Metal3OwnershipError, match="IDENTITY_SET_DIFFERS"):
        observe(machines=machines, metal3machines=metal3machines, bmhs=bmhs, source=source)


def test_target_metal3_ownership_rejects_duplicate_source_hardware_identity():
    _, machines, metal3machines, bmhs, source = ownership_documents()
    source[1]["hardware_identity"] = source[0]["hardware_identity"]
    with pytest.raises(Metal3OwnershipError, match="IDENTITY_DUPLICATE"):
        observe(machines=machines, metal3machines=metal3machines, bmhs=bmhs, source=source)


def test_target_metal3_ownership_rejects_incomplete_control_plane_inventory():
    _, machines, metal3machines, bmhs, source = ownership_documents()
    machines["items"] = machines["items"][:2]
    with pytest.raises(Metal3OwnershipError, match="MACHINES_INCOMPLETE"):
        observe(machines=machines, metal3machines=metal3machines, bmhs=bmhs, source=source)



def test_target_agent_verifies_every_bmh_against_live_ironic_api(monkeypatch):
    cluster, machines, metal3machines, bmhs, source = ownership_documents()
    documents = {
        "cluster.cluster.x-k8s.io": cluster,
        "machines.cluster.x-k8s.io": machines,
        "metal3machines.infrastructure.cluster.x-k8s.io": metal3machines,
        "baremetalhosts.metal3.io": bmhs,
    }
    ironic_calls = []

    def fake_kubectl(arguments, **_kwargs):
        resource = arguments[1]
        return {
            "available": True,
            "returncode": 0,
            "stdout": json.dumps(documents[resource]),
            "stderr": "",
        }

    def fake_ironic_node(node_uuid):
        ironic_calls.append(node_uuid)
        return {
            "uuid": node_uuid,
            "provision_state": "active",
            "maintenance": False,
            "target_provision_state": None,
            "last_error": None,
        }

    monkeypatch.setattr(target_main, "_kubectl", fake_kubectl)
    monkeypatch.setattr(target_main, "ironic_api_configured", lambda: True)
    monkeypatch.setattr(target_main, "get_ironic_node", fake_ironic_node)
    result = target_main._observe_target_metal3_ownership(
        {
            "namespace": "metal3-system",
            "cluster_name": "target-cluster",
        },
        deployment_context=trusted_deployment_context(source),
    )
    assert result["target_ironic_inventory_verified"] is True
    assert result["ironic_node_count"] == 3
    assert len(ironic_calls) == 3


def test_target_agent_fails_closed_when_ironic_api_is_not_configured(monkeypatch):
    cluster, machines, metal3machines, bmhs, source = ownership_documents()
    documents = {
        "cluster.cluster.x-k8s.io": cluster,
        "machines.cluster.x-k8s.io": machines,
        "metal3machines.infrastructure.cluster.x-k8s.io": metal3machines,
        "baremetalhosts.metal3.io": bmhs,
    }
    monkeypatch.setattr(
        target_main,
        "_kubectl",
        lambda arguments, **_kwargs: {
            "available": True,
            "returncode": 0,
            "stdout": json.dumps(documents[arguments[1]]),
            "stderr": "",
        },
    )
    monkeypatch.setattr(target_main, "ironic_api_configured", lambda: False)
    with pytest.raises(Metal3OwnershipError, match="IRONIC_API_CONFIGURATION_REQUIRED"):
        target_main._observe_target_metal3_ownership(
            {
                "namespace": "metal3-system",
                "cluster_name": "target-cluster",
            },
            deployment_context=trusted_deployment_context(source),
        )


def test_target_agent_rejects_ironic_node_in_maintenance(monkeypatch):
    cluster, machines, metal3machines, bmhs, source = ownership_documents()
    documents = {
        "cluster.cluster.x-k8s.io": cluster,
        "machines.cluster.x-k8s.io": machines,
        "metal3machines.infrastructure.cluster.x-k8s.io": metal3machines,
        "baremetalhosts.metal3.io": bmhs,
    }
    monkeypatch.setattr(
        target_main,
        "_kubectl",
        lambda arguments, **_kwargs: {
            "available": True,
            "returncode": 0,
            "stdout": json.dumps(documents[arguments[1]]),
            "stderr": "",
        },
    )
    monkeypatch.setattr(target_main, "ironic_api_configured", lambda: True)
    monkeypatch.setattr(
        target_main,
        "get_ironic_node",
        lambda node_uuid: {
            "uuid": node_uuid,
            "provision_state": "active",
            "maintenance": True,
            "target_provision_state": None,
            "last_error": None,
        },
    )
    with pytest.raises(Metal3OwnershipError, match="IRONIC_NODE_IN_MAINTENANCE"):
        target_main._observe_target_metal3_ownership(
            {
                "namespace": "metal3-system",
                "cluster_name": "target-cluster",
            },
            deployment_context=trusted_deployment_context(source),
        )


def test_target_agent_rejects_malformed_target_cluster_annotations(monkeypatch):
    cluster, machines, metal3machines, bmhs, source = ownership_documents()
    cluster["metadata"]["annotations"] = None
    documents = {
        "cluster.cluster.x-k8s.io": cluster,
        "machines.cluster.x-k8s.io": machines,
        "metal3machines.infrastructure.cluster.x-k8s.io": metal3machines,
        "baremetalhosts.metal3.io": bmhs,
    }
    monkeypatch.setattr(
        target_main,
        "_kubectl",
        lambda arguments, **_kwargs: {
            "available": True,
            "returncode": 0,
            "stdout": json.dumps(documents[arguments[1]]),
            "stderr": "",
        },
    )
    with pytest.raises(Metal3OwnershipError, match="TARGET_CLUSTER_ANNOTATIONS_INVALID"):
        target_main._observe_target_metal3_ownership(
            {
                "namespace": "metal3-system",
                "cluster_name": "target-cluster",
            },
            deployment_context=trusted_deployment_context(source),
        )
