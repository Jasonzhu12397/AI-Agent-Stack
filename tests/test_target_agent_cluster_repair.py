"""Repair deletes exactly one unhealthy Machine only after proving ownership."""

import copy
import json

import pytest

from apps.target_agent.cluster_repair import (
    ClusterRepairExecutionError,
    execute_cluster_repair,
)
from packages.agent_contracts.cluster_repair import ClusterRepairError, validate_request

CLUSTER_UID = "cluster-uid-1"
DEPLOYMENT_ID = "deployment-1"


def _machine(name, healthy=None, created="2026-11-26T02:00:00Z"):
    conditions = []
    if healthy is not None:
        conditions.append({"type": "NodeHealthy", "status": "True" if healthy else "False"})
    return {
        "apiVersion": "cluster.x-k8s.io/v1beta1",
        "kind": "Machine",
        "metadata": {
            "name": name,
            "creationTimestamp": created,
            "labels": {
                "cluster.x-k8s.io/cluster-name": "cluster-1",
                "cluster.x-k8s.io/deployment-name": "workers",
            },
        },
        "status": {"conditions": conditions},
    }


def _md(owned=True):
    return {
        "apiVersion": "cluster.x-k8s.io/v1beta1",
        "kind": "MachineDeployment",
        "metadata": {
            "name": "workers",
            "generation": 2,
            "labels": {
                "ai-agent-stack.io/managed-by": "target-agent" if owned else "someone-else",
                "ai-agent-stack.io/deployment-id": DEPLOYMENT_ID,
                "cluster.x-k8s.io/cluster-name": "cluster-1",
            },
            "annotations": {"ai-agent-stack.io/target-cluster-uid": CLUSTER_UID},
        },
        "spec": {"replicas": 3},
        "status": {"replicas": 3},
    }


class FakeKubectl:
    def __init__(self, machines=None, owned=True):
        self.md = _md(owned)
        self.machines = machines or [
            _machine("worker-1", True),
            _machine("worker-2", False),
            _machine("worker-3", True),
        ]
        self.calls = []

    def __call__(self, arguments, **kwargs):
        self.calls.append((list(arguments), kwargs))
        if arguments[:3] == ["get", "clusters.cluster.x-k8s.io", "cluster-1"]:
            return {
                "available": True,
                "returncode": 0,
                "stdout": json.dumps({"metadata": {"uid": CLUSTER_UID, "name": "cluster-1"}}),
            }
        if arguments[:2] == ["get", "machinedeployments"] and "-l" in arguments:
            return {
                "available": True,
                "returncode": 0,
                "stdout": json.dumps({"items": [copy.deepcopy(self.md)]}),
            }
        if arguments[:2] == ["get", "machinedeployments"]:
            return {"available": True, "returncode": 0, "stdout": json.dumps(self.md)}
        if arguments[:2] == ["get", "machines.cluster.x-k8s.io"]:
            return {
                "available": True,
                "returncode": 0,
                "stdout": json.dumps({"items": copy.deepcopy(self.machines)}),
            }
        if arguments[:2] == ["delete", "machines.cluster.x-k8s.io"]:
            name = arguments[2]
            self.machines = [
                machine for machine in self.machines if machine["metadata"]["name"] != name
            ]
            return {"available": True, "returncode": 0, "stdout": "deleted\n"}
        raise AssertionError(f"unexpected kubectl call: {arguments}")


def _payload(mode="simulation", **overrides):
    return {
        "target_cluster_name": "cluster-1",
        "target_namespace": "default",
        "machine_deployment": "workers",
        "execution_mode": mode,
        "reason": "replace an unhealthy worker",
        **overrides,
    }


def test_contract_rejects_a_foreign_cluster():
    with pytest.raises(ClusterRepairError, match="not enrolled"):
        validate_request(_payload(), cluster_name="other-cluster")


def test_simulation_selects_one_unhealthy_machine_without_side_effects():
    kubectl = FakeKubectl()
    outcome = execute_cluster_repair(_payload(), kubectl, cluster_name="cluster-1")
    assert outcome["total_machines"] == 3
    assert outcome["healthy_machines"] == 2
    assert outcome["unhealthy_machines"] == ["worker-2"]
    assert outcome["selected_machine"] == "worker-2"
    assert outcome["repair_simulated"] is True
    assert outcome["repair_performed"] is False
    assert outcome["side_effects"] is False
    assert not any(call[0][0] == "delete" for call in kubectl.calls)


def test_execute_deletes_the_selected_machine_only():
    kubectl = FakeKubectl()
    outcome = execute_cluster_repair(_payload("execute"), kubectl, cluster_name="cluster-1")
    assert outcome["repair_performed"] is True
    assert outcome["side_effects"] is True
    assert outcome["deleted_machines"] == ["worker-2"]
    assert outcome["replacement_expected"] is True
    deletes = [call[0] for call in kubectl.calls if call[0][0] == "delete"]
    assert len(deletes) == 1
    assert deletes[0][2] == "worker-2"
    assert "--wait=false" in deletes[0]


def test_a_named_healthy_machine_is_refused_before_any_delete():
    kubectl = FakeKubectl()
    with pytest.raises(ClusterRepairExecutionError) as caught:
        execute_cluster_repair(
            _payload("execute", machine_names=["worker-1"]),
            kubectl,
            cluster_name="cluster-1",
        )
    assert caught.value.code == "CLUSTER_REPAIR_MACHINE_IS_HEALTHY"
    assert not any(call[0][0] == "delete" for call in kubectl.calls)


def test_a_foreign_machine_deployment_is_rejected_before_any_delete():
    kubectl = FakeKubectl(owned=False)
    with pytest.raises(ClusterRepairExecutionError) as caught:
        execute_cluster_repair(_payload("execute"), kubectl, cluster_name="cluster-1")
    assert caught.value.code == "CLUSTER_SCALE_MACHINE_DEPLOYMENT_NOT_OWNED"
    assert not any(call[0][0] == "delete" for call in kubectl.calls)


def test_no_unhealthy_machine_is_a_successful_no_op():
    kubectl = FakeKubectl(machines=[_machine("worker-1", True), _machine("worker-2", True)])
    outcome = execute_cluster_repair(_payload("execute"), kubectl, cluster_name="cluster-1")
    assert outcome["unhealthy_machines"] == []
    assert outcome["selected_machine"] is None
    assert outcome["repair_performed"] is False
    assert outcome["side_effects"] is False
    assert not any(call[0][0] == "delete" for call in kubectl.calls)
