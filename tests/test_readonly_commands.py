"""Read-only diagnostics are authorized for both Agents and must actually run.

``agent_commands`` lists these actions as dispatchable, so an unimplemented one
would surface as ``ACTION_NOT_IMPLEMENTED`` in the terminal. The contract module
is exercised here against a fake runner; the Agents only supply the runner.
"""

import json

import pytest
from agent_contracts.agent_commands import EPH_AGENT_COMMANDS, TARGET_AGENT_COMMANDS
from agent_contracts.readonly_commands import (
    READONLY_ACTIONS,
    ReadonlyCommandError,
    execute_readonly,
)

NODES = {
    "items": [
        {
            "metadata": {"name": "master-0"},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        },
        {
            "metadata": {"name": "worker-0"},
            "status": {"conditions": [{"type": "Ready", "status": "False"}]},
        },
    ]
}

PODS = {
    "items": [
        {
            "metadata": {"namespace": "kube-system", "name": "ready"},
            "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
        }
    ]
}


class FakeRunner:
    def __init__(self, responses=None, *, available=True, missing=()):
        self.calls = []
        self.responses = responses or {}
        self.available = available
        self.missing = tuple(missing)

    def __call__(self, argv, timeout=20):
        self.calls.append(list(argv))
        key = " ".join(argv)
        if any(key.startswith(prefix) for prefix in self.missing):
            return {"available": False, "returncode": 127, "stdout": "", "stderr": "not found"}
        payload = self.responses.get(key)
        if payload is None:
            payload = ""
        if isinstance(payload, (dict, list)):
            return {
                "available": True,
                "returncode": 0,
                "stdout": json.dumps(payload),
                "stderr": "",
            }
        return {
            "available": self.available,
            "returncode": 0 if self.available else None,
            "stdout": str(payload),
            "stderr": "" if self.available else "kubectl not found",
        }


def _base_responses():
    return {
        "kubectl get --raw /readyz": "ok",
        "kubectl get nodes -o json": NODES,
        "kubectl get pods --all-namespaces -o json": PODS,
        "kubectl get namespaces -o json": {"items": [{"metadata": {"name": "default"}}]},
    }


def test_every_readonly_action_is_authorized_for_both_agents():
    assert READONLY_ACTIONS <= EPH_AGENT_COMMANDS
    assert READONLY_ACTIONS <= TARGET_AGENT_COMMANDS


def test_resource_read_builds_the_exact_kubectl_arguments():
    runner = FakeRunner({"kubectl get pods app-0 --namespace team -o json": {"items": [1, 2]}})

    outcome = execute_readonly(
        "k8s.resource.read",
        {"resource": "pods", "namespace": "team", "name": "app-0"},
        runner,
    )

    assert outcome["status"] == "succeeded"
    assert outcome["output"]["item_count"] == 2
    assert runner.calls == [
        ["kubectl", "get", "pods", "app-0", "--namespace", "team", "-o", "json"]
    ]


def test_resource_read_rejects_a_resource_outside_the_allowlist():
    outcome = execute_readonly("k8s.resource.read", {"resource": "secrets"}, FakeRunner())

    assert outcome["status"] == "blocked"
    assert outcome["error_code"] == "RESOURCE_NOT_ALLOWED"


def test_resource_read_rejects_namespace_together_with_all_namespaces():
    outcome = execute_readonly(
        "k8s.resource.read",
        {"resource": "pods", "namespace": "team", "all_namespaces": True},
        FakeRunner(),
    )

    assert outcome["status"] == "blocked"
    assert outcome["error_code"] == "PAYLOAD_INVALID"


def test_resource_read_rejects_unknown_payload_keys():
    outcome = execute_readonly(
        "k8s.resource.read",
        {"resource": "pods", "kubeconfig": "/etc/shadow"},
        FakeRunner(),
    )

    assert outcome["error_code"] == "PAYLOAD_INVALID"


def test_a_missing_kubectl_is_reported_not_swallowed():
    outcome = execute_readonly(
        "cluster.health", {}, FakeRunner(_base_responses(), available=False)
    )

    assert outcome["status"] == "failed"
    assert outcome["error_code"] == "KUBECTL_UNAVAILABLE"
    assert outcome["side_effects"] is False


def test_cluster_health_summarizes_nodes():
    outcome = execute_readonly("cluster.health", {}, FakeRunner(_base_responses()))

    assert outcome["status"] == "succeeded"
    assert outcome["output"]["nodes"] == {
        "total": 2,
        "ready": 1,
        "not_ready": ["worker-0"],
    }
    assert outcome["output"]["healthy"] is False


def test_cluster_postflight_requires_every_node_and_pod():
    responses = _base_responses()
    responses["kubectl get pods --all-namespaces -o json"] = {
        "items": [
            {
                "metadata": {"namespace": "kube-system", "name": "pending"},
                "status": {"phase": "Pending", "conditions": []},
            }
        ]
    }

    outcome = execute_readonly("cluster.postflight", {}, FakeRunner(responses))

    assert outcome["output"]["pods"] == {"total": 1, "not_ready": 1}
    assert outcome["output"]["ok"] is False


def test_cluster_observe_reports_namespace_count():
    runner = FakeRunner(
        {
            **_base_responses(),
            "kubectl cluster-info": "Kubernetes control plane is running at https://10.0.0.1:6443\n",
        }
    )

    outcome = execute_readonly("cluster.observe", {}, runner)

    assert outcome["output"]["namespace_count"] == 1
    assert "control plane" in outcome["output"]["cluster_info"]


def test_cluster_diagnose_lists_pressure_and_warning_events():
    nodes = {
        "items": [
            {
                "metadata": {"name": "worker-0"},
                "status": {
                    "conditions": [
                        {"type": "Ready", "status": "True"},
                        {"type": "DiskPressure", "status": "True"},
                    ]
                },
            }
        ]
    }
    events = {
        "items": [
            {
                "metadata": {"namespace": "kube-system"},
                "reason": "BackOff",
                "involvedObject": {"name": "coredns"},
                "message": "Back-off restarting failed container",
            }
        ]
    }
    responses = {
        "kubectl get nodes -o json": nodes,
        "kubectl get events --all-namespaces --field-selector type=Warning -o json": events,
    }

    outcome = execute_readonly("cluster.diagnose", {}, FakeRunner(responses))

    assert outcome["output"]["node_pressure"] == [
        {"node": "worker-0", "condition": "DiskPressure"}
    ]
    assert outcome["output"]["warning_events"][0]["reason"] == "BackOff"


def test_network_inspection_summarizes_addresses_and_default_route():
    responses = {
        "ip -j addr show": [
            {
                "ifname": "eno1",
                "operstate": "UP",
                "addr_info": [{"local": "10.1.1.5", "prefixlen": 24}],
            }
        ],
        "ip -j route show": [
            {"dst": "default", "gateway": "10.1.1.1", "dev": "eno1"},
            {"dst": "10.2.0.0/24", "gateway": "10.1.1.2", "dev": "eno1"},
        ],
    }

    outcome = execute_readonly("cluster.network.inspect", {}, FakeRunner(responses))

    assert outcome["output"]["interfaces"] == [
        {"name": "eno1", "state": "UP", "addresses": ["10.1.1.5"]}
    ]
    assert outcome["output"]["default_routes"] == [{"gateway": "10.1.1.1", "dev": "eno1"}]


def test_network_inspection_without_ip_reports_unavailable():
    outcome = execute_readonly("cluster.network.inspect", {}, FakeRunner({}, available=False))

    assert outcome["status"] == "failed"
    assert outcome["error_code"] == "HOST_NETWORK_UNAVAILABLE"


def test_host_preflight_marks_missing_optional_tools():
    responses = {
        "kubectl version --client -o json": {"clientVersion": {"gitVersion": "v1.31.0"}},
        "hostname": "eph-node",
    }

    outcome = execute_readonly(
        "host.preflight",
        {},
        FakeRunner(responses, missing=("nmstatectl", "kubeadm")),
    )

    assert outcome["output"]["host"] == "eph-node"
    assert outcome["output"]["ok"] is True
    assert outcome["output"]["optional_missing"] == ["nmstatectl", "kubeadm"]


def test_non_readonly_actions_are_rejected_by_name():
    with pytest.raises(ReadonlyCommandError) as excinfo:
        execute_readonly("eph.controlstack.apply", {}, FakeRunner())

    assert excinfo.value.code == "ACTION_NOT_READONLY"


def test_readonly_actions_without_payload_reject_payload_keys():
    outcome = execute_readonly("cluster.health", {"fast": True}, FakeRunner(_base_responses()))

    assert outcome["status"] == "blocked"
    assert outcome["error_code"] == "PAYLOAD_INVALID"
