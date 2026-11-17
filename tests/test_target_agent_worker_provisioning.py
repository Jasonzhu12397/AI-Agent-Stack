import json
from threading import Event

import pytest

from apps.target_agent.worker_provisioning import (
    WorkerProvisioningError,
    execute_worker_provisioning,
)


NTP_IP = "10.130.60.84"


def _resource_bundle():
    return [
        {
            "apiVersion": "bootstrap.cluster.x-k8s.io/v1beta1",
            "kind": "KubeadmConfigTemplate",
            "metadata": {"name": "worker-bootstrap-v2", "namespace": "metal3"},
            "spec": {"template": {"spec": {"preKubeadmCommands": ["echo bootstrap"]}}},
        },
        {
            "apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1",
            "kind": "Metal3MachineTemplate",
            "metadata": {"name": "worker-infra-v2", "namespace": "metal3"},
            "spec": {"template": {"spec": {"userData": {"name": "worker-user-data"}}}},
        },
        {
            "apiVersion": "cluster.x-k8s.io/v1beta1",
            "kind": "MachineDeployment",
            "metadata": {"name": "workers", "namespace": "metal3"},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"cluster.x-k8s.io/deployment-name": "workers"}},
                "template": {
                    "metadata": {"labels": {"cluster.x-k8s.io/deployment-name": "workers"}},
                    "spec": {
                        "clusterName": "target-cluster",
                        "bootstrap": {
                            "configRef": {
                                "apiVersion": "bootstrap.cluster.x-k8s.io/v1beta1",
                                "kind": "KubeadmConfigTemplate",
                                "name": "worker-bootstrap-v2",
                                "namespace": "metal3",
                            }
                        },
                        "infrastructureRef": {
                            "apiVersion": "infrastructure.cluster.x-k8s.io/v1beta1",
                            "kind": "Metal3MachineTemplate",
                            "name": "worker-infra-v2",
                            "namespace": "metal3",
                        },
                    },
                },
            },
        },
    ]


def _payload():
    return {
        "namespace": "metal3",
        "desired_worker_count": 1,
        "ironic_internal_ntp_ip": NTP_IP,
        "timeout_seconds": 60,
        "resources": _resource_bundle(),
    }


def _success_runner(calls):
    def runner(arguments, input_text=None, timeout=30):
        calls.append((arguments, input_text))
        if arguments[0] == "get" and arguments[-1] == "json":
            resource = arguments[1]
            if resource == "machinedeployments.cluster.x-k8s.io":
                return {
                    "ok": True,
                    "stdout": json.dumps({
                        "metadata": {"generation": 2},
                        "spec": {"replicas": 1},
                        "status": {
                            "observedGeneration": 2,
                            "replicas": 1,
                            "updatedReplicas": 1,
                            "readyReplicas": 1,
                            "availableReplicas": 1,
                        },
                    }),
                    "stderr": "",
                }
            if resource == "machines.cluster.x-k8s.io":
                return {
                    "ok": True,
                    "stdout": json.dumps({
                        "items": [{
                            "status": {"nodeRef": {"name": "worker-1"}},
                        }],
                    }),
                    "stderr": "",
                }
            if resource == "nodes":
                return {
                    "ok": True,
                    "stdout": json.dumps({
                        "items": [{
                            "metadata": {"name": "worker-1"},
                            "status": {
                                "conditions": [{"type": "Ready", "status": "True"}],
                            },
                        }],
                    }),
                    "stderr": "",
                }
        if arguments[0] == "get":
            return {"ok": False, "stderr": "NotFound"}
        return {"ok": True, "stdout": "created", "stderr": ""}

    return runner


def test_worker_provisioning_creates_templates_and_waits_for_ready_nodes(monkeypatch):
    monkeypatch.setenv("IRONIC_INTERNAL_NTP_IP", NTP_IP)
    calls = []
    result = execute_worker_provisioning(
        _payload(),
        runner=_success_runner(calls),
        sleep=lambda _seconds: None,
        monotonic=iter([0.0, 0.0, 0.0, 0.0, 0.0, 0.0]).__next__,
    )
    assert result["status"] == "succeeded"
    assert result["side_effects"] is True
    assert result["output"]["ready_node_names"] == ["worker-1"]
    assert result["output"]["worker_ntp_bootstrap_injected"] is True
    creates = [call for call in calls if call[0][:2] == ["create", "-f"]]
    applies = [call for call in calls if call[0][:1] == ["apply"]]
    assert len(creates) == 2
    assert len(applies) == 1
    bootstrap = json.loads(creates[0][1])
    commands = bootstrap["spec"]["template"]["spec"]["preKubeadmCommands"]
    assert "echo bootstrap" in commands
    assert any("10.130.60.84" in command for command in commands)


def test_worker_provisioning_rejects_external_ntp_before_any_mutation(monkeypatch):
    monkeypatch.setenv("IRONIC_INTERNAL_NTP_IP", NTP_IP)
    payload = _payload()
    payload["ironic_internal_ntp_ip"] = "1.1.1.1"
    calls = []
    with pytest.raises(WorkerProvisioningError, match="CONFIGURATION_MISMATCH"):
        execute_worker_provisioning(payload, runner=lambda *args, **kwargs: calls.append(args))
    assert calls == []


def test_worker_provisioning_rejects_machine_deployment_reference_mismatch(monkeypatch):
    monkeypatch.setenv("IRONIC_INTERNAL_NTP_IP", NTP_IP)
    payload = _payload()
    payload["resources"][2]["spec"]["template"]["spec"]["infrastructureRef"]["name"] = "wrong"
    calls = []
    with pytest.raises(WorkerProvisioningError, match="REFERENCE_MISMATCH"):
        execute_worker_provisioning(payload, runner=lambda *args, **kwargs: calls.append(args))
    assert calls == []


def test_worker_provisioning_rejects_non_matching_template_replacement(monkeypatch):
    monkeypatch.setenv("IRONIC_INTERNAL_NTP_IP", NTP_IP)
    calls = []

    def runner(arguments, input_text=None, timeout=30):
        calls.append(arguments)
        if arguments[0] == "get":
            return {
                "ok": True,
                "stdout": json.dumps({"spec": {"template": {"spec": {"preKubeadmCommands": []}}}}),
                "stderr": "",
            }
        return {"ok": True, "stdout": "", "stderr": ""}

    with pytest.raises(WorkerProvisioningError, match="IMMUTABLE_TEMPLATE_CONFLICT"):
        execute_worker_provisioning(_payload(), runner=runner)
    assert not any(call[0] == "apply" for call in calls)


def test_worker_provisioning_stops_before_mutation_when_command_lease_is_lost(monkeypatch):
    monkeypatch.setenv("IRONIC_INTERNAL_NTP_IP", NTP_IP)
    lost = Event()
    lost.set()
    calls = []
    with pytest.raises(WorkerProvisioningError, match="LEASE_LOST"):
        execute_worker_provisioning(
            _payload(),
            lease_lost_event=lost,
            runner=lambda *args, **kwargs: calls.append(args),
        )
    assert calls == []


def test_worker_provisioning_rejects_invalid_replica_count(monkeypatch):
    monkeypatch.setenv("IRONIC_INTERNAL_NTP_IP", NTP_IP)
    payload = _payload()
    payload["desired_worker_count"] = 0
    with pytest.raises(WorkerProvisioningError, match="DESIRED_COUNT_INVALID"):
        execute_worker_provisioning(payload)
