import json
from pathlib import Path
from threading import Event

import pytest

def _worker_provisioning():
    from apps.target_agent import worker_provisioning

    return worker_provisioning


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
    result = _worker_provisioning().execute_worker_provisioning(
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
    with pytest.raises(_worker_provisioning().WorkerProvisioningError, match="CONFIGURATION_MISMATCH"):
        _worker_provisioning().execute_worker_provisioning(payload, runner=lambda *args, **kwargs: calls.append(args))
    assert calls == []


def test_worker_provisioning_rejects_machine_deployment_reference_mismatch(monkeypatch):
    monkeypatch.setenv("IRONIC_INTERNAL_NTP_IP", NTP_IP)
    payload = _payload()
    payload["resources"][2]["spec"]["template"]["spec"]["infrastructureRef"]["name"] = "wrong"
    calls = []
    with pytest.raises(_worker_provisioning().WorkerProvisioningError, match="REFERENCE_MISMATCH"):
        _worker_provisioning().execute_worker_provisioning(payload, runner=lambda *args, **kwargs: calls.append(args))
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

    with pytest.raises(_worker_provisioning().WorkerProvisioningError, match="IMMUTABLE_TEMPLATE_CONFLICT"):
        _worker_provisioning().execute_worker_provisioning(_payload(), runner=runner)
    assert not any(call[0] == "apply" for call in calls)


def test_worker_provisioning_stops_before_mutation_when_command_lease_is_lost(monkeypatch):
    monkeypatch.setenv("IRONIC_INTERNAL_NTP_IP", NTP_IP)
    lost = Event()
    lost.set()
    calls = []
    with pytest.raises(_worker_provisioning().WorkerProvisioningError, match="LEASE_LOST"):
        _worker_provisioning().execute_worker_provisioning(
            _payload(),
            lease_lost_event=lost,
            runner=lambda *args, **kwargs: calls.append(args),
        )
    assert calls == []


def test_worker_provisioning_rejects_invalid_replica_count(monkeypatch):
    monkeypatch.setenv("IRONIC_INTERNAL_NTP_IP", NTP_IP)
    payload = _payload()
    payload["desired_worker_count"] = 0
    with pytest.raises(_worker_provisioning().WorkerProvisioningError, match="DESIRED_COUNT_INVALID"):
        _worker_provisioning().execute_worker_provisioning(payload)



def test_worker_provisioning_rejects_external_chrony_directives(monkeypatch):
    monkeypatch.setenv("IRONIC_INTERNAL_NTP_IP", NTP_IP)
    payload = _payload()
    payload["resources"][0]["spec"]["template"]["spec"]["preKubeadmCommands"] = [
        "cat > /etc/chrony/chrony.conf <<EOF\\npool pool.ntp.org iburst\\nEOF"
    ]
    calls = []
    with pytest.raises(_worker_provisioning().WorkerProvisioningError, match="NTP_DIRECTIVE_REJECTED"):
        _worker_provisioning().execute_worker_provisioning(payload, runner=lambda *args, **kwargs: calls.append(args))
    assert calls == []


def test_target_agent_dispatches_worker_provisioning(monkeypatch):
    from apps.target_agent import main as target_main

    monkeypatch.setattr(
        target_main,
        "execute_worker_provisioning",
        lambda payload, lease_lost_event=None: {
            "status": "succeeded",
            "error_code": None,
            "output": {"workers_ready": True, "desired_replicas": payload["desired_worker_count"]},
            "side_effects": True,
        },
    )
    result = target_main.execute({
        "action": "target.worker.provision",
        "payload": {"desired_worker_count": 4},
    })
    assert result["status"] == "succeeded"
    assert result["side_effects"] is True
    assert result["output"]["desired_replicas"] == 4
    assert "target.worker.provision" in target_main.CAPABILITIES


def test_target_agent_rbac_precreates_lease_and_grants_only_required_worker_writes():
    import yaml

    root = Path(__file__).resolve().parents[1]
    manifest = root / "deploy/agents/target-cluster-agent.yaml"
    documents = list(yaml.safe_load_all(manifest.read_text(encoding="utf-8")))
    lease = next(item for item in documents if item.get("kind") == "Lease")
    assert lease["metadata"]["name"] == "target-agent-leader"
    role = next(
        item for item in documents
        if item.get("kind") == "Role"
        and item["metadata"]["name"] == "target-agent-leader-election"
    )
    assert role["rules"][0]["verbs"] == ["get", "update"]
    worker_role = next(
        item for item in documents
        if item.get("kind") == "Role"
        and item["metadata"]["name"] == "target-agent-worker-provisioner"
    )
    assert worker_role["metadata"]["namespace"] == "REPLACE_WITH_CAPI_NAMESPACE"
    permissions = {
        (rule.get("apiGroups", [""])[0], resource): set(rule["verbs"])
        for rule in worker_role["rules"]
        for resource in rule["resources"]
    }
    assert {"get", "list", "watch", "create", "patch"} <= permissions[
        ("cluster.x-k8s.io", "machinedeployments")
    ]
    assert {"get", "create"} <= permissions[
        ("bootstrap.cluster.x-k8s.io", "kubeadmconfigtemplates")
    ]
    assert {"get", "create"} <= permissions[
        ("infrastructure.cluster.x-k8s.io", "metal3machinetemplates")
    ]
    worker_binding = next(
        item for item in documents
        if item.get("kind") == "RoleBinding"
        and item["metadata"]["name"] == "target-agent-worker-provisioner"
    )
    assert worker_binding["subjects"][0]["namespace"] == "ai-agent-system"



def _ready_observation_runner(ntp_command):
    from apps.target_agent.worker_provisioning import RESOURCE_TYPES

    deployment = {
        "metadata": {"generation": 3},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"cluster.x-k8s.io/deployment-name": "workers"}},
            "template": {
                "metadata": {"labels": {"cluster.x-k8s.io/deployment-name": "workers"}},
                "spec": {
                    "bootstrap": {
                        "configRef": {
                            "apiVersion": RESOURCE_TYPES["KubeadmConfigTemplate"][1],
                            "kind": "KubeadmConfigTemplate",
                            "name": "worker-bootstrap-v2",
                        }
                    }
                },
            },
        },
        "status": {
            "observedGeneration": 3,
            "updatedReplicas": 1,
            "readyReplicas": 1,
            "availableReplicas": 1,
        },
    }
    bootstrap = {
        "spec": {"template": {"spec": {"preKubeadmCommands": [ntp_command]}}},
    }

    def runner(arguments, input_text=None, timeout=30):
        if arguments[0] == "get":
            resource = arguments[1]
            if resource == "machinedeployments.cluster.x-k8s.io":
                return {"ok": True, "stdout": json.dumps(deployment), "stderr": ""}
            if resource == RESOURCE_TYPES["KubeadmConfigTemplate"][0]:
                return {"ok": True, "stdout": json.dumps(bootstrap), "stderr": ""}
            if resource == "machines.cluster.x-k8s.io":
                return {
                    "ok": True,
                    "stdout": json.dumps({"items": [{
                        "status": {
                            "bootstrapReady": True,
                            "nodeRef": {"name": "worker-1"},
                        },
                    }]}),
                    "stderr": "",
                }
            if resource == "nodes":
                return {
                    "ok": True,
                    "stdout": json.dumps({"items": [{
                        "metadata": {"name": "worker-1"},
                        "status": {
                            "conditions": [{"type": "Ready", "status": "True"}],
                        },
                    }]}),
                    "stderr": "",
                }
        return {"ok": False, "stderr": "NotFound"}

    return runner


def test_worker_readiness_observes_machine_bootstrap_node_and_internal_ntp(monkeypatch):
    from apps.target_agent.ntp_policy import render_worker_chrony_pre_kubeadm_command
    from apps.target_agent.worker_provisioning import observe_worker_readiness

    monkeypatch.setenv("IRONIC_INTERNAL_NTP_IP", NTP_IP)
    result = observe_worker_readiness(
        "metal3",
        "workers",
        NTP_IP,
        runner=_ready_observation_runner(render_worker_chrony_pre_kubeadm_command(NTP_IP)),
    )
    assert result["workers_ready"] is True
    assert result["ready_worker_count"] == 1
    assert result["worker_ntp_sources"] == {"worker-1": [NTP_IP]}
    assert result["worker_ntp_bootstrap_verified"] is True


def test_worker_readiness_rejects_missing_chrony_bootstrap(monkeypatch):
    from apps.target_agent.worker_provisioning import observe_worker_readiness

    monkeypatch.setenv("IRONIC_INTERNAL_NTP_IP", NTP_IP)
    with pytest.raises(
        _worker_provisioning().WorkerProvisioningError,
        match="INTERNAL_NTP_BOOTSTRAP_NOT_VERIFIED",
    ):
        observe_worker_readiness(
            "metal3",
            "workers",
            NTP_IP,
            runner=_ready_observation_runner("echo no chrony"),
        )


def test_handoff_workers_ready_requires_live_worker_observation(monkeypatch):
    from apps.target_agent import main as target_main

    monkeypatch.setenv("IRONIC_INTERNAL_NTP_IP", NTP_IP)
    monkeypatch.setattr(
        target_main,
        "observe_worker_readiness",
        lambda namespace, name, ntp_ip: {
            "desired_worker_count": 2,
            "ready_worker_count": 2,
            "worker_ntp_sources": {
                "worker-1": [ntp_ip],
                "worker-2": [ntp_ip],
            },
            "worker_ntp_bootstrap_verified": True,
            "workers_ready": True,
        },
    )
    result = target_main.execute({
        "action": "target.handoff.evaluate",
        "deployment_checkpoint": {
            "phase": "worker_provisioning",
            "sequence": 7,
            "state": {},
        },
        "payload": {
            "next_phase": "workers_ready",
            "namespace": "metal3",
            "machine_deployment_name": "workers",
            "evidence": {
                "ironic_internal_ntp_ip": NTP_IP,
            },
        },
    })
    assert result["status"] == "succeeded"
    assert result["output"]["evidence"]["ready_worker_count"] == 2
    assert result["output"]["evidence"]["worker_ntp_sources"]["worker-2"] == [NTP_IP]



def test_worker_provisioning_rejects_unconfigured_namespace_before_mutation(monkeypatch):
    monkeypatch.setenv("IRONIC_INTERNAL_NTP_IP", NTP_IP)
    monkeypatch.setenv("TARGET_CAPI_NAMESPACE", "ccd-sig1-capi")
    calls = []
    with pytest.raises(_worker_provisioning().WorkerProvisioningError, match="CAPI_NAMESPACE_NOT_ALLOWED"):
        _worker_provisioning().execute_worker_provisioning(
            _payload(),
            runner=lambda *args, **kwargs: calls.append(args),
        )
    assert calls == []
