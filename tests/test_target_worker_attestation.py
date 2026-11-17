from __future__ import annotations

import pytest

from apps.target_agent.worker_attestation import (
    WorkerAttestationError,
    verify_worker_readiness,
)


def _documents(ntp_ip: str = "10.0.0.10"):
    deployment = {
        "metadata": {"name": "workers", "namespace": "target"},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"pool": "worker-a"}},
        },
    }
    machine = {
        "metadata": {
            "name": "workers-abc",
            "namespace": "target",
            "uid": "machine-uid",
            "labels": {
                "cluster.x-k8s.io/deployment-name": "workers",
                "pool": "worker-a",
            },
        },
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "bootstrapReady": True,
            "nodeRef": {"name": "worker-01"},
        },
        "spec": {
            "bootstrap": {
                "configRef": {"kind": "KubeadmConfig", "name": "workers-abc"},
            },
        },
    }
    config = {
        "metadata": {
            "name": "workers-abc",
            "ownerReferences": [{
                "kind": "Machine",
                "name": "workers-abc",
                "uid": "machine-uid",
                "controller": True,
            }],
        },
        "status": {"ready": True},
        "spec": {
            "preKubeadmCommands": [
                f"server {ntp_ip} iburst prefer",
                "systemctl restart chrony",
                "chronyc -n sources",
                "chronyc tracking",
                "if ! chronyc tracking | grep -Eiq '^Leap status[[:space:]]*:[[:space:]]*Normal'; then exit 1; fi",
            ],
        },
    }
    node = {
        "metadata": {"name": "worker-01"},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    return deployment, {"items": [machine]}, {"items": [config]}, {"items": [node]}


def test_worker_readiness_uses_live_machine_node_and_ntp_evidence():
    documents = _documents()
    result = verify_worker_readiness(*documents, "10.0.0.10")
    assert result["workers_ready"] is True
    assert result["ready_worker_count"] == 1
    assert result["worker_ntp_sources"] == {"worker-01": ["10.0.0.10"]}


def test_worker_readiness_rejects_external_ntp_source():
    documents = _documents("192.0.2.20")
    with pytest.raises(WorkerAttestationError, match="NTP_SOURCE_MISMATCH"):
        verify_worker_readiness(*documents, "10.0.0.10")


def test_worker_readiness_rejects_unready_kubernetes_node():
    deployment, machines, configs, nodes = _documents()
    nodes["items"][0]["status"]["conditions"][0]["status"] = "False"
    with pytest.raises(WorkerAttestationError, match="NODE_NOT_READY"):
        verify_worker_readiness(deployment, machines, configs, nodes, "10.0.0.10")


def test_worker_readiness_rejects_machine_owner_uid_mismatch():
    deployment, machines, configs, nodes = _documents()
    configs["items"][0]["metadata"]["ownerReferences"][0]["uid"] = "wrong-uid"
    with pytest.raises(WorkerAttestationError, match="OWNER_MISMATCH"):
        verify_worker_readiness(deployment, machines, configs, nodes, "10.0.0.10")


def test_worker_readiness_rejects_missing_chrony_sync_gate():
    deployment, machines, configs, nodes = _documents()
    configs["items"][0]["spec"]["preKubeadmCommands"] = ["server 10.0.0.10 iburst"]
    with pytest.raises(WorkerAttestationError, match="SYNC_GATE_MISSING"):
        verify_worker_readiness(deployment, machines, configs, nodes, "10.0.0.10")
