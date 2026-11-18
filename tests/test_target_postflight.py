from __future__ import annotations

import hashlib
import json

import pytest

from apps.target_agent import main as target_main

from apps.target_agent.postflight import PostflightError, verify_target_postflight


def _ready_daemonset(name, namespace="kube-system"):
    return {
        "metadata": {"name": name, "namespace": namespace, "labels": {"app": name}},
        "status": {
            "desiredNumberScheduled": 2,
            "numberReady": 2,
            "numberAvailable": 2,
        },
    }


def _node(name, control_plane=False):
    labels = {"node-role.kubernetes.io/control-plane": ""} if control_plane else {}
    return {
        "metadata": {"name": name, "labels": labels},
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "allocatable": {
                "intel.com/sriov_dpdk": "8",
                "intel.com/dpdk": "8",
            },
        },
    }


def _fixtures():
    documents = {
        "clusters.cluster.x-k8s.io": {
            "metadata": {"name": "target", "namespace": "metal3"},
            "spec": {
                "controlPlaneRef": {
                    "apiGroup": "controlplane.cluster.x-k8s.io",
                    "kind": "KubeadmControlPlane",
                    "name": "target-control-plane",
                },
            },
            "status": {"infrastructureReady": True, "controlPlaneReady": True},
        },
        "kubeadmcontrolplanes.controlplane.cluster.x-k8s.io": {
            "spec": {"replicas": 3, "version": "v1.37.0"},
            "status": {"readyReplicas": 3, "initialized": True},
        },
        "machinedeployments.cluster.x-k8s.io": {
            "metadata": {
                "name": "target-workers",
                "namespace": "metal3",
                "generation": 4,
                "labels": {
                    "app.kubernetes.io/managed-by": "target-agent",
                    "ai-agent-stack.io/deployment-id": "deployment-1",
                },
                "annotations": {
                    "ai-agent-stack.io/target-cluster-uid": "target-cluster-uid",
                },
            },
            "spec": {
                "replicas": 2,
                "clusterName": "target",
                "template": {"spec": {"version": "v1.37.0"}},
            },
            "status": {
                "readyReplicas": 2,
                "updatedReplicas": 2,
                "availableReplicas": 2,
                "observedGeneration": 4,
            },
        },
        "machines.cluster.x-k8s.io": {
            "items": [
                {
                    "metadata": {
                        "labels": {
                            "cluster.x-k8s.io/deployment-name": "target-workers",
                            "cluster.x-k8s.io/cluster-name": "target",
                        },
                    },
                    "status": {
                        "phase": "Running",
                        "infrastructureReady": True,
                        "bootstrapReady": True,
                        "nodeRef": {"name": "worker-1"},
                    },
                },
                {
                    "metadata": {
                        "labels": {
                            "cluster.x-k8s.io/deployment-name": "target-workers",
                            "cluster.x-k8s.io/cluster-name": "target",
                        },
                    },
                    "status": {
                        "phase": "Running",
                        "infrastructureReady": True,
                        "bootstrapReady": True,
                        "nodeRef": {"name": "worker-2"},
                    },
                },
            ],
        },
        "nodes": {
            "items": [
                _node("master-1", control_plane=True),
                _node("worker-1"),
                _node("worker-2"),
                _node("infra-1"),
            ],
        },
        "daemonsets.apps": {
            "items": [
                _ready_daemonset("calico-node"),
                _ready_daemonset("kube-multus-ds"),
                _ready_daemonset("sriov-network-config-daemon", "sriov-network-operator"),
                _ready_daemonset("ovs-cni"),
            ],
        },
        "network-attachment-definitions.k8s.cni.cncf.io": {
            "items": [
                {
                    "metadata": {"name": "data-sriov", "namespace": "metal3"},
                    "spec": {
                        "config": json.dumps({
                            "cniVersion": "0.3.1",
                            "type": "sriov",
                            "resourceName": "intel.com/sriov_dpdk",
                        }),
                    },
                },
                {
                    "metadata": {"name": "data-ovs", "namespace": "metal3"},
                    "spec": {"config": json.dumps({"type": "ovs"})},
                },
            ],
        },
    }

    def runner(arguments, **_kwargs):
        document = documents[arguments[1]]
        return {"available": True, "stdout": json.dumps(document), "stderr": ""}
    return runner, documents


def _kwargs():
    return {
        "cluster_name": "target",
        "namespace": "metal3",
        "machine_deployment_name": "target-workers",
        "deployment_id": "deployment-1",
        "desired_worker_count": 2,
        "ironic_internal_ntp_ip": "10.130.60.84",
        "worker_ntp_bootstrap_sources": {
            "worker-1": ["10.130.60.84"],
            "worker-2": ["10.130.60.84"],
        },
        "worker_network_contract": {
            "calico_enabled": True,
            "multus_enabled": True,
            "ovs_enabled": True,
            "sriov_cni_enabled": True,
            "sriov_dpdk_enabled": True,
            "pod_traffic_network": "data",
            "control_plane_interface": "eno1",
            "data_plane_interface": "ens1f0",
            "control_plane_cidr": "10.130.60.0/24",
            "data_plane_cidr": "10.130.61.0/24",
        },
        "ownership_evidence": {
            "target_metal3_ownership_verified": True,
            "target_metal3_reconciliation_healthy": True,
            "target_ironic_inventory_verified": True,
            "target_cluster_uid": "target-cluster-uid",
        },
        "deployment_checkpoint_durable": True,
    }


def test_postflight_requires_live_cluster_worker_and_network_evidence():
    runner, _documents = _fixtures()
    result = verify_target_postflight(runner=runner, **_kwargs())
    assert result["target_postflight_passed"] is True
    assert result["worker_network_postflight_passed"] is True
    assert result["ready_worker_count"] == 2
    assert result["ready_control_plane_count"] == 3
    assert result["worker_ntp_bootstrap_attested"] is True
    assert result["sriov_nad_count"] == 1
    assert result["ovs_nad_count"] == 1
    assert result["ovs_daemonset"] == "kube-system/ovs-cni"


def test_postflight_rejects_workers_without_dpdk_device_resources():
    runner, documents = _fixtures()
    for node in documents["nodes"]["items"]:
        node["status"]["allocatable"].pop("intel.com/dpdk")
        node["status"]["allocatable"].pop("intel.com/sriov_dpdk")
    with pytest.raises(PostflightError, match="POSTFLIGHT_DPDK_DEVICE_RESOURCE_MISSING"):
        verify_target_postflight(runner=runner, **_kwargs())


def test_postflight_rejects_worker_ntp_source_outside_ironic_internal_ip():
    runner, _documents = _fixtures()
    kwargs = _kwargs()
    kwargs["worker_ntp_bootstrap_sources"]["worker-2"] = ["100.112.135.196"]
    with pytest.raises(PostflightError, match="POSTFLIGHT_WORKER_NTP_SOURCE_MISMATCH"):
        verify_target_postflight(runner=runner, **kwargs)


def test_postflight_rejects_unready_capi_worker_machine():
    runner, documents = _fixtures()
    documents["machines.cluster.x-k8s.io"]["items"][1]["status"]["phase"] = "Provisioning"
    with pytest.raises(PostflightError, match="POSTFLIGHT_CAPI_WORKER_MACHINES_NOT_READY"):
        verify_target_postflight(runner=runner, **_kwargs())


def test_postflight_rejects_missing_ovs_network_attachment_definition():
    runner, documents = _fixtures()
    documents["network-attachment-definitions.k8s.cni.cncf.io"]["items"] = [
        documents["network-attachment-definitions.k8s.cni.cncf.io"]["items"][0]
    ]
    with pytest.raises(PostflightError, match="POSTFLIGHT_OVS_DATA_PLANE_NAD_MISSING"):
        verify_target_postflight(runner=runner, **_kwargs())



def test_durable_checkpoint_evidence_is_verified_before_postflight():
    evidence = {
        "desired_worker_count": 2,
        "worker_network_contract": {"pod_traffic_network": "data"},
    }
    canonical = json.dumps(
        evidence,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    checkpoint = {
        "phase": "workers_ready",
        "sequence": 8,
        "state": {
            "state_digest": hashlib.sha256(canonical).hexdigest(),
            "evidence": evidence,
        },
    }
    assert target_main._validated_checkpoint_evidence(
        checkpoint,
        "workers_ready",
    ) == evidence


def test_postflight_rejects_tampered_checkpoint_evidence():
    evidence = {"desired_worker_count": 2}
    checkpoint = {
        "phase": "workers_ready",
        "sequence": 8,
        "state": {
            "state_digest": "0" * 64,
            "evidence": evidence,
        },
    }
    with pytest.raises(
        target_main.HandoffError,
        match="HANDOFF_DURABLE_CHECKPOINT_DIGEST_MISMATCH",
    ):
        target_main._validated_checkpoint_evidence(
            checkpoint,
            "workers_ready",
        )



def test_postflight_uses_capi_machine_node_references_not_all_non_control_plane_nodes():
    runner, _documents = _fixtures()
    result = verify_target_postflight(runner=runner, **_kwargs())
    assert result["ready_worker_count"] == 2


def test_postflight_rejects_machine_without_node_reference():
    runner, documents = _fixtures()
    documents["machines.cluster.x-k8s.io"]["items"][1]["status"].pop("nodeRef")
    with pytest.raises(PostflightError, match="POSTFLIGHT_CAPI_WORKER_MACHINES_NOT_READY"):
        verify_target_postflight(runner=runner, **_kwargs())


def test_postflight_rejects_unready_ovs_daemonset():
    runner, documents = _fixtures()
    documents["daemonsets.apps"]["items"][-1]["status"]["numberReady"] = 1
    with pytest.raises(PostflightError, match="POSTFLIGHT_OVS_DAEMONSET_NOT_READY"):
        verify_target_postflight(runner=runner, **_kwargs())
