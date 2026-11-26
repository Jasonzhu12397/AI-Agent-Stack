"""Day-2 scale changes only replicas, after proving the MachineDeployment owner."""

import copy
import json

import pytest

from apps.target_agent.cluster_scale import (
    ClusterScaleExecutionError,
    execute_cluster_scale,
)
from packages.agent_contracts.cluster_scale import (
    ClusterScaleError,
    simulation_plan,
    validate_request,
)

CLUSTER_UID = "cluster-uid-1"
DEPLOYMENT_ID = "deployment-1"


def _md(replicas=3, generation=2, status_replicas=3):
    return {
        "apiVersion": "cluster.x-k8s.io/v1beta1",
        "kind": "MachineDeployment",
        "metadata": {
            "name": "workers",
            "generation": generation,
            "labels": {
                "ai-agent-stack.io/managed-by": "target-agent",
                "ai-agent-stack.io/deployment-id": DEPLOYMENT_ID,
                "cluster.x-k8s.io/cluster-name": "cluster-1",
            },
            "annotations": {"ai-agent-stack.io/target-cluster-uid": CLUSTER_UID},
        },
        "spec": {"replicas": replicas},
        "status": {
            "replicas": status_replicas,
            "updatedReplicas": status_replicas,
            "readyReplicas": status_replicas,
            "observedGeneration": generation,
        },
    }


class FakeKubectl:
    def __init__(self, *, replicas=3, available=4, owned=True, duplicate_md=False):
        self.md = _md(replicas=replicas, status_replicas=replicas)
        self.available = available
        self.owned = owned
        self.duplicate_md = duplicate_md
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
            items = [copy.deepcopy(self.md)]
            if self.duplicate_md:
                items.append(copy.deepcopy(self.md))
            return {"available": True, "returncode": 0, "stdout": json.dumps({"items": items})}
        if arguments[:2] == ["get", "machinedeployments"]:
            document = copy.deepcopy(self.md)
            if not self.owned:
                document["metadata"]["labels"]["ai-agent-stack.io/managed-by"] = "someone-else"
            return {"available": True, "returncode": 0, "stdout": json.dumps(document)}
        if arguments[:2] == ["get", "baremetalhosts.metal3.io"]:
            items = [
                {
                    "spec": {"online": True, "consumerRef": None},
                    "status": {
                        "operationalStatus": "OK",
                        "provisioning": {"state": "available"},
                    },
                }
                for _ in range(self.available)
            ]
            return {"available": True, "returncode": 0, "stdout": json.dumps({"items": items})}
        if arguments[:2] == ["patch", "machinedeployments"]:
            payload = json.loads(arguments[arguments.index("-p") + 1])
            self.md["spec"]["replicas"] = payload["spec"]["replicas"]
            self.md["metadata"]["generation"] += 1
            self.md["status"] = {
                "replicas": payload["spec"]["replicas"],
                "updatedReplicas": payload["spec"]["replicas"],
                "readyReplicas": payload["spec"]["replicas"],
                "observedGeneration": self.md["metadata"]["generation"],
            }
            return {"available": True, "returncode": 0, "stdout": "machinedeployment patched\n"}
        raise AssertionError(f"unexpected kubectl call: {arguments}")


def _payload(mode="simulation", desired=5):
    return {
        "target_cluster_name": "cluster-1",
        "machine_deployment": "workers",
        "target_namespace": "default",
        "desired_workers": desired,
        "execution_mode": mode,
        "reason": "add capacity",
    }


def test_scale_discovery_requires_one_machine_deployment():
    unique = FakeKubectl()
    outcome = execute_cluster_scale(
        {**_payload("simulation"), "machine_deployment": None},
        unique,
        cluster_name="cluster-1",
    )
    assert outcome["machine_deployment"] == "workers"
    ambiguous = FakeKubectl(duplicate_md=True)
    with pytest.raises(ClusterScaleExecutionError) as caught:
        execute_cluster_scale(
            {**_payload("execute"), "machine_deployment": None},
            ambiguous,
            cluster_name="cluster-1",
        )
    assert caught.value.code == "CLUSTER_SCALE_MACHINE_DEPLOYMENT_AMBIGUOUS"


def test_contract_validates_names_count_and_cluster_identity():
    request = validate_request(_payload(), cluster_name="cluster-1")
    assert request["desired_workers"] == 5
    with pytest.raises(ClusterScaleError, match="enrolled"):
        validate_request(_payload(), cluster_name="other-cluster")
    with pytest.raises(ClusterScaleError, match="positive integer"):
        validate_request({**_payload(), "desired_workers": 0})
    with pytest.raises(ClusterScaleError, match="execution_mode"):
        validate_request({**_payload(), "execution_mode": "apply"})


def test_simulation_reports_capacity_without_side_effects():
    kubectl = FakeKubectl()
    outcome = execute_cluster_scale(_payload("simulation"), kubectl, cluster_name="cluster-1")
    assert outcome["action"] == "cluster.scale"
    assert outcome["current_workers"] == 3
    assert outcome["worker_delta"] == 2
    assert outcome["available_bmh_count"] == 4
    assert outcome["bmh_capacity_sufficient"] is True
    assert outcome["scale_simulated"] is True
    assert outcome["scale_performed"] is False
    assert outcome["side_effects"] is False
    assert not any(call[0][0] == "patch" for call in kubectl.calls)


def test_execute_patches_replicas_and_waits_for_reconciliation():
    kubectl = FakeKubectl(replicas=3, available=4)
    outcome = execute_cluster_scale(_payload("execute"), kubectl, cluster_name="cluster-1")
    assert outcome["scale_performed"] is True
    assert outcome["side_effects"] is True
    assert outcome["controller_replicas"] == 5
    assert outcome["controller_status"]["ready_replicas"] == 5
    patch_calls = [call for call in kubectl.calls if call[0][0] == "patch"]
    assert len(patch_calls) == 1
    assert "--type=merge" in patch_calls[0][0]
    assert "-p" in patch_calls[0][0]


def test_scale_out_blocks_before_patch_when_bmh_capacity_is_short():
    kubectl = FakeKubectl(replicas=3, available=1)
    with pytest.raises(ClusterScaleExecutionError) as caught:
        execute_cluster_scale(_payload("execute", desired=5), kubectl, cluster_name="cluster-1")
    assert caught.value.code == "CLUSTER_SCALE_BMH_CAPACITY_INSUFFICIENT"
    assert caught.value.side_effects is False
    assert not any(call[0][0] == "patch" for call in kubectl.calls)


def test_scale_down_does_not_require_new_hardware():
    kubectl = FakeKubectl(replicas=5, available=0)
    outcome = execute_cluster_scale(
        _payload("execute", desired=3), kubectl, cluster_name="cluster-1"
    )
    assert outcome["worker_delta"] == -2
    assert outcome["bmh_capacity_sufficient"] is True
    assert outcome["scale_performed"] is True


def test_foreign_machine_deployment_is_rejected_before_any_patch():
    kubectl = FakeKubectl(owned=False)
    with pytest.raises(ClusterScaleExecutionError) as caught:
        execute_cluster_scale(_payload("execute"), kubectl, cluster_name="cluster-1")
    assert caught.value.code == "CLUSTER_SCALE_MACHINE_DEPLOYMENT_NOT_OWNED"
    assert not any(call[0][0] == "patch" for call in kubectl.calls)


def test_no_op_scale_is_successful_without_side_effects():
    kubectl = FakeKubectl(replicas=5, available=0)
    outcome = execute_cluster_scale(
        _payload("execute", desired=5), kubectl, cluster_name="cluster-1"
    )
    assert outcome["scale_performed"] is False
    assert outcome["side_effects"] is False
    assert not any(call[0][0] == "patch" for call in kubectl.calls)


def test_plan_helper_exposes_the_capacity_decision():
    plan = simulation_plan(validate_request(_payload()), current_workers=4, available_bmh=0)
    assert plan["worker_delta"] == 1
    assert plan["bmh_capacity_sufficient"] is False
