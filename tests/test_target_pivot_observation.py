from __future__ import annotations

import json

import pytest

from apps.target_agent import main as target_main


def _target_cluster():
    return {
        "metadata": {
            "name": "target-cluster",
            "namespace": "target",
            "uid": "target-cluster-uid",
            "generation": 3,
            "annotations": {
                "ai-agent-stack.io/source-cluster-uid": "source-cluster-uid",
                "ai-agent-stack.io/stable-cluster-identity": "logical-cluster-01",
            },
        },
        "status": {
            "observedGeneration": 3,
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


def _deployments():
    items = []
    for namespace, name in (
        ("capi-system", "capi-controller-manager"),
        ("metal3-system", "metal3-baremetal-operator"),
    ):
        items.append({
            "metadata": {
                "namespace": namespace,
                "name": name,
                "generation": 2,
            },
            "spec": {"replicas": 1},
            "status": {
                "observedGeneration": 2,
                "updatedReplicas": 1,
                "availableReplicas": 1,
            },
        })
    return {"items": items}


def test_pivot_observation_requires_source_not_found_and_ready_controllers(
    monkeypatch,
    tmp_path,
):
    source_kubeconfig = tmp_path / "source-readonly.kubeconfig"
    source_kubeconfig.write_text("read-only-source-context", encoding="utf-8")
    monkeypatch.setenv("CAPI_PIVOT_SOURCE_KUBECONFIG", str(source_kubeconfig))
    monkeypatch.setenv(
        "TARGET_REQUIRED_CONTROLLER_DEPLOYMENTS",
        "capi-system/capi-controller-manager,metal3-system/metal3-baremetal-operator",
    )

    def fake_kubectl(arguments, **kwargs):
        if arguments[:2] == ["get", "cluster.cluster.x-k8s.io"]:
            if kwargs.get("kubeconfig") == str(source_kubeconfig):
                return {
                    "available": False,
                    "stderr": 'Error from server (NotFound): cluster "target-cluster" not found',
                }
            return {"available": True, "stdout": json.dumps(_target_cluster())}
        if arguments[:2] == ["get", "deployments"]:
            return {"available": True, "stdout": json.dumps(_deployments())}
        raise AssertionError(arguments)

    monkeypatch.setattr(target_main, "_kubectl", fake_kubectl)
    result = target_main._observe_target_capi_pivot({
        "namespace": "target",
        "cluster_name": "target-cluster",
        "source_cluster_uid": "source-cluster-uid",
        "stable_cluster_identity": "logical-cluster-01",
    })
    assert result["source_cluster_absent"] is True
    assert result["source_cluster_absence_observed"] is True
    assert result["target_controllers_ready"] is True


def test_pivot_observation_does_not_treat_source_api_failure_as_absence(
    monkeypatch,
    tmp_path,
):
    source_kubeconfig = tmp_path / "source-readonly.kubeconfig"
    source_kubeconfig.write_text("read-only-source-context", encoding="utf-8")
    monkeypatch.setenv("CAPI_PIVOT_SOURCE_KUBECONFIG", str(source_kubeconfig))
    monkeypatch.setenv(
        "TARGET_REQUIRED_CONTROLLER_DEPLOYMENTS",
        "capi-system/capi-controller-manager",
    )

    def fake_kubectl(arguments, **kwargs):
        if arguments[:2] == ["get", "cluster.cluster.x-k8s.io"]:
            if kwargs.get("kubeconfig") == str(source_kubeconfig):
                return {"available": False, "stderr": "The connection to the server timed out"}
            return {"available": True, "stdout": json.dumps(_target_cluster())}
        if arguments[:2] == ["get", "deployments"]:
            return {"available": True, "stdout": json.dumps(_deployments())}
        raise AssertionError(arguments)

    monkeypatch.setattr(target_main, "_kubectl", fake_kubectl)
    with pytest.raises(target_main.HandoffError, match="SOURCE_ABSENCE_NOT_VERIFIED"):
        target_main._observe_target_capi_pivot({
            "namespace": "target",
            "cluster_name": "target-cluster",
            "source_cluster_uid": "source-cluster-uid",
            "stable_cluster_identity": "logical-cluster-01",
        })


def test_pivot_observation_rejects_unready_required_controller(
    monkeypatch,
    tmp_path,
):
    source_kubeconfig = tmp_path / "source-readonly.kubeconfig"
    source_kubeconfig.write_text("read-only-source-context", encoding="utf-8")
    monkeypatch.setenv("CAPI_PIVOT_SOURCE_KUBECONFIG", str(source_kubeconfig))
    monkeypatch.setenv(
        "TARGET_REQUIRED_CONTROLLER_DEPLOYMENTS",
        "capi-system/capi-controller-manager",
    )
    deployments = _deployments()
    deployments["items"][0]["status"]["availableReplicas"] = 0

    def fake_kubectl(arguments, **kwargs):
        if arguments[:2] == ["get", "cluster.cluster.x-k8s.io"]:
            if kwargs.get("kubeconfig") == str(source_kubeconfig):
                return {"available": False, "stderr": "NotFound: cluster not found"}
            return {"available": True, "stdout": json.dumps(_target_cluster())}
        if arguments[:2] == ["get", "deployments"]:
            return {"available": True, "stdout": json.dumps(deployments)}
        raise AssertionError(arguments)

    monkeypatch.setattr(target_main, "_kubectl", fake_kubectl)
    with pytest.raises(target_main.HandoffError, match="CONTROLLER_NOT_READY"):
        target_main._observe_target_capi_pivot({
            "namespace": "target",
            "cluster_name": "target-cluster",
            "source_cluster_uid": "source-cluster-uid",
            "stable_cluster_identity": "logical-cluster-01",
        })
