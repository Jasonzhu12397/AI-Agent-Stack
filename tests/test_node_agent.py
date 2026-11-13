# ruff: noqa: I001
import base64
import os
import datetime
import hashlib
import io
import json
import time

import pytest

from node_agent import agent, capabilities, capi_pivot

TEST_INFRA_API_VERSION = "infrastructure.cluster.x-k8s.io/v1beta1"


def test_node_health_is_available_for_both_agent_roles():
    for node_type in ["eph_node", "target_cluster"]:
        result = agent.execute_command(
            {"command_type": "node.health", "payload": {}},
            node_type,
        )
        assert result["agent_status"] == "ready"
        assert result["node_type"] == node_type


def test_target_agent_kubernetes_version_upgrade_is_simulation_only():
    result = agent.execute_command(
        {
            "command_type": "cluster.kubernetes.version.upgrade",
            "payload": {
                "cluster_name": "edge-lab",
                "current_version": "v1.34.2",
                "target_version": "v1.35",
                "execution_mode": "simulation",
            },
        },
        "target_cluster",
    )

    assert result["target_version"] == "v1.35"
    assert result["upgrade_simulated"] is True
    assert result["upgrade_performed"] is False
    assert result["side_effects"] is False
    assert result["requires_manual_execution"] is True
    assert result["plan_status"] == "simulation_plan_created"
    assert result["approval_required"] is True
    assert result["preflight_performed"] is False
    assert [step["name"] for step in result["upgrade_steps"]] == [
        "preflight",
        "control_plane",
        "workers",
        "postflight",
    ]
    assert all(step["status"] in {"not_run", "not_started"} for step in result["upgrade_steps"])
    assert result["resume_supported"] is False


def test_target_agent_can_upgrade_from_v133_to_v134_in_simulation():
    result = agent.execute_command(
        {
            "command_type": "cluster.kubernetes.version.upgrade",
            "payload": {
                "cluster_name": "edge-lab",
                "current_version": "v1.33.2",
                "target_version": "v1.34",
                "execution_mode": "simulation",
            },
        },
        "target_cluster",
    )

    assert result["target_version"] == "v1.34"
    assert result["upgrade_simulated"] is True
    assert result["upgrade_performed"] is False
    assert result["side_effects"] is False


def test_target_agent_rejects_unsupported_kubernetes_version():
    with pytest.raises(agent.NodeAgentError, match="KUBERNETES_VERSION_UPGRADE_REQUEST_INVALID"):
        agent.execute_command(
            {
                "command_type": "cluster.kubernetes.version.upgrade",
                "payload": {
                    "cluster_name": "edge-lab",
                    "current_version": "v1.32.2",
                    "target_version": "v1.37",
                    "execution_mode": "simulation",
                },
            },
            "target_cluster",
        )


def test_target_agent_rejects_non_string_kubernetes_version():
    with pytest.raises(agent.NodeAgentError, match="KUBERNETES_VERSION_UPGRADE_REQUEST_INVALID"):
        agent.execute_command(
            {
                "command_type": "cluster.kubernetes.version.upgrade",
                "payload": {
                    "cluster_name": "edge-lab",
                    "current_version": "v1.34.2",
                    "target_version": [],
                    "execution_mode": "simulation",
                },
            },
            "target_cluster",
        )


def test_target_agent_rejects_skipping_kubernetes_minor_versions():
    with pytest.raises(agent.NodeAgentError, match="KUBERNETES_VERSION_UPGRADE_REQUEST_INVALID"):
        agent.execute_command(
            {
                "command_type": "cluster.kubernetes.version.upgrade",
                "payload": {
                    "cluster_name": "edge-lab",
                    "current_version": "v1.34.2",
                    "target_version": "v1.36",
                    "execution_mode": "simulation",
                },
            },
            "target_cluster",
        )


def test_eph_agent_cannot_execute_kubernetes_version_upgrade():
    with pytest.raises(agent.NodeAgentError, match="CAPABILITY_NOT_CONFIGURED"):
        agent.execute_command(
            {
                "command_type": "cluster.kubernetes.version.upgrade",
                "payload": {
                    "cluster_name": "edge-lab",
                    "current_version": "v1.32.2",
                    "target_version": "v1.35",
                    "execution_mode": "simulation",
                },
            },
            "eph_node",
        )


def test_eph_host_preflight_reports_environment_without_running_commands():
    result = agent.execute_command(
        {"command_type": "host.preflight", "payload": {}},
        "eph_node",
    )

    assert result["hostname"]
    assert result["cpu_count"] >= 1
    assert result["root_disk_total_bytes"] > 0
    assert set(result["container_runtime"]) == {"docker", "containerd", "crictl"}


def test_cluster_plan_validates_spec_and_has_no_side_effects():
    spec = {
        "cluster_name": "edge-lab",
        "kubernetes_version": "v1.32.2",
        "control_plane_replicas": 3,
        "worker_replicas": 5,
    }

    result = agent.execute_command(
        {"command_type": "cluster.plan", "payload": {"cluster_spec": spec}},
        "eph_node",
    )

    assert result["plan_status"] == "validated_dry_run"
    assert result["side_effects"] is False
    assert result["cluster_name"] == "edge-lab"
    assert len(result["plan_hash"]) == 64


@pytest.mark.parametrize(
    "spec",
    [
        {},
        {
            "cluster_name": "edge-lab",
            "kubernetes_version": "latest",
            "control_plane_replicas": 3,
            "worker_replicas": 2,
        },
        {
            "cluster_name": "edge-lab",
            "kubernetes_version": "v1.32.2",
            "control_plane_replicas": 0,
            "worker_replicas": 2,
        },
        {
            "cluster_name": "edge-lab",
            "kubernetes_version": "v1.32.2",
            "control_plane_replicas": 3,
            "worker_replicas": -1,
        },
    ],
)
def test_cluster_plan_rejects_invalid_specs(spec):
    with pytest.raises(agent.NodeAgentError):
        agent.execute_command(
            {"command_type": "cluster.plan", "payload": {"cluster_spec": spec}},
            "eph_node",
        )


def test_node_agent_never_executes_cluster_mutations_without_an_adapter():
    with pytest.raises(agent.NodeAgentError, match="CAPABILITY_NOT_CONFIGURED"):
        agent.execute_command(
            {"command_type": "cluster.provision", "payload": {"cluster_spec": {}}},
            "eph_node",
        )
    with pytest.raises(agent.NodeAgentError, match="CAPABILITY_NOT_CONFIGURED"):
        agent.execute_command(
            {"command_type": "target-agent.bootstrap", "payload": {}},
            "eph_node",
        )


def test_target_agent_reads_cluster_health_through_scoped_api_client(monkeypatch):
    def fake_get(path):
        assert path == "/version"
        return {"gitVersion": "v1.32.2"}

    monkeypatch.setattr(agent, "kubernetes_api_get", fake_get)
    result = agent.execute_command(
        {"command_type": "cluster.health", "payload": {}},
        "target_cluster",
    )

    assert result == {"api_reachable": True, "kubernetes_version": "v1.32.2"}


def test_target_agent_postflight_detects_not_ready_nodes(monkeypatch):
    def fake_get(path):
        assert path == "/api/v1/nodes"
        return {
            "items": [
                {
                    "metadata": {"name": "worker-01"},
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                },
                {
                    "metadata": {"name": "worker-02"},
                    "status": {"conditions": [{"type": "Ready", "status": "False"}]},
                },
            ]
        }

    monkeypatch.setattr(agent, "kubernetes_api_get", fake_get)
    result = agent.execute_command(
        {"command_type": "cluster.postflight", "payload": {}},
        "target_cluster",
    )

    assert result["node_count"] == 2
    assert result["ready"] is False
    assert result["not_ready_nodes"] == ["worker-02"]


def test_target_postflight_requires_capi_worker_machines_and_ready_nodes(monkeypatch):
    node_items = [
        {
            "metadata": {"name": "control-plane-01"},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        },
        {
            "metadata": {"name": "worker-01"},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        },
        {
            "metadata": {"name": "worker-02"},
            "status": {"conditions": [{"type": "Ready", "status": "False"}]},
        },
    ]
    machine_items = [
        {
            "metadata": {
                "name": "edge-lab-control-plane",
                "labels": {"cluster.x-k8s.io/control-plane": ""},
            },
            "spec": {"clusterName": "edge-lab"},
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "nodeRef": {"name": "control-plane-01"},
            },
        },
        {
            "metadata": {"name": "edge-lab-worker-01", "labels": {}},
            "spec": {"clusterName": "edge-lab"},
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "nodeRef": {"name": "worker-01"},
            },
        },
        {
            "metadata": {"name": "edge-lab-worker-02", "labels": {}},
            "spec": {"clusterName": "edge-lab"},
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "nodeRef": {"name": "worker-02"},
            },
        },
        {
            "metadata": {"name": "other-cluster-worker", "labels": {}},
            "spec": {"clusterName": "other-cluster"},
            "status": {
                "conditions": [{"type": "Ready", "status": "True"}],
                "nodeRef": {"name": "other-worker"},
            },
        },
    ]

    def fake_get(path):
        if path == "/api/v1/nodes":
            return {"items": node_items}
        if path == "/apis":
            return {
                "groups": [
                    {
                        "name": "cluster.x-k8s.io",
                        "preferredVersion": {"version": "v1beta1"},
                    }
                ]
            }
        raise AssertionError(path)

    def fake_list(path, max_items=100000):
        assert path == (
            "/apis/cluster.x-k8s.io/v1beta1/namespaces/"
            "metal3-system/machines"
        )
        return machine_items

    monkeypatch.setattr(agent, "kubernetes_api_get", fake_get)
    monkeypatch.setattr(agent, "list_all_kubernetes_items", fake_list)
    command = {
        "command_type": "cluster.postflight",
        "payload": {
            "cluster_name": "edge-lab",
            "namespace": "metal3-system",
            "expected_worker_replicas": 2,
        },
    }

    result = agent.execute_command(command, "target_cluster")

    assert result["worker_machine_count"] == 2
    assert result["ready_worker_machine_count"] == 1
    assert result["not_ready_worker_machine_names"] == ["edge-lab-worker-02"]
    assert result["worker_node_names"] == ["worker-01"]
    assert result["capi_worker_inventory_complete"] is False

    node_items[2]["status"]["conditions"][0]["status"] = "True"
    result = agent.execute_command(command, "target_cluster")

    assert result["worker_machine_count"] == 2
    assert result["ready_worker_machine_count"] == 2
    assert result["not_ready_worker_machine_names"] == []
    assert result["worker_node_names"] == ["worker-01", "worker-02"]
    assert result["capi_worker_inventory_complete"] is True
    assert result["side_effects"] is False



def test_target_postflight_requires_machine_deployment_rollout_convergence(monkeypatch):
    deployment = {
        "metadata": {"name": "edge-workers", "uid": "md-uid-1", "generation": 4},
        "spec": {"replicas": 1},
        "status": {
            "observedGeneration": 4,
            "updatedReplicas": 1,
            "readyReplicas": 1,
            "availableReplicas": 1,
            "unavailableReplicas": 0,
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }

    def fake_get(path):
        if path == "/api/v1/nodes":
            return {
                "items": [
                    {
                        "metadata": {"name": "worker-01"},
                        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    }
                ]
            }
        if path == "/apis":
            return {
                "groups": [
                    {
                        "name": "cluster.x-k8s.io",
                        "preferredVersion": {"version": "v1beta1"},
                    }
                ]
            }
        if path.endswith("/machinedeployments/edge-workers"):
            return deployment
        raise AssertionError(path)

    def fake_list(path, max_items=100000):
        assert path.endswith("/namespaces/metal3-system/machines")
        return [
            {
                "metadata": {
                    "name": "edge-workers-abc",
                    "labels": {"cluster.x-k8s.io/deployment-name": "edge-workers"},
                },
                "spec": {"clusterName": "edge-lab"},
                "status": {
                    "conditions": [{"type": "Ready", "status": "True"}],
                    "nodeRef": {"name": "worker-01"},
                },
            }
        ]

    monkeypatch.setattr(agent, "kubernetes_api_get", fake_get)
    monkeypatch.setattr(agent, "list_all_kubernetes_items", fake_list)
    command = {
        "command_type": "cluster.postflight",
        "payload": {
            "cluster_name": "edge-lab",
            "namespace": "metal3-system",
            "expected_worker_replicas": 1,
            "machine_deployment_name": "edge-workers",
        },
    }

    result = agent.execute_command(command, "target_cluster")

    assert result["machine_deployment_status"] == {
        "requested": True,
        "found": True,
        "uid": "md-uid-1",
        "generation": 4,
        "observed_generation": 4,
        "desired_replicas": 1,
        "updated_replicas": 1,
        "ready_replicas": 1,
        "available_replicas": 1,
        "unavailable_replicas": 0,
        "rollout_complete": True,
    }
    assert result["capi_worker_inventory_complete"] is True
    assert result["side_effects"] is False

    deployment["status"]["observedGeneration"] = 3
    result = agent.execute_command(command, "target_cluster")

    assert result["machine_deployment_status"]["rollout_complete"] is False
    assert result["capi_worker_inventory_complete"] is False



def test_kubernetes_resource_read_rejects_non_allowlisted_resources(monkeypatch):
    monkeypatch.setattr(
        agent,
        "kubernetes_api_get",
        lambda path: {"items": []},
    )
    with pytest.raises(agent.NodeAgentError, match="RESOURCE_NOT_ALLOWLISTED"):
        agent.execute_command(
            {"command_type": "k8s.resource.read", "payload": {"resource": "secrets"}},
            "target_cluster",
        )


def test_kubernetes_api_client_requires_in_cluster_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("AGENT_SERVICE_ACCOUNT_TOKEN_FILE", str(tmp_path / "missing-token"))
    monkeypatch.setenv("AGENT_SERVICE_ACCOUNT_CA_FILE", str(tmp_path / "missing-ca"))

    with pytest.raises(agent.NodeAgentError, match="KUBERNETES_IN_CLUSTER_IDENTITY_UNAVAILABLE"):
        agent.kubernetes_api_get("/version")


def test_kubernetes_resource_summaries_omit_pod_environment_and_redact_event_secrets():
    pod = {
        "metadata": {"name": "api-0", "namespace": "platform"},
        "spec": {
            "nodeName": "worker-01",
            "containers": [
                {
                    "env": [
                        {"name": "API_TOKEN", "value": "do-not-return-this"},
                    ]
                }
            ],
        },
        "status": {"phase": "Running"},
    }
    event = {
        "metadata": {"name": "warning-01", "namespace": "platform"},
        "type": "Warning",
        "reason": "FailedMount",
        "message": "token=do-not-return-this volume mount failed",
        "involvedObject": {"kind": "Pod", "name": "api-0"},
    }

    pod_summary = agent.sanitize_kubernetes_item("pods", pod)
    event_summary = agent.sanitize_kubernetes_item("events", event)

    assert pod_summary == {
        "name": "api-0",
        "namespace": "platform",
        "phase": "Running",
        "node_name": "worker-01",
    }
    assert "do-not-return-this" not in str(pod_summary)
    assert "do-not-return-this" not in event_summary["message"]
    assert "[REDACTED]" in event_summary["message"]


def test_agent_health_files_distinguish_process_liveness_from_control_plane_readiness(
    monkeypatch,
    tmp_path,
):
    liveness = tmp_path / "agent-live"
    readiness = tmp_path / "agent-ready"
    monkeypatch.setenv("AGENT_LIVENESS_FILE", str(liveness))
    monkeypatch.setenv("AGENT_READINESS_FILE", str(readiness))

    agent.touch_health_files()
    assert agent.health_file_is_fresh("AGENT_LIVENESS_FILE")
    assert not agent.health_file_is_fresh("AGENT_READINESS_FILE")

    agent.touch_health_files(readiness=True)
    assert agent.health_file_is_fresh("AGENT_LIVENESS_FILE")
    assert agent.health_file_is_fresh("AGENT_READINESS_FILE")


def test_agent_health_files_reject_implausible_future_timestamps(
    monkeypatch,
    tmp_path,
):
    health_file = tmp_path / "agent-live"
    health_file.touch()
    future_timestamp = time.time() + 60
    os.utime(health_file, (future_timestamp, future_timestamp))
    monkeypatch.setenv("AGENT_LIVENESS_FILE", str(health_file))

    assert not agent.health_file_is_fresh("AGENT_LIVENESS_FILE")


def test_metal3_inspection_discovers_version_and_returns_redacted_bmh_summary(monkeypatch):
    def fake_get(path):
        if path == "/apis":
            return {
                "groups": [
                    {
                        "name": "metal3.io",
                        "preferredVersion": {"version": "v1alpha1"},
                    }
                ]
            }
        if path == "/apis/metal3.io/v1alpha1":
            return {
                "resources": [
                    {"name": "baremetalhosts", "namespaced": True},
                ]
            }
        if path == "/apis/metal3.io/v1alpha1/namespaces/metal3-system/baremetalhosts":
            return {
                "items": [
                    {
                        "metadata": {"name": "bmh-01", "namespace": "metal3-system"},
                        "spec": {
                            "online": True,
                            "bmc": {
                                "address": "redfish://bmc-01",
                                "credentialsName": "bmc-secret",
                            },
                        },
                        "status": {
                            "operationalStatus": "OK",
                            "provisioning": {"state": "provisioned"},
                            "poweredOn": True,
                            "errorMessage": "",
                        },
                    }
                ]
            }
        raise AssertionError(path)

    monkeypatch.setattr(agent, "kubernetes_api_get", fake_get)
    result = agent.execute_command(
        {
            "command_type": "metal3.host.inspect",
            "payload": {"namespace": "metal3-system"},
        },
        "eph_node",
    )

    host = result["resources"][0]["items"][0]
    assert result["version"] == "v1alpha1"
    assert host["name"] == "bmh-01"
    assert host["provisioning_state"] == "provisioned"
    assert "bmc" not in host
    assert "credentialsName" not in str(host)


def test_capi_observation_uses_discovered_group_version(monkeypatch):
    def fake_get(path):
        if path == "/apis":
            return {
                "groups": [
                    {
                        "name": "cluster.x-k8s.io",
                        "preferredVersion": {"version": "v1beta1"},
                    }
                ]
            }
        if path == "/apis/cluster.x-k8s.io/v1beta1":
            return {
                "resources": [
                    {"name": "clusters", "namespaced": True},
                    {"name": "machines", "namespaced": True},
                    {"name": "machinedeployments", "namespaced": True},
                ]
            }
        if path.endswith("/clusters"):
            return {
                "items": [
                    {
                        "metadata": {"name": "edge-lab", "namespace": "capi-system"},
                        "status": {"phase": "Provisioned"},
                    }
                ]
            }
        if path.endswith("/machines") or path.endswith("/machinedeployments"):
            return {"items": []}
        raise AssertionError(path)

    monkeypatch.setattr(agent, "kubernetes_api_get", fake_get)
    result = agent.execute_command(
        {"command_type": "capi.cluster.observe", "payload": {"namespace": "capi-system"}},
        "eph_node",
    )

    assert result["discovered"] is True
    assert result["version"] == "v1beta1"
    assert result["resources"][0]["items"][0]["name"] == "edge-lab"



def test_provider_discovery_reports_live_api_versions_and_missing_resources(monkeypatch):
    calls = []

    def fake_get(path):
        calls.append(path)
        if path == "/apis":
            return {
                "groups": [
                    {
                        "name": "cluster.x-k8s.io",
                        "preferredVersion": {"version": "v1beta1"},
                    },
                    {
                        "name": "bootstrap.cluster.x-k8s.io",
                        "preferredVersion": {"version": "v1beta1"},
                    },
                    {
                        "name": "controlplane.cluster.x-k8s.io",
                        "preferredVersion": {"version": "v1beta1"},
                    },
                    {
                        "name": "infrastructure.cluster.x-k8s.io",
                        "preferredVersion": {"version": "v1beta1"},
                    },
                    {
                        "name": "metal3.io",
                        "preferredVersion": {"version": "v1alpha1"},
                    },
                ]
            }
        if path == "/version":
            return {"gitVersion": "v1.32.2"}
        if path.startswith("/apis/apiextensions.k8s.io/"):
            crd_name = path.rsplit("/", 1)[-1]
            group = crd_name.split(".", 1)[1]
            return {
                "spec": {
                    "group": group,
                    "names": {
                        "plural": crd_name.split(".", 1)[0],
                        "kind": "Fixture",
                    },
                    "scope": "Namespaced",
                    "versions": [
                        {
                            "name": "v1beta1",
                            "served": True,
                            "storage": True,
                            "schema": {
                                "openAPIV3Schema": {
                                    "properties": {
                                        "spec": {"type": "object"}
                                    }
                                }
                            },
                        }
                    ],
                }
            }
        if path.endswith("/cluster.x-k8s.io/v1beta1"):
            return {
                "resources": [
                    {"name": name}
                    for name in ["clusters", "machines", "machinedeployments"]
                ]
            }
        if path.endswith("/bootstrap.cluster.x-k8s.io/v1beta1"):
            return {"resources": [{"name": "kubeadmconfigs"}]}
        if path.endswith("/controlplane.cluster.x-k8s.io/v1beta1"):
            return {"resources": [{"name": "kubeadmcontrolplanes"}]}
        if path.endswith("/infrastructure.cluster.x-k8s.io/v1beta1"):
            return {"resources": [{"name": name} for name in ["metal3clusters", "metal3machines"]]}
        if path.endswith("/metal3.io/v1alpha1"):
            return {"resources": [{"name": "baremetalhosts"}]}
        raise AssertionError(path)

    monkeypatch.setattr(agent, "kubernetes_api_get", fake_get)
    result = agent.execute_command(
        {"command_type": "cluster.provider.discover", "payload": {}},
        "eph_node",
    )

    assert result["management_kubernetes_version"] == "v1.32.2"
    assert result["providers"]["cluster_api"]["version"] == "v1beta1"
    assert result["providers"]["metal3"]["version"] == "v1alpha1"
    assert result["discovery_complete"] is False
    assert result["missing_requirements"] == [
        "provider_api.infrastructure_metal3.metal3machinetemplates"
    ]
    assert result["external_components"]["ironic"] == "not_verified_by_kubernetes_api_discovery"
    assert result["mutation_adapter_enabled"] is False
    assert result["provisioning_ready"] is False
    assert "/apis/infrastructure.cluster.x-k8s.io/v1beta1" in calls


def test_provider_discovery_inspects_crd_openapi_schemas(monkeypatch):
    groups = {
        "cluster.x-k8s.io": (
            "v1beta1",
            ["clusters", "machines", "machinedeployments"],
        ),
        "bootstrap.cluster.x-k8s.io": ("v1beta1", ["kubeadmconfigs"]),
        "controlplane.cluster.x-k8s.io": ("v1beta1", ["kubeadmcontrolplanes"]),
        "infrastructure.cluster.x-k8s.io": (
            "v1beta1",
            [
                "metal3clusters",
                "metal3machines",
                "metal3machinetemplates",
                "metal3datatemplates",
                "metal3datas",
            ],
        ),
        "metal3.io": ("v1alpha1", ["baremetalhosts"]),
    }

    def fake_get(path):
        if path == "/apis":
            return {
                "groups": [
                    {
                        "name": name,
                        "preferredVersion": {"version": data[0]},
                    }
                    for name, data in groups.items()
                ]
            }
        if path == "/version":
            return {"gitVersion": "v1.32.2"}
        if path.startswith("/apis/apiextensions.k8s.io/"):
            crd_name = path.rsplit("/", 1)[-1]
            group = crd_name.split(".", 1)[1]
            return {
                "spec": {
                    "group": group,
                    "names": {
                        "plural": crd_name.split(".", 1)[0],
                        "kind": "Fixture",
                    },
                    "scope": "Namespaced",
                    "versions": [
                        {
                            "name": "v1beta1",
                            "served": True,
                            "storage": True,
                            "schema": {
                                "openAPIV3Schema": {
                                    "properties": {
                                        "spec": {
                                            "properties": {
                                                "clusterName": {"type": "string"}
                                            },
                                            "required": ["clusterName"],
                                        }
                                    }
                                }
                            },
                        }
                    ],
                }
            }
        for name, data in groups.items():
            expected_path = "/apis/" + name + "/" + data[0]
            if path == expected_path:
                return {
                    "resources": [
                        {"name": resource} for resource in data[1]
                    ]
                }
        raise AssertionError(path)

    monkeypatch.setattr(agent, "kubernetes_api_get", fake_get)
    result = agent.discover_provider_apis()
    inspection = result["crd_schema_inspection"]
    assert inspection["schema_check_complete"] is True
    assert inspection["invalid_schemas"] == []
    crd = inspection["crds"]["clusters.cluster.x-k8s.io"]
    assert crd["schema_valid"] is True
    assert crd["served_versions"][0]["required_spec_fields"] == ["clusterName"]
    assert result["provisioning_ready"] is False
    assert result["mutation_adapter_enabled"] is False


def test_provider_crd_schema_rejects_missing_openapi_schema():
    result = agent.summarize_crd_schema(
        {
            "spec": {
                "group": "metal3.io",
                "names": {
                    "plural": "baremetalhosts",
                    "kind": "BareMetalHost",
                },
                "versions": [
                    {
                        "name": "v1alpha1",
                        "served": True,
                        "storage": True,
                    }
                ],
            }
        }
    )
    assert result["schema_valid"] is False
    assert "no_served_version_with_openapi_schema" in result["schema_errors"]



def _cluster_crd_fixture(version="v1beta1"):
    return {
        "spec": {
            "group": "cluster.x-k8s.io",
            "names": {"plural": "clusters", "kind": "Cluster"},
            "scope": "Namespaced",
            "versions": [
                {
                    "name": version,
                    "served": True,
                    "storage": True,
                    "schema": {
                        "openAPIV3Schema": {
                            "type": "object",
                            "required": ["apiVersion", "kind", "metadata", "spec"],
                            "properties": {
                                "apiVersion": {"type": "string"},
                                "kind": {"type": "string", "enum": ["Cluster"]},
                                "metadata": {
                                    "type": "object",
                                    "required": ["name", "namespace"],
                                    "properties": {
                                        "name": {"type": "string", "minLength": 1},
                                        "namespace": {"type": "string", "minLength": 1},
                                    },
                                },
                                "spec": {
                                    "type": "object",
                                    "required": ["clusterNetwork"],
                                    "properties": {
                                        "clusterNetwork": {
                                            "type": "object",
                                            "required": ["pods"],
                                            "properties": {
                                                "pods": {
                                                    "type": "object",
                                                    "required": ["cidrBlocks"],
                                                    "properties": {
                                                        "cidrBlocks": {
                                                            "type": "array",
                                                            "minItems": 1,
                                                            "items": {"type": "string"},
                                                        }
                                                    },
                                                }
                                            },
                                        },
                                        "controlPlaneEndpoint": {"type": "string"},
                                    },
                                },
                            },
                        }
                    },
                }
            ],
        }
    }


def test_manifest_validation_checks_nested_required_fields_and_arrays(monkeypatch):
    def fake_get(path):
        if path.endswith("/clusters.cluster.x-k8s.io"):
            return _cluster_crd_fixture()
        raise agent.NodeAgentError("KUBERNETES_API_HTTP_404")

    monkeypatch.setattr(agent, "kubernetes_api_get", fake_get)
    manifest = {
        "apiVersion": "cluster.x-k8s.io/v1beta1",
        "kind": "Cluster",
        "metadata": {"name": "edge-lab", "namespace": "clusters"},
        "spec": {
            "clusterNetwork": {
                "pods": {"cidrBlocks": ["10.244.0.0/16"]}
            }
        },
    }
    result = agent.validate_provider_manifests([manifest])
    assert result["valid"] is True
    assert result["validated_count"] == 1
    assert result["server_side_dry_run"] is False
    assert result["mutation_adapter_enabled"] is False


def test_manifest_validation_rejects_missing_nested_required_and_wrong_array_type(monkeypatch):
    monkeypatch.setattr(
        agent,
        "kubernetes_api_get",
        lambda path: (
            _cluster_crd_fixture()
            if path.endswith("/clusters.cluster.x-k8s.io")
            else (_ for _ in ()).throw(agent.NodeAgentError("KUBERNETES_API_HTTP_404"))
        ),
    )
    manifest = {
        "apiVersion": "cluster.x-k8s.io/v1beta1",
        "kind": "Cluster",
        "metadata": {"name": "edge-lab", "namespace": "clusters"},
        "spec": {"clusterNetwork": {"pods": {"cidrBlocks": "10.244.0.0/16"}}},
    }
    result = agent.validate_provider_manifests([manifest])
    assert result["valid"] is False
    errors = result["results"][0]["errors"]
    assert "manifests[0].spec.clusterNetwork.pods.cidrBlocks:type_mismatch" in errors


def test_manifest_validation_rejects_unsupported_api_version(monkeypatch):
    monkeypatch.setattr(
        agent,
        "kubernetes_api_get",
        lambda path: (
            _cluster_crd_fixture()
            if path.endswith("/clusters.cluster.x-k8s.io")
            else (_ for _ in ()).throw(agent.NodeAgentError("KUBERNETES_API_HTTP_404"))
        ),
    )
    manifest = {
        "apiVersion": "cluster.x-k8s.io/v1alpha9",
        "kind": "Cluster",
        "metadata": {"name": "edge-lab", "namespace": "clusters"},
        "spec": {},
    }
    result = agent.validate_provider_manifests([manifest])
    assert result["valid"] is False
    assert result["results"][0]["errors"] == ["unsupported_api_version"]


def test_manifest_validation_reports_missing_crd(monkeypatch):
    monkeypatch.setattr(
        agent,
        "kubernetes_api_get",
        lambda path: (_ for _ in ()).throw(agent.NodeAgentError("KUBERNETES_API_HTTP_404")),
    )
    result = agent.validate_provider_manifests(
        [{
            "apiVersion": "cluster.x-k8s.io/v1beta1",
            "kind": "Cluster",
            "metadata": {"name": "edge-lab", "namespace": "clusters"},
            "spec": {},
        }]
    )
    assert result["valid"] is False
    assert result["results"][0]["errors"] == ["crd_not_found_for_group_and_kind"]


def test_manifest_validation_command_is_eph_only(monkeypatch):
    with pytest.raises(agent.NodeAgentError, match="CAPABILITY_NOT_CONFIGURED"):
        agent.execute_command(
            {
                "command_type": "cluster.manifest.validate",
                "payload": {"manifests": []},
            },
            "target_cluster",
        )

def test_provider_discovery_rejects_non_eph_agent():
    with pytest.raises(agent.NodeAgentError, match="CAPABILITY_NOT_CONFIGURED"):
        agent.execute_command(
            {"command_type": "cluster.provider.discover", "payload": {}},
            "target_cluster",
        )


def test_provider_discovery_reports_absent_provider_groups(monkeypatch):
    monkeypatch.setattr(
        agent,
        "kubernetes_api_get",
        lambda path: {"groups": []} if path == "/apis" else {"gitVersion": "v1.32.2"},
    )
    result = agent.discover_provider_apis()
    assert result["discovery_complete"] is False
    assert len(result["missing_requirements"]) == 5
    assert result["mutation_adapter_enabled"] is False


def test_cluster_plan_requires_odd_control_plane_quorum():
    spec = {
        "cluster_name": "edge-lab",
        "kubernetes_version": "v1.32.2",
        "control_plane_replicas": 2,
        "worker_replicas": 2,
    }

    with pytest.raises(agent.NodeAgentError, match="INVALID_CONTROL_PLANE_REPLICAS"):
        agent.execute_command(
            {"command_type": "cluster.plan", "payload": {"cluster_spec": spec}},
            "eph_node",
        )


def test_cluster_plan_never_accepts_plaintext_bmc_credentials():
    spec = {
        "cluster_name": "edge-lab",
        "kubernetes_version": "v1.32.2",
        "control_plane_replicas": 3,
        "worker_replicas": 2,
        "metal3": {"bmc_password": "do-not-store"},
    }

    with pytest.raises(agent.NodeAgentError, match="FORBIDDEN_PAYLOAD_FIELD"):
        agent.execute_command(
            {"command_type": "cluster.plan", "payload": {"cluster_spec": spec}},
            "eph_node",
        )


def test_complete_cluster_plan_reports_missing_adapter_separately():
    hosts = [
        {
            "baremetal_host": f"bmh-{index:02d}",
            "role": "control-plane" if index < 3 else "worker",
            "bmc_credential_ref": f"secret-ref-{index:02d}",
            "boot_mac": f"02:00:00:00:00:{index:02d}",
            "root_device_hints": {"deviceName": "/dev/sda"},
            "network_data_ref": f"network-template-{index:02d}",
        }
        for index in range(4)
    ]
    spec = {
        "cluster_name": "edge-lab",
        "kubernetes_version": "v1.32.2",
        "control_plane_replicas": 3,
        "worker_replicas": 1,
        "provider_versions": {
            "cluster_api": "verified-version",
            "bootstrap_kubeadm": "verified-version",
            "control_plane_kubeadm": "verified-version",
            "infrastructure_metal3": "verified-version",
            "metal3": "verified-version",
            "ironic": "verified-version",
            "ipa": "verified-version",
        },
        "network": {
            "pod_cidr": "10.244.0.0/16",
            "service_cidr": "10.96.0.0/12",
            "control_plane_endpoint": "192.0.2.10:6443",
        },
        "machine_image": {
            "url": "https://images.example.invalid/worker.qcow2",
            "checksum": "sha256:example",
        },
        "metal3": {"namespace": "metal3-system", "hosts": hosts},
    }

    result = agent.execute_command(
        {"command_type": "cluster.plan", "payload": {"cluster_spec": spec}},
        "eph_node",
    )

    assert result["provisioning_ready"] is True
    assert result["missing_requirements"] == []
    assert result["adapter_enabled"] is False
    assert result["side_effects"] is False



@pytest.mark.parametrize(
    "network",
    [
        {"pod_cidr": "10.244.0.0/16"},
        {"service_cidr": "not-a-cidr"},
    ],
)
def test_cluster_plan_rejects_invalid_network_cidrs(network):
    with pytest.raises(agent.NodeAgentError, match="INVALID_NETWORK_CIDR"):
        agent.validate_network_inputs(network)


@pytest.mark.parametrize(
    "endpoint",
    ["192.0.2.10", "192.0.2.10:0", "192.0.2.10:65536", ":6443"],
)
def test_cluster_plan_rejects_invalid_control_plane_endpoints(endpoint):
    with pytest.raises(agent.NodeAgentError, match="INVALID_CONTROL_PLANE_ENDPOINT"):
        agent.validate_network_inputs({"control_plane_endpoint": endpoint})


def test_cluster_plan_rejects_duplicate_baremetal_host_references():
    hosts = [
        {"baremetal_host": "metal3-system/bmh-01"},
        {"baremetal_host": "metal3-system/bmh-01"},
    ]

    with pytest.raises(agent.NodeAgentError, match="DUPLICATE_BAREMETAL_HOST"):
        agent.validate_metal3_hosts(hosts)


def test_cluster_plan_rejects_invalid_or_duplicate_boot_macs():
    with pytest.raises(agent.NodeAgentError, match="INVALID_BOOT_MAC"):
        agent.validate_metal3_hosts([{"boot_mac": "not-a-mac"}])

    with pytest.raises(agent.NodeAgentError, match="DUPLICATE_BOOT_MAC"):
        agent.validate_metal3_hosts(
            [
                {"boot_mac": "02:00:00:00:00:01"},
                {"boot_mac": "02-00-00-00-00-01"},
            ]
        )



def test_cluster_plan_accepts_bracketed_ipv6_control_plane_endpoint():
    agent.validate_network_inputs(
        {"control_plane_endpoint": "[2001:db8::10]:6443"}
    )


def test_cluster_plan_rejects_url_as_control_plane_endpoint():
    with pytest.raises(agent.NodeAgentError, match="INVALID_CONTROL_PLANE_ENDPOINT"):
        agent.validate_network_inputs(
            {"control_plane_endpoint": "https://192.0.2.10:6443"}
        )


def test_cluster_plan_rejects_malformed_baremetal_host_reference():
    with pytest.raises(agent.NodeAgentError, match="INVALID_BAREMETAL_HOST_REFERENCE"):
        agent.validate_metal3_hosts([{"baremetal_host": ["metal3-system", "bmh-01"]}])



def test_target_agent_control_plane_verification_requires_all_expected_control_planes_ready(
    monkeypatch,
):
    responses = {
        "/version": {"gitVersion": "v1.32.2"},
        "/api/v1/nodes": {
            "items": [
                {
                    "metadata": {
                        "name": "cp-01",
                        "labels": {"node-role.kubernetes.io/control-plane": ""},
                    },
                    "status": {
                        "conditions": [{"type": "Ready", "status": "True"}]
                    },
                },
                {
                    "metadata": {
                        "name": "cp-02",
                        "labels": {"node-role.kubernetes.io/control-plane": ""},
                    },
                    "status": {
                        "conditions": [{"type": "Ready", "status": "False"}]
                    },
                },
                {
                    "metadata": {
                        "name": "worker-01",
                        "labels": {"node-role.kubernetes.io/worker": ""},
                    },
                    "status": {
                        "conditions": [{"type": "Ready", "status": "True"}]
                    },
                },
            ]
        },
    }
    monkeypatch.setattr(agent, "kubernetes_api_get", lambda path: responses[path])
    result = agent.execute_command(
        {
            "command_type": "cluster.control-plane.verify",
            "payload": {"expected_control_plane_replicas": 3},
        },
        "target_cluster",
    )
    assert result["control_plane_ready"] is False
    assert result["ready_control_plane_replicas"] == 1
    assert result["not_ready_control_plane_nodes"] == ["cp-02"]
    assert result["worker_provisioning_allowed"] is False
    assert result["capi_pivot_allowed"] is False
    assert result["side_effects"] is False


def test_target_agent_control_plane_verification_succeeds_without_workers(monkeypatch):
    responses = {
        "/version": {"gitVersion": "v1.32.2"},
        "/api/v1/nodes": {
            "items": [
                {
                    "metadata": {
                        "name": "cp-01",
                        "labels": {"node-role.kubernetes.io/control-plane": ""},
                    },
                    "status": {
                        "conditions": [{"type": "Ready", "status": "True"}]
                    },
                }
            ]
        },
    }
    monkeypatch.setattr(agent, "kubernetes_api_get", lambda path: responses[path])
    result = agent.execute_command(
        {
            "command_type": "cluster.control-plane.verify",
            "payload": {"expected_control_plane_replicas": 1},
        },
        "target_cluster",
    )
    assert result["control_plane_ready"] is True
    assert result["ready_control_plane_replicas"] == 1
    assert result["control_plane_nodes"] == ["cp-01"]
    assert result["worker_provisioning_allowed"] is True
    assert result["capi_pivot_allowed"] is True


def test_target_agent_control_plane_verification_rejects_even_replica_count():
    with pytest.raises(agent.NodeAgentError, match="INVALID_CONTROL_PLANE_REPLICAS"):
        agent.execute_command(
            {
                "command_type": "cluster.control-plane.verify",
                "payload": {"expected_control_plane_replicas": 2},
            },
            "target_cluster",
        )


def test_target_agent_capi_pivot_preflight_requires_apis_controllers_and_no_cluster_conflict(
    monkeypatch,
):
    required = {
        "cluster.x-k8s.io": ["clusters", "machines", "machinesets", "machinedeployments"],
        "bootstrap.cluster.x-k8s.io": ["kubeadmconfigs", "kubeadmconfigtemplates"],
        "controlplane.cluster.x-k8s.io": ["kubeadmcontrolplanes"],
        "infrastructure.cluster.x-k8s.io": [
            "metal3clusters",
            "metal3machines",
            "metal3machinetemplates", "metal3datatemplates",
            "metal3datas",
        ],
        "metal3.io": ["baremetalhosts"],
    }
    responses = {
        "/apis": {
            "groups": [
                {"name": name, "preferredVersion": {"version": "v1beta1"}}
                for name in required
            ]
        },
        "/apis/cluster.x-k8s.io/v1beta1": {
            "resources": [{"name": name} for name in required["cluster.x-k8s.io"]]
        },
        "/apis/bootstrap.cluster.x-k8s.io/v1beta1": {
            "resources": [{"name": name} for name in required["bootstrap.cluster.x-k8s.io"]]
        },
        "/apis/controlplane.cluster.x-k8s.io/v1beta1": {
            "resources": [{"name": name} for name in required["controlplane.cluster.x-k8s.io"]]
        },
        "/apis/infrastructure.cluster.x-k8s.io/v1beta1": {
            "resources": [
                {"name": name}
                for name in required["infrastructure.cluster.x-k8s.io"]
            ]
        },
        "/apis/metal3.io/v1beta1": {
            "resources": [{"name": name} for name in required["metal3.io"]]
        },
        "/apis/cluster.x-k8s.io/v1beta1/namespaces/default/clusters": {
            "items": []
        },
    }
    controllers = {
        "capi-system": "capi-controller-manager",
        "capi-kubeadm-bootstrap-system": "capi-kubeadm-bootstrap-controller-manager",
        "capi-kubeadm-control-plane-system": "capi-kubeadm-control-plane-controller-manager",
        "capm3-system": "capm3-controller-manager",
    }
    for namespace, name in controllers.items():
        responses[f"/apis/apps/v1/namespaces/{namespace}/deployments"] = {
            "items": [
                {
                    "metadata": {"name": name, "generation": 1},
                    "status": {"availableReplicas": 1, "observedGeneration": 1},
                }
            ]
        }
    monkeypatch.setattr(agent, "kubernetes_api_get", lambda path: responses[path])

    result = agent.execute_command(
        {
            "command_type": "cluster.capi.pivot.target.preflight",
            "payload": {"cluster_name": "edge-lab"},
        },
        "target_cluster",
    )

    assert result["target_management_plane_ready"] is True
    assert result["pivot_execution_allowed"] is True
    assert result["provider_versions"] == {group: "v1beta1" for group in required}
    assert result["missing_resources"] == []
    assert result["blockers"] == []
    assert all(item["ready"] for item in result["controllers"])
    assert result["side_effects"] is False


def test_target_agent_capi_pivot_preflight_blocks_missing_controller(monkeypatch):
    required = {
        "cluster.x-k8s.io": ["clusters", "machines", "machinesets", "machinedeployments"],
        "bootstrap.cluster.x-k8s.io": ["kubeadmconfigs", "kubeadmconfigtemplates"],
        "controlplane.cluster.x-k8s.io": ["kubeadmcontrolplanes"],
        "infrastructure.cluster.x-k8s.io": [
            "metal3clusters",
            "metal3machines",
            "metal3machinetemplates", "metal3datatemplates",
        ],
        "metal3.io": ["baremetalhosts"],
    }
    responses = {
        "/apis": {
            "groups": [
                {"name": name, "preferredVersion": {"version": "v1beta1"}}
                for name in required
            ]
        },
        "/apis/cluster.x-k8s.io/v1beta1": {
            "resources": [{"name": name} for name in required["cluster.x-k8s.io"]]
        },
        "/apis/bootstrap.cluster.x-k8s.io/v1beta1": {
            "resources": [{"name": name} for name in required["bootstrap.cluster.x-k8s.io"]]
        },
        "/apis/controlplane.cluster.x-k8s.io/v1beta1": {
            "resources": [{"name": name} for name in required["controlplane.cluster.x-k8s.io"]]
        },
        "/apis/infrastructure.cluster.x-k8s.io/v1beta1": {
            "resources": [
                {"name": name}
                for name in required["infrastructure.cluster.x-k8s.io"]
            ]
        },
        "/apis/metal3.io/v1beta1": {
            "resources": [{"name": name} for name in required["metal3.io"]]
        },
        "/apis/cluster.x-k8s.io/v1beta1/namespaces/default/clusters": {
            "items": []
        },
    }
    controllers = {
        "capi-system": "capi-controller-manager",
        "capi-kubeadm-bootstrap-system": "capi-kubeadm-bootstrap-controller-manager",
        "capi-kubeadm-control-plane-system": "capi-kubeadm-control-plane-controller-manager",
        "capm3-system": "capm3-controller-manager",
    }
    for namespace, name in controllers.items():
        responses[f"/apis/apps/v1/namespaces/{namespace}/deployments"] = {
            "items": [
                {
                    "metadata": {"name": name, "generation": 1},
                    "status": {
                        "availableReplicas": 0 if namespace == "capm3-system" else 1,
                        "observedGeneration": 1,
                    },
                }
            ]
        }
    monkeypatch.setattr(agent, "kubernetes_api_get", lambda path: responses[path])

    result = agent.execute_command(
        {
            "command_type": "cluster.capi.pivot.target.preflight",
            "payload": {"cluster_name": "edge-lab"},
        },
        "target_cluster",
    )

    assert result["target_management_plane_ready"] is False
    assert result["pivot_execution_allowed"] is False
    assert "management_controllers_not_ready" in result["blockers"]
    assert result["side_effects"] is False


def test_target_agent_heartbeat_advertises_worker_provisioning_capabilities():
    client = agent.ControlPlaneClient("https://control.example", "node-123", "agent-token")
    captured = {}

    def fake_request(path, payload=None, lease_token="", method="POST"):
        captured["path"] = path
        captured["payload"] = payload
        captured["method"] = method
        return {"ok": True}

    client.request = fake_request
    result = client.heartbeat("target_cluster")

    assert result == {"ok": True}
    assert captured["path"] == "/api/v1/agent-runtime/heartbeat/"
    assert captured["method"] == "POST"
    assert {
        "cluster.capi.worker.preflight",
        "cluster.capi.worker.provision",
    }.issubset(set(captured["payload"]["capabilities"]))


def test_agent_heartbeats_advertise_the_shared_role_capability_registry():
    for node_type, expected_capabilities in capabilities.ROLE_CAPABILITIES.items():
        client = agent.ControlPlaneClient(
            "https://control.example",
            "node-123",
            "agent-token",
        )
        captured = {}

        def fake_request(
            path,
            payload=None,
            lease_token="",
            method="POST",
            _captured=captured,
        ):
            _captured["payload"] = payload
            return {"ok": True}

        client.request = fake_request
        client.heartbeat(node_type)

        advertised = set(captured["payload"]["capabilities"])
        assert advertised == expected_capabilities
        for command_type in expected_capabilities:
            assert command_type in capabilities.ROLE_CAPABILITIES[node_type]


def test_unknown_agent_role_does_not_advertise_target_capabilities():
    client = agent.ControlPlaneClient(
        "https://control.example",
        "node-123",
        "agent-token",
    )
    captured = {}

    def fake_request(path, payload=None, lease_token="", method="POST"):
        captured["payload"] = payload
        return {"ok": True}

    client.request = fake_request
    client.heartbeat("unknown_role")

    assert captured["payload"]["capabilities"] == []


def test_control_plane_client_restores_durable_deployment_checkpoint(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self, size=-1):
            return json.dumps(
                {
                    "deployment_id": "deployment-123",
                    "generation": 1,
                    "sequence": 4,
                    "phase": "capi_pivot_preflight",
                    "data": {"completed_steps": ["target_agent_bootstrap"]},
                    "resume_available": True,
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(agent.urllib.request, "urlopen", fake_urlopen)
    client = agent.ControlPlaneClient("https://control.example", "node-123", "agent-token")

    checkpoint = client.restore_deployment_checkpoint("deployment-123")

    request = captured["request"]
    assert request.get_method() == "GET"
    assert request.full_url.endswith(
        "/api/v1/agent-runtime/deployments/deployment-123/checkpoint/?node_id=node-123"
    )
    assert request.get_header("Authorization") == "Bearer agent-token"
    assert checkpoint["sequence"] == 4
    assert checkpoint["resume_available"] is True


def test_control_plane_client_saves_checkpoint_without_credentials(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self, size=-1):
            return b'{"sequence":5,"idempotent":false}'

    def fake_urlopen(request, timeout):
        captured["request"] = request
        return Response()

    monkeypatch.setattr(agent.urllib.request, "urlopen", fake_urlopen)
    client = agent.ControlPlaneClient("https://control.example", "node-123", "agent-token")

    result = client.save_deployment_checkpoint(
        "deployment-123",
        1,
        5,
        "capi_pivot_preflight",
        {"completed_steps": ["target_agent_bootstrap"]},
    )

    request = captured["request"]
    payload = json.loads(request.data.decode("utf-8"))
    assert request.get_method() == "POST"
    assert payload["sequence"] == 5
    assert "token" not in json.dumps(payload).lower()
    assert result["sequence"] == 5


def test_control_plane_client_rejects_checkpoint_credentials():
    client = agent.ControlPlaneClient("https://control.example", "node-123", "agent-token")

    with pytest.raises(agent.NodeAgentError, match="INVALID_DEPLOYMENT_CHECKPOINT"):
        client.save_deployment_checkpoint(
            "deployment-123",
            1,
            6,
            "capi_pivot_preflight",
            {"completed_steps": [], "kubeconfig": "sensitive"},
        )


def test_control_plane_client_rejects_invalid_checkpoint_generation_and_phase():
    client = agent.ControlPlaneClient("https://control.example", "node-123", "agent-token")

    with pytest.raises(agent.NodeAgentError, match="INVALID_DEPLOYMENT_CHECKPOINT"):
        client.save_deployment_checkpoint(
            "deployment-123",
            True,
            1,
            "target_agent_bootstrap",
            {},
        )

    with pytest.raises(agent.NodeAgentError, match="INVALID_DEPLOYMENT_CHECKPOINT"):
        client.save_deployment_checkpoint(
            "deployment-123",
            1,
            1,
            "unknown_phase",
            {},
        )


def test_collect_capi_reconciliation_inventory_records_cluster_and_observed_machines(monkeypatch):
    versions = {
        "cluster.x-k8s.io": "v1beta1",
        "controlplane.cluster.x-k8s.io": "v1beta1",
        "infrastructure.cluster.x-k8s.io": "v1beta1",
        "metal3.io": "v1alpha1",
    }
    cluster = {
        "metadata": {
            "name": "edge-lab",
            "namespace": "capi-system",
            "uid": "cluster-uid",
            "generation": 2,
            "resourceVersion": "17",
        },
        "spec": {},
        "status": {"conditions": [{"type": "ControlPlaneReady", "status": "True"}]},
    }
    machine = {
        "metadata": {
            "name": "edge-lab-worker-0",
            "namespace": "capi-system",
            "uid": "machine-uid",
            "generation": 1,
            "resourceVersion": "21",
        },
        "spec": {},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    monkeypatch.setattr(
        agent,
        "kubernetes_api_get",
        lambda path: {"groups": [
            {"name": name, "preferredVersion": {"version": version}}
            for name, version in versions.items()
        ]} if path == "/apis" else cluster,
    )
    monkeypatch.setattr(
        agent,
        "list_all_kubernetes_items",
        lambda path, max_items=100000: [machine] if path.endswith("/machines") else [],
    )

    report = agent.collect_capi_reconciliation_inventory("edge-lab", "capi-system")

    assert report["cluster_name"] == "edge-lab"
    assert report["cluster_uid"] == "cluster-uid"
    assert [item["kind"] for item in report["resources"]] == ["Cluster", "Machine"]
    assert report["resources"][1]["uid"] == "machine-uid"
    assert report["resources"][0]["conditions"] == [
        {"type": "ControlPlaneReady", "status": "True"}
    ]


def test_target_agent_submits_reconciliation_evidence_without_authorizing_recovery(
    monkeypatch,
    capsys,
):
    class FakeClient:
        deployment_checkpoints = {}

        def __init__(self):
            self.submitted = []

        def heartbeat(self, node_type):
            return {
                "active_deployments": [{
                    "deployment_id": "deployment-456",
                    "cluster_name": "edge-lab",
                    "capi_namespace": "capi-system",
                    "status": "reconciliation_required",
                }]
            }

        def report_inventory(self, inventory):
            return {}

        def report_reconciliation_inventory(self, deployment_id, report):
            self.submitted.append((deployment_id, report))
            return {"reconciliation_state": "evidence_recorded_not_authorized"}

        def restore_deployment_checkpoint(self, deployment_id):
            checkpoint = {
                "deployment_id": deployment_id,
                "phase": "worker_provisioning",
                "sequence": 4,
                "resume_available": False,
            }
            self.deployment_checkpoints[deployment_id] = checkpoint
            return checkpoint

        def poll(self):
            return {"command": None}

    client = FakeClient()
    monkeypatch.setattr(agent, "inspect_host_network_hardware", lambda: {"status": "unavailable"})
    monkeypatch.setattr(
        agent,
        "collect_capi_reconciliation_inventory",
        lambda cluster_name, namespace: {
            "cluster_name": cluster_name,
            "cluster_uid": "cluster-uid",
            "resources": [{"kind": "Cluster"}],
        },
    )
    monkeypatch.setattr(agent, "touch_health_files", lambda readiness=False: None)

    assert agent.run_once(client, "target_cluster") is False
    assert len(client.submitted) == 1
    assert client.submitted[0][0] == "deployment-456"
    output = json.loads(capsys.readouterr().out.strip())
    assert output["event"] == "deployment_reconciliation_report_submitted"
    assert output["retry_authorized"] is False
    assert output["eph_reclaim_authorized"] is False


def test_target_agent_restores_assigned_deployment_checkpoint_before_polling(
    monkeypatch,
    capsys,
):
    client = agent.ControlPlaneClient("https://control.example", "node-123", "agent-token")
    deployment_id = "deployment-123"
    client.heartbeat = lambda node_type: {
        "active_deployments": [{"deployment_id": deployment_id}]
    }
    client.deployment_checkpoints[deployment_id] = {
        "deployment_id": deployment_id,
        "generation": 1,
        "sequence": 2,
        "phase": "target_agent_bootstrap",
        "data": {"completed_steps": []},
        "resume_available": True,
    }

    def restore(deployment):
        checkpoint = {
            "deployment_id": deployment,
            "generation": 1,
            "sequence": 3,
            "phase": "capi_pivot_preflight",
            "data": {"completed_steps": ["target_agent_bootstrap"]},
            "resume_available": True,
        }
        client.deployment_checkpoints[deployment] = checkpoint
        return checkpoint

    client.restore_deployment_checkpoint = restore
    client.poll = lambda: {"command": None}
    monkeypatch.setattr(agent, "touch_health_files", lambda readiness=False: None)

    assert agent.run_once(client, "target_cluster") is False
    output = json.loads(capsys.readouterr().out.strip())
    assert output["event"] == "deployment_checkpoint_restored"
    assert output["deployment_id"] == deployment_id
    assert output["sequence"] == 3
    assert client.deployment_checkpoints[deployment_id]["phase"] == "capi_pivot_preflight"


def test_target_capi_pivot_preflight_checks_apis_controllers_and_cluster_conflicts(
    monkeypatch,
):
    group_versions = {
        "cluster.x-k8s.io": "v1beta1",
        "bootstrap.cluster.x-k8s.io": "v1beta1",
        "controlplane.cluster.x-k8s.io": "v1beta1",
        "infrastructure.cluster.x-k8s.io": "v1beta1",
        "metal3.io": "v1alpha1",
    }
    required_resources = {
        "cluster.x-k8s.io": ["clusters", "machines", "machinesets", "machinedeployments"],
        "bootstrap.cluster.x-k8s.io": ["kubeadmconfigs", "kubeadmconfigtemplates"],
        "controlplane.cluster.x-k8s.io": ["kubeadmcontrolplanes"],
        "infrastructure.cluster.x-k8s.io": [
            "metal3clusters",
            "metal3machines",
            "metal3machinetemplates", "metal3datatemplates",
        ],
        "metal3.io": ["baremetalhosts"],
    }
    controllers = {
        "capi-system": "capi-controller-manager",
        "capi-kubeadm-bootstrap-system": "capi-kubeadm-bootstrap-controller-manager",
        "capi-kubeadm-control-plane-system": (
            "capi-kubeadm-control-plane-controller-manager"
        ),
        "capm3-system": "capm3-controller-manager",
    }
    responses = {
        "/apis": {
            "groups": [
                {
                    "name": group,
                    "preferredVersion": {"version": version},
                }
                for group, version in group_versions.items()
            ]
        },
        "/apis/cluster.x-k8s.io/v1beta1/namespaces/default/clusters": {
            "items": []
        },
    }
    for group, resources in required_resources.items():
        version = group_versions[group]
        responses[f"/apis/{group}/{version}"] = {
            "resources": [{"name": resource} for resource in resources]
        }
    for namespace, deployment_name in controllers.items():
        responses[
            f"/apis/apps/v1/namespaces/{namespace}/deployments"
        ] = {
            "items": [
                {
                    "metadata": {"name": deployment_name, "generation": 2},
                    "spec": {"replicas": 1},
                    "status": {"availableReplicas": 1, "observedGeneration": 2},
                }
            ]
        }

    monkeypatch.setattr(agent, "kubernetes_api_get", lambda path: responses[path])
    result = agent.execute_command(
        {
            "command_type": "cluster.capi.pivot.target.preflight",
            "payload": {"cluster_name": "edge-lab", "namespace": "default"},
        },
        "target_cluster",
    )

    assert result["target_management_plane_ready"] is True
    assert result["pivot_execution_allowed"] is True
    assert result["missing_resources"] == []
    assert result["cluster_name_conflicts"] == []
    assert result["blockers"] == []
    assert result["side_effects"] is False


def test_capi_owner_graph_handles_deep_inventory_without_quadratic_scans():
    inventory = [
        {
            "api_version": "cluster.x-k8s.io/v1beta1",
            "kind": "Cluster",
            "resource": "cluster.x-k8s.io/clusters",
            "name": "edge-lab",
            "uid": "cluster-root",
            "owner_references": [],
            "owner_references_truncated": False,
            "cluster_name": "edge-lab",
        }
    ]
    previous_uid = "cluster-root"
    previous_kind = "Cluster"
    previous_name = "edge-lab"

    for index in range(2500):
        uid = f"machine-{index}"
        inventory.append(
            {
                "api_version": "cluster.x-k8s.io/v1beta1",
                "kind": "Machine",
                "resource": "cluster.x-k8s.io/machines",
                "name": uid,
                "uid": uid,
                "owner_references": [
                    {
                        "api_version": "cluster.x-k8s.io/v1beta1",
                        "kind": previous_kind,
                        "name": previous_name,
                        "uid": previous_uid,
                    }
                ],
                "owner_references_truncated": False,
                "cluster_name": "edge-lab",
            }
        )
        previous_uid = uid
        previous_kind = "Machine"
        previous_name = uid

    result = agent.build_capi_owner_graph(
        inventory,
        "edge-lab",
        "cluster-root",
        True,
    )

    assert result["complete"] is True
    assert result["object_count"] == 2501
    assert result["unique_uid_count"] == 2501
    assert result["reachable_object_count"] == 2501


def test_eph_capi_pivot_source_preflight_inventories_all_namespaces_read_only(
    monkeypatch,
):
    groups = {
        "cluster.x-k8s.io": (
            "v1beta1",
            ["clusters", "machines", "machinesets", "machinedeployments"],
        ),
        "bootstrap.cluster.x-k8s.io": (
            "v1beta1",
            ["kubeadmconfigs", "kubeadmconfigtemplates"],
        ),
        "controlplane.cluster.x-k8s.io": ("v1beta1", ["kubeadmcontrolplanes"]),
        "infrastructure.cluster.x-k8s.io": (
            "v1beta1",
            [
                "metal3clusters",
                "metal3machines",
                "metal3machinetemplates",
                "metal3datatemplates",
                "metal3datas",
            ],
        ),
        "metal3.io": ("v1alpha1", ["baremetalhosts"]),
    }
    controllers = {
        "capi-system": "capi-controller-manager",
        "capi-kubeadm-bootstrap-system": "capi-kubeadm-bootstrap-controller-manager",
        "capi-kubeadm-control-plane-system": (
            "capi-kubeadm-control-plane-controller-manager"
        ),
        "capm3-system": "capm3-controller-manager",
    }
    calls = []

    def fake_get(path):
        calls.append(path)
        query = path.split("?", 1)[1] if "?" in path else ""
        path = path.split("?", 1)[0]
        if path == "/apis":
            return {
                "groups": [
                    {
                        "name": group,
                        "preferredVersion": {"version": version},
                    }
                    for group, (version, _) in groups.items()
                ]
            }
        if path.startswith("/apis/apps/v1/namespaces/"):
            namespace = path.split("/")[5]
            deployment_name = controllers[namespace]
            return {
                "items": [
                    {
                        "metadata": {"name": deployment_name, "generation": 3},
                        "spec": {"replicas": 1},
                        "status": {"availableReplicas": 1, "observedGeneration": 3},
                    }
                ]
            }
        for group, (version, resources) in groups.items():
            if path == f"/apis/{group}/{version}":
                return {"resources": [{"name": name} for name in resources]}
            prefix = f"/apis/{group}/{version}/"
            if path.startswith(prefix):
                resource = path[len(prefix):]
                assert "?" not in path
                items = []
                if resource == "clusters":
                    items = [
                        {
                            "apiVersion": f"{group}/{version}",
                            "kind": "Cluster",
                            "metadata": {
                                "name": "edge-lab",
                                "namespace": "capi-system",
                                "uid": "cluster-uid",
                                "labels": {"cluster.x-k8s.io/cluster-name": "edge-lab"},
                                "ownerReferences": [],
                            },
                        }
                    ]
                if resource == "machines":
                    owner = {
                        "apiVersion": "cluster.x-k8s.io/v1beta1",
                        "kind": "Cluster",
                        "name": "edge-lab",
                        "uid": "cluster-uid",
                    }
                    page = 2 if "continue=page-2" in query else 1
                    items = [
                        {
                            "apiVersion": f"{group}/{version}",
                            "kind": "Machine",
                            "metadata": {
                                "name": f"machine-{page}",
                                "namespace": "capi-system",
                                "uid": f"machine-uid-{page}",
                                "labels": {"cluster.x-k8s.io/cluster-name": "edge-lab"},
                                "ownerReferences": [owner],
                            },
                        }
                    ]
                    metadata = {"continue": "page-2"} if page == 1 else {}
                    return {"items": items, "metadata": metadata}
                return {"items": items, "metadata": {}}
        raise AssertionError(path)

    monkeypatch.setattr(agent, "kubernetes_api_get", fake_get)
    result = agent.execute_command(
        {
            "command_type": "cluster.capi.pivot.source.preflight",
            "payload": {"cluster_name": "edge-lab"},
        },
        "eph_node",
    )

    assert result["source_management_plane_ready"] is True
    assert result["source_cluster_identity"] == {
        "name": "edge-lab",
        "namespace": "capi-system",
        "uid": "cluster-uid",
        "api_version": "cluster.x-k8s.io/v1beta1",
    }
    assert result["provider_versions"] == {
        group: version for group, (version, _) in groups.items()
    }
    assert len(result["resource_counts"]) == 11
    assert result["resource_counts"]["cluster.x-k8s.io/clusters"] == 1
    assert result["resource_counts"]["cluster.x-k8s.io/machines"] == 2
    assert result["owner_graph"]["object_count"] == 3
    assert result["owner_graph"]["unique_uid_count"] == 3
    assert result["inventory"][0]["name"] == "edge-lab"
    assert result["inventory"][0]["namespace"] == "capi-system"
    assert result["missing_resources"] == []
    assert result["pagination_required"] == []
    assert result["blockers"] == []
    assert result["inventory_truncated"] is False
    assert result["side_effects"] is False
    assert any(
        path.startswith("/apis/cluster.x-k8s.io/v1beta1/clusters?limit=500")
        for path in calls
    )
    assert any("continue=page-2" in path for path in calls)

    original_get = agent.kubernetes_api_get

    def missing_source_cluster(path):
        if path.split("?", 1)[0].endswith("/clusters"):
            return {"items": [], "metadata": {}}
        return original_get(path)

    monkeypatch.setattr(agent, "kubernetes_api_get", missing_source_cluster)
    blocked = agent.execute_command(
        {
            "command_type": "cluster.capi.pivot.source.preflight",
            "payload": {"cluster_name": "edge-lab"},
        },
        "eph_node",
    )
    assert blocked["source_management_plane_ready"] is False
    assert "requested_source_cluster_not_found" in blocked["blockers"]



def test_long_running_command_renews_its_lease(monkeypatch):
    renewals = []

    class FakeClient:
        def renew_lease(self, command_id, lease_token):
            renewals.append((command_id, lease_token))
            return {"id": command_id}

    def slow_command(command, node_type, cancellation_event=None):
        time.sleep(0.06)
        return {"ready": True}

    monkeypatch.setattr(agent, "execute_command", slow_command)
    result = agent.execute_command_with_lease(
        FakeClient(),
        {"id": "command-1", "lease_token": "lease-1"},
        "eph_node",
        renew_interval=0.01,
    )

    assert result == {"ready": True}
    assert renewals
    assert all(item == ("command-1", "lease-1") for item in renewals)


def test_long_running_command_keeps_liveness_fresh_without_refreshing_readiness(
    monkeypatch,
):
    health_updates = []

    class FakeClient:
        def renew_lease(self, command_id, lease_token):
            return {"id": command_id}

    def fake_touch_health_files(readiness=False):
        health_updates.append(readiness)

    def slow_command(command, node_type, cancellation_event=None):
        time.sleep(0.06)
        return {"ready": True}

    monkeypatch.setattr(agent, "touch_health_files", fake_touch_health_files)
    monkeypatch.setattr(agent, "execute_command", slow_command)

    result = agent.execute_command_with_lease(
        FakeClient(),
        {"id": "command-readiness", "lease_token": "lease-readiness"},
        "eph_node",
        renew_interval=0.01,
    )

    assert result == {"ready": True}
    assert health_updates
    assert all(readiness is False for readiness in health_updates)


def test_command_is_not_reported_successful_after_lease_renewal_fails(monkeypatch):
    class FakeClient:
        def renew_lease(self, command_id, lease_token):
            raise agent.NodeAgentError("CONTROL_PLANE_UNAVAILABLE")

    def slow_command(command, node_type, cancellation_event=None):
        time.sleep(0.04)
        return {"ready": True}

    monkeypatch.setattr(agent, "execute_command", slow_command)
    with pytest.raises(agent.NodeAgentError, match="COMMAND_LEASE_LOST"):
        agent.execute_command_with_lease(
            FakeClient(),
            {"id": "command-2", "lease_token": "lease-2"},
            "target_cluster",
            renew_interval=0.01,
        )


def test_command_lease_is_required_before_execution():
    class FakeClient:
        def renew_lease(self, command_id, lease_token):
            raise AssertionError("Lease renewal must not run")

    with pytest.raises(agent.NodeAgentError, match="COMMAND_LEASE_REQUIRED"):
        agent.execute_command_with_lease(
            FakeClient(),
            {"id": "command-3"},
            "eph_node",
        )



def test_target_agent_verifies_pivot_adoption_by_preserved_cluster_uid(monkeypatch):
    responses = {
        "/apis": {
            "groups": [
                {
                    "name": "cluster.x-k8s.io",
                    "preferredVersion": {"version": "v1beta1"},
                }
            ]
        },
        "/apis/cluster.x-k8s.io/v1beta1/namespaces/metal3/clusters/edge-lab": {
            "metadata": {"name": "edge-lab", "namespace": "metal3", "uid": "source-uid"}
        },
        "/apis/cluster.x-k8s.io/v1beta1/namespaces/metal3/machines": {
            "items": [
                {
                    "metadata": {"name": "edge-lab-control-plane-0"},
                    "spec": {"clusterName": "edge-lab"},
                    "status": {
                        "conditions": [{"type": "Ready", "status": "True"}]
                    },
                },
                {
                    "metadata": {"name": "unrelated-cluster-worker"},
                    "spec": {"clusterName": "another-cluster"},
                    "status": {
                        "conditions": [{"type": "Ready", "status": "False"}]
                    },
                },
            ]
        },
        "/apis/apps/v1/namespaces/capi-system/deployments/capi-controller-manager": {
            "metadata": {"generation": 1},
            "spec": {"replicas": 1},
            "status": {"availableReplicas": 1, "observedGeneration": 1},
        },
        (
            "/apis/apps/v1/namespaces/capi-kubeadm-bootstrap-system/deployments/"
            "capi-kubeadm-bootstrap-controller-manager"
        ): {
            "metadata": {"generation": 1},
            "spec": {"replicas": 1},
            "status": {"availableReplicas": 1, "observedGeneration": 1},
        },
        (
            "/apis/apps/v1/namespaces/capi-kubeadm-control-plane-system/deployments/"
            "capi-kubeadm-control-plane-controller-manager"
        ): {
            "metadata": {"generation": 1},
            "spec": {"replicas": 1},
            "status": {"availableReplicas": 1, "observedGeneration": 1},
        },
        "/apis/apps/v1/namespaces/capm3-system/deployments/capm3-controller-manager": {
            "metadata": {"generation": 1},
            "spec": {"replicas": 1},
            "status": {"availableReplicas": 1, "observedGeneration": 1},
        },
    }
    monkeypatch.setattr(
        agent,
        "kubernetes_api_get",
        lambda path: responses[path],
    )

    result = agent.execute_command(
        {
            "command_type": "cluster.capi.pivot.target.verify",
            "payload": {
                "cluster_name": "edge-lab",
                "namespace": "metal3",
                "expected_cluster_uid": "source-uid",
            },
        },
        "target_cluster",
    )

    assert result["pivot_adoption_verified"] is True
    assert result["cluster_identity_preserved"] is True
    assert result["cluster_uid"] == "source-uid"
    assert result["machine_count"] == 1
    assert result["ready_machine_count"] == 1
    assert result["not_ready_machine_names"] == []
    assert result["machine_inventory_ready"] is True
    assert result["controllers_ready"] is True
    assert len(result["controllers"]) == 4
    assert result["side_effects"] is False


def test_target_agent_rejects_partial_machine_readiness_after_pivot(monkeypatch):
    responses = {
        "/apis": {
            "groups": [
                {
                    "name": "cluster.x-k8s.io",
                    "preferredVersion": {"version": "v1beta1"},
                }
            ]
        },
        "/apis/cluster.x-k8s.io/v1beta1/namespaces/metal3/clusters/edge-lab": {
            "metadata": {"name": "edge-lab", "namespace": "metal3", "uid": "source-uid"}
        },
        "/apis/cluster.x-k8s.io/v1beta1/namespaces/metal3/machines": {
            "items": [
                {
                    "metadata": {"name": "edge-lab-control-plane-0"},
                    "spec": {"clusterName": "edge-lab"},
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                },
                {
                    "metadata": {"name": "edge-lab-worker-0"},
                    "spec": {"clusterName": "edge-lab"},
                    "status": {"conditions": [{"type": "Ready", "status": "False"}]},
                },
                {
                    "metadata": {"name": "other-cluster-machine"},
                    "spec": {"clusterName": "other-cluster"},
                    "status": {"conditions": [{"type": "Ready", "status": "False"}]},
                },
            ]
        },
        "/apis/apps/v1/namespaces/capi-system/deployments/capi-controller-manager": {
            "metadata": {"generation": 1},
            "spec": {"replicas": 1},
            "status": {"availableReplicas": 1, "observedGeneration": 1},
        },
        (
            "/apis/apps/v1/namespaces/capi-kubeadm-bootstrap-system/deployments/"
            "capi-kubeadm-bootstrap-controller-manager"
        ): {
            "metadata": {"generation": 1},
            "spec": {"replicas": 1},
            "status": {"availableReplicas": 1, "observedGeneration": 1},
        },
        (
            "/apis/apps/v1/namespaces/capi-kubeadm-control-plane-system/deployments/"
            "capi-kubeadm-control-plane-controller-manager"
        ): {
            "metadata": {"generation": 1},
            "spec": {"replicas": 1},
            "status": {"availableReplicas": 1, "observedGeneration": 1},
        },
        "/apis/apps/v1/namespaces/capm3-system/deployments/capm3-controller-manager": {
            "metadata": {"generation": 1},
            "spec": {"replicas": 1},
            "status": {"availableReplicas": 1, "observedGeneration": 1},
        },
    }
    monkeypatch.setattr(agent, "kubernetes_api_get", lambda path: responses[path])

    result = agent.execute_command(
        {
            "command_type": "cluster.capi.pivot.target.verify",
            "payload": {
                "cluster_name": "edge-lab",
                "namespace": "metal3",
                "expected_cluster_uid": "source-uid",
            },
        },
        "target_cluster",
    )

    assert result["machine_count"] == 2
    assert result["ready_machine_count"] == 1
    assert result["not_ready_machine_names"] == ["edge-lab-worker-0"]
    assert result["machine_inventory_ready"] is False
    assert result["pivot_adoption_verified"] is False


def test_eph_agent_cannot_run_target_adoption_verification():
    with pytest.raises(agent.NodeAgentError, match="CAPABILITY_NOT_CONFIGURED"):
        agent.execute_command(
            {
                "command_type": "cluster.capi.pivot.target.verify",
                "payload": {
                    "cluster_name": "edge-lab",
                    "namespace": "metal3",
                    "expected_cluster_uid": "source-uid",
                },
            },
            "eph_node",
        )



def test_agent_factory_returns_distinct_role_runtimes():
    class FakeClient:
        pass

    eph_agent = agent.create_agent(FakeClient(), "eph_node")
    target_agent = agent.create_agent(FakeClient(), "target_cluster")

    assert isinstance(eph_agent, agent.EPHNodeAgent)
    assert isinstance(target_agent, agent.TargetClusterAgent)
    assert eph_agent.node_type == "eph_node"
    assert target_agent.node_type == "target_cluster"


def test_agent_factory_rejects_unknown_role():
    with pytest.raises(agent.NodeAgentError, match="INVALID_AGENT_ROLE"):
        agent.create_agent(object(), "unknown")


def valid_dual_network_profile():
    return {
        "version": "v1",
        "pod_traffic_default": "data_plane",
        "control_plane": {
            "interface": "eno1",
            "cidr": "10.10.10.10/24",
            "gateway": "10.10.10.1",
            "mtu": 1500,
        },
        "data_plane": {
            "interfaces": ["ens2f0"],
            "mtu": 9000,
            "hugepages": "1Gi",
            "numa_policy": "single-numa-node",
            "cpu_manager_policy": "static",
            "cni": {
                "primary": "calico",
                "meta": "multus",
                "attachments": [
                    {"name": "ovs-data", "type": "ovs"},
                    {
                        "name": "sriov-data",
                        "type": "sriov",
                        "resource_name": "intel.com/sriov_netdevice",
                        "dpdk": True,
                    },
                ],
            },
        },
    }

def valid_metal3_data_template(profile):
    normalized_profile = agent.validate_network_profile(profile)
    control_interface = profile["control_plane"]["interface"]
    control_host_interface = normalized_profile["control_plane"]["host_interface"]
    data_interfaces = profile["data_plane"]["interfaces"]
    ethernets = [
        {
            "id": control_interface,
            "macAddress": {"fromHostInterface": control_host_interface},
            "mtu": profile["control_plane"]["mtu"],
            "type": "phy",
        }
    ]
    ethernets.extend(
        {
            "id": name,
            "macAddress": {
                "fromHostInterface": normalized_profile["data_plane"]["host_interfaces"][name]
            },
            "mtu": profile["data_plane"]["mtu"],
            "type": "phy",
        }
        for name in data_interfaces
    )
    return {
        "apiVersion": TEST_INFRA_API_VERSION,
        "kind": "Metal3DataTemplate",
        "metadata": {
            "name": "worker-network-template",
            "namespace": "default",
            "uid": "network-template-uid",
            "annotations": {
                "ai-agent-stack.io/network-profile-hash": normalized_profile["profile_hash"]
            },
        },
        "spec": {
            "networkData": {"links": {"ethernets": ethernets}}
        },
    }


def test_metal3_data_template_verifies_control_and_data_plane_nics():
    profile = valid_dual_network_profile()
    result = agent.validate_metal3_data_template(
        agent.validate_network_profile(profile),
        valid_metal3_data_template(profile),
        TEST_INFRA_API_VERSION,
        "default",
    )
    assert result["verified"] is True
    assert result["interface_names"] == ["eno1", "ens2f0"]


def test_metal3_data_template_supports_host_interface_mapping():
    profile = valid_dual_network_profile()
    profile["control_plane"]["host_interface"] = "eno2"
    profile["data_plane"]["host_interfaces"] = {"ens2f0": "enp2s0"}
    normalized_profile = agent.validate_network_profile(profile)
    result = agent.validate_metal3_data_template(
        normalized_profile,
        valid_metal3_data_template(profile),
        TEST_INFRA_API_VERSION,
        "default",
    )
    assert result["verified"] is True
    assert result["interface_names"] == ["eno1", "ens2f0"]



def remove_data_plane_link(data_template):
    data_template["spec"]["networkData"]["links"]["ethernets"].pop()


def change_control_plane_mtu(data_template):
    data_template["spec"]["networkData"]["links"]["ethernets"][0]["mtu"] = 9000


def change_network_profile_hash(data_template):
    data_template["metadata"]["annotations"]["ai-agent-stack.io/network-profile-hash"] = "0" * 64


@pytest.mark.parametrize(
    "mutate,error",
    [
        (remove_data_plane_link, "METAL3_DATA_TEMPLATE_INTERFACE_MISSING_OR_DUPLICATE"),
        (change_control_plane_mtu, "METAL3_DATA_TEMPLATE_INTERFACE_CONFIGURATION_MISMATCH"),
        (change_network_profile_hash, "METAL3_DATA_TEMPLATE_IDENTITY_OR_PROFILE_MISMATCH"),
    ],
)
def test_metal3_data_template_rejects_missing_or_mismatched_network_config(mutate, error):
    profile = valid_dual_network_profile()
    data_template = valid_metal3_data_template(profile)
    mutate(data_template)
    with pytest.raises(agent.NodeAgentError, match=error):
        agent.validate_metal3_data_template(
            agent.validate_network_profile(profile),
            data_template,
            TEST_INFRA_API_VERSION,
            "default",
        )


def valid_rendered_network_data(profile):
    return {
        "links": [
            {
                "id": profile["control_plane"]["interface"],
                "type": "phy",
                "mtu": profile["control_plane"]["mtu"],
                "ethernet_mac_address": "02:00:00:00:00:01",
            },
            *[
                {
                    "id": name,
                    "type": "phy",
                    "mtu": profile["data_plane"]["mtu"],
                    "ethernet_mac_address": "02:00:00:00:00:02",
                }
                for name in profile["data_plane"]["interfaces"]
            ],
        ]
    }


def test_rendered_network_data_verifies_actual_interface_links():
    profile = agent.validate_network_profile(valid_dual_network_profile())
    result = agent.validate_rendered_network_data(
        profile, valid_rendered_network_data(profile)
    )
    assert result["verified"] is True
    assert result["interface_names"] == ["eno1", "ens2f0"]


def test_rendered_network_data_rejects_missing_interface_and_invalid_mac():
    profile = agent.validate_network_profile(valid_dual_network_profile())
    network_data = valid_rendered_network_data(profile)
    network_data["links"].pop()
    with pytest.raises(
        agent.NodeAgentError,
        match="METAL3_RENDERED_NETWORK_INTERFACE_MISSING_OR_DUPLICATE",
    ):
        agent.validate_rendered_network_data(profile, network_data)
    network_data = valid_rendered_network_data(profile)
    network_data["links"][0]["ethernet_mac_address"] = "not-a-mac"
    with pytest.raises(
        agent.NodeAgentError,
        match="METAL3_RENDERED_NETWORK_INTERFACE_CONFIGURATION_MISMATCH",
    ):
        agent.validate_rendered_network_data(profile, network_data)


@pytest.mark.parametrize("node_type", ["eph_node", "target_cluster"])
def test_network_inspection_command_is_available_to_both_agent_roles(monkeypatch, node_type):
    calls = []

    def fake_get(path):
        calls.append(path)
        if path == "/version":
            return {"gitVersion": "v1.32.2"}
        if path == "/apis":
            return {
                "groups": [
                    {
                        "name": "k8s.cni.cncf.io",
                        "preferredVersion": {"version": "v1"},
                    }
                ]
            }
        raise AssertionError(path)

    def fake_list(path, max_items=100000):
        calls.append(path)
        if path == "/api/v1/namespaces/ai-agent-inventory/configmaps":
            return []
        if path == "/api/v1/nodes":
            return [
                {
                    "metadata": {
                        "name": "worker-01",
                        "labels": {"feature.node.kubernetes.io/network-sriov": "true"},
                    },
                    "status": {
                        "allocatable": {
                            "cpu": "32",
                            "intel.com/sriov_netdevice": "8",
                            "hugepages-2Mi": "1Gi",
                        }
                    },
                }
            ]
        if path == "/api/v1/namespaces/kube-system/pods":
            return [
                {"metadata": {"name": "calico-node-abc", "namespace": "kube-system"}},
                {"metadata": {"name": "kube-multus-ds-abc", "namespace": "kube-system"}},
                {"metadata": {"name": "sriov-device-plugin-abc", "namespace": "kube-system"}},
            ]
        if path == "/apis/k8s.cni.cncf.io/v1/network-attachment-definitions":
            return [
                {
                    "metadata": {"name": "sriov-net", "namespace": "default"},
                    "spec": {
                        "config": json.dumps(
                            {"type": "sriov", "resourceName": "intel.com/sriov_netdevice"}
                        )
                    },
                },
                {
                    "metadata": {"name": "ovs-net", "namespace": "default"},
                    "spec": {"config": json.dumps({"type": "ovs"})},
                },
            ]
        raise AssertionError(path)

    monkeypatch.setattr(agent, "kubernetes_api_get", fake_get)
    monkeypatch.setattr(agent, "list_all_kubernetes_items", fake_list)
    monkeypatch.setattr(
        agent,
        "_pod_network_namespace_snapshot",
        lambda: {
            "scope": "agent_pod_network_namespace",
            "interfaces": ["eth0"],
            "default_routes": [{"interface": "eth0", "gateway": "10.20.0.1"}],
        },
    )

    result = agent.execute_command(
        {"command_type": "cluster.network.inspect", "payload": {}},
        node_type,
    )

    assert result["inspection_status"] == "observed"
    assert result["node_count"] == 1
    assert result["observed_components"]["calico"] is True
    assert result["observed_components"]["multus"] is True
    assert result["observed_components"]["sriov_device_plugin"] is True
    assert result["observed_attachment_plugin_types"] == ["ovs", "sriov"]
    assert result["network_attachment_definition_count"] == 2
    assert result["node_network_resources"][0]["allocatable_network_resources"][
        "intel.com/sriov_netdevice"
    ] == "8"
    assert result["agent_pod_network"]["interfaces"] == ["eth0"]
    assert "Pod namespace interfaces are not host NIC inventory." in result["limitations"]
    assert "/api/v1/namespaces/kube-system/pods" in calls


def test_network_profile_accepts_calico_multus_ovs_and_sriov():
    result = agent.validate_network_profile(valid_dual_network_profile())

    assert result["valid"] is True
    assert result["pod_traffic_default"] == "data_plane"
    assert result["primary_cni"] == "calico"
    assert result["meta_cni"] == "multus"
    assert result["attachment_count"] == 2
    assert len(result["profile_hash"]) == 64


@pytest.mark.parametrize(
    "mutate,error",
    [
        (lambda profile: profile.update({"pod_traffic_default": "control_plane"}),
         "POD_TRAFFIC_MUST_DEFAULT_TO_DATA_PLANE"),
        (lambda profile: profile["control_plane"].update({"gateway": "10.10.11.1"}),
         "CONTROL_PLANE_GATEWAY_OUTSIDE_CIDR"),
        (lambda profile: profile["control_plane"].pop("mtu"), "INVALID_CONTROL_PLANE_MTU"),
        (lambda profile: profile["data_plane"].pop("mtu"), "INVALID_DATA_PLANE_MTU"),
        (lambda profile: profile["data_plane"]["cni"].update({"primary": "flannel"}),
         "UNSUPPORTED_PRIMARY_CNI_COMPOSITION"),
        (lambda profile: profile["data_plane"]["cni"]["attachments"].append(
            {"name": "sriov-no-resource", "type": "sriov"}
        ), "SRIOV_RESOURCE_NAME_REQUIRED"),
    ],
)
def test_network_profile_rejects_invalid_routing_or_cni_composition(mutate, error):
    profile = valid_dual_network_profile()
    mutate(profile)

    with pytest.raises(agent.NodeAgentError, match=error):
        agent.validate_network_profile(profile)


def test_dpdk_network_profile_requires_hugepages_and_numa_policy():
    profile = valid_dual_network_profile()
    profile["data_plane"].pop("hugepages")
    profile["data_plane"].pop("numa_policy")
    profile["data_plane"].pop("cpu_manager_policy")
    profile["data_plane"]["cni"]["attachments"].append(
        {
            "name": "dpdk-vf",
            "type": "sriov",
            "resource_name": "intel.com/sriov_dpdk",
            "dpdk": True,
        }
    )

    with pytest.raises(agent.NodeAgentError, match="DPDK_HUGEPAGES_REQUIRED"):
        agent.validate_network_profile(profile)

    profile["data_plane"]["hugepages"] = {"2Mi": "1Gi"}
    with pytest.raises(agent.NodeAgentError, match="DPDK_NUMA_POLICY_REQUIRED"):
        agent.validate_network_profile(profile)

    profile["data_plane"]["numa_policy"] = "single_numa"
    with pytest.raises(agent.NodeAgentError, match="DPDK_CPU_MANAGER_STATIC_REQUIRED"):
        agent.validate_network_profile(profile)

    profile["data_plane"]["cpu_manager_policy"] = "static"
    result = agent.validate_network_profile(profile)
    assert result["dpdk_requested"] is True


def test_cluster_plan_includes_validated_network_profile_hash():
    spec = {
        "cluster_name": "edge-lab",
        "kubernetes_version": "v1.32.2",
        "control_plane_replicas": 3,
        "worker_replicas": 2,
        "network_profile": valid_dual_network_profile(),
    }

    result = agent.execute_command(
        {"command_type": "cluster.plan", "payload": {"cluster_spec": spec}},
        "eph_node",
    )

    assert result["network_profile"]["valid"] is True
    assert result["network_profile"]["pod_traffic_default"] == "data_plane"
    assert result["network_profile"]["profile_hash"] == agent.canonical_hash(
        valid_dual_network_profile()
    )


def test_network_inspection_verifies_expected_multus_attachments_on_workload_pods(
    monkeypatch,
):
    def fake_get(path):
        if path == "/version":
            return {"gitVersion": "v1.32.2"}
        if path == "/apis":
            return {
                "groups": [
                    {
                        "name": "k8s.cni.cncf.io",
                        "preferredVersion": {"version": "v1"},
                    }
                ]
            }
        raise AssertionError(path)

    def fake_list(path, max_items=100000):
        if path in {
            "/api/v1/nodes",
            "/api/v1/namespaces/kube-system/pods",
            "/api/v1/namespaces/ai-agent-inventory/configmaps",
        }:
            return []
        if path == "/apis/k8s.cni.cncf.io/v1/network-attachment-definitions":
            return [
                {
                    "metadata": {"name": "sriov-data", "namespace": "app-ns"},
                    "spec": {"config": json.dumps({"type": "sriov"})},
                }
            ]
        if path == "/api/v1/namespaces/app-ns/pods":
            return [
                {
                    "metadata": {
                        "name": "api-0",
                        "annotations": {
                            "k8s.v1.cni.cncf.io/network-status": json.dumps(
                                [
                                    {
                                        "name": "app-ns/calico",
                                        "interface": "eth0",
                                        "ips": ["10.244.1.10"],
                                        "default": True,
                                    },
                                    {
                                        "name": "app-ns/sriov-data",
                                        "interface": "net1",
                                        "ips": ["192.0.2.10"],
                                    },
                                ]
                            )
                        },
                    },
                    "spec": {"nodeName": "worker-01"},
                    "status": {"phase": "Running"},
                },
                {
                    "metadata": {
                        "name": "api-1",
                        "annotations": {
                            "k8s.v1.cni.cncf.io/network-status": json.dumps(
                                [
                                    {
                                        "name": "app-ns/calico",
                                        "interface": "eth0",
                                        "ips": ["10.244.1.11"],
                                        "default": True,
                                    }
                                ]
                            )
                        },
                    },
                    "spec": {"nodeName": "worker-02"},
                    "status": {"phase": "Running"},
                },
            ]
        raise AssertionError(path)

    monkeypatch.setattr(agent, "kubernetes_api_get", fake_get)
    monkeypatch.setattr(agent, "list_all_kubernetes_items", fake_list)
    monkeypatch.setattr(agent, "_pod_network_namespace_snapshot", lambda: {})

    result = agent.execute_command(
        {
            "command_type": "cluster.network.inspect",
            "payload": {
                "workload_namespaces": ["app-ns"],
                "expected_attachments": ["app-ns/sriov-data"],
            },
        },
        "target_cluster",
    )

    namespace_result = result["workload_namespace_results"][0]
    assert namespace_result["inspection_status"] == "observed"
    assert namespace_result["pod_count"] == 2
    assert result["missing_expected_network_attachment_definitions"] == []
    assert namespace_result["pods_missing_expected_attachments"] == ["api-1"]
    assert namespace_result["pods"][0]["attachments_valid"] is True
    assert namespace_result["pods"][0]["networks"][1]["interface"] == "net1"
    assert namespace_result["pods"][1]["attachments_valid"] is False
    assert result["workload_attachment_validation"]["complete"] is True
    assert result["workload_attachment_validation"]["passed"] is False



@pytest.mark.parametrize(
    ("network_status", "expected_pass"),
    [
        (
            [
                {
                    "name": "app-ns/sriov-data",
                    "interface": "eth0",
                    "ips": ["192.0.2.20"],
                    "default": True,
                }
            ],
            True,
        ),
        (
            [
                {
                    "name": "app-ns/calico",
                    "interface": "eth0",
                    "ips": ["10.244.1.20"],
                    "default": True,
                },
                {
                    "name": "app-ns/sriov-data",
                    "interface": "net1",
                    "ips": ["192.0.2.20"],
                },
            ],
            False,
        ),
        (
            [
                {
                    "name": "app-ns/sriov-data",
                    "interface": "eth0",
                    "ips": ["192.0.2.20"],
                    "default": True,
                },
                {
                    "name": "app-ns/ovs-extra",
                    "interface": "net1",
                    "ips": ["198.51.100.20"],
                    "default": True,
                },
            ],
            False,
        ),
    ],
)
def test_network_inspection_validates_pod_data_plane_default_network(
    monkeypatch,
    network_status,
    expected_pass,
):
    def fake_get(path):
        if path == "/version":
            return {"gitVersion": "v1.32.2"}
        if path == "/apis":
            return {"groups": []}
        raise AssertionError(path)

    def fake_list(path, max_items=100000):
        if path in {
            "/api/v1/nodes",
            "/api/v1/namespaces/kube-system/pods",
        }:
            return []
        if path == "/api/v1/namespaces/app-ns/pods":
            return [
                {
                    "metadata": {
                        "name": "app-0",
                        "annotations": {
                            "k8s.v1.cni.cncf.io/network-status": json.dumps(
                                network_status
                            )
                        },
                    },
                    "spec": {"nodeName": "worker-01"},
                    "status": {"phase": "Running"},
                }
            ]
        raise AssertionError(path)

    monkeypatch.setattr(agent, "kubernetes_api_get", fake_get)
    monkeypatch.setattr(agent, "list_all_kubernetes_items", fake_list)
    monkeypatch.setattr(agent, "_pod_network_namespace_snapshot", lambda: {})

    result = agent.inspect_network_environment(
        {
            "workload_namespaces": ["app-ns"],
            "require_data_plane_default": True,
            "expected_default_network": "app-ns/sriov-data",
        }
    )

    pod = result["workload_namespace_results"][0]["pods"][0]
    validation = result["pod_default_network_validation"]
    assert pod["default_network_valid"] is expected_pass
    assert validation["requested"] is True
    assert validation["complete"] is True
    assert validation["passed"] is expected_pass



@pytest.mark.parametrize(
    ("routes", "expected_pass"),
    [
        ([{"interface": "net1", "gateway": "192.0.2.1"}], True),
        ([{"interface": "eth0", "gateway": "10.244.0.1"}], False),
        ([{"interface": "net1", "gateway": "192.0.2.254"}], False),
        ([], False),
    ],
)
def test_network_inspection_validates_agent_pod_default_route(
    monkeypatch,
    routes,
    expected_pass,
):
    def fake_get(path):
        if path == "/version":
            return {"gitVersion": "v1.32.2"}
        if path == "/apis":
            return {"groups": []}
        raise AssertionError(path)

    def fake_list(path, max_items=100000):
        if path in {
            "/api/v1/nodes",
            "/api/v1/namespaces/kube-system/pods",
        }:
            return []
        raise AssertionError(path)

    monkeypatch.setattr(agent, "kubernetes_api_get", fake_get)
    monkeypatch.setattr(agent, "list_all_kubernetes_items", fake_list)
    monkeypatch.setattr(
        agent,
        "_pod_network_namespace_snapshot",
        lambda: {
            "scope": "agent_pod_network_namespace",
            "interfaces": ["eth0", "net1"],
            "default_routes": routes,
        },
    )

    result = agent.inspect_network_environment(
        {
            "expected_agent_default_interface": "net1",
            "expected_agent_default_gateway": "192.0.2.1",
        }
    )

    validation = result["agent_pod_default_route_validation"]
    assert validation["requested"] is True
    assert validation["scope"] == "agent_pod_network_namespace"
    assert validation["passed"] is expected_pass


def test_network_inspection_rejects_invalid_expected_agent_default_route():
    with pytest.raises(
        agent.NodeAgentError,
        match="INVALID_EXPECTED_AGENT_DEFAULT_GATEWAY",
    ):
        agent.inspect_network_environment(
            {
                "expected_agent_default_interface": "net1",
                "expected_agent_default_gateway": 123,
            }
        )



def test_network_inspection_requires_namespace_scope_for_default_network_check():
    with pytest.raises(
        agent.NodeAgentError,
        match="WORKLOAD_NAMESPACES_REQUIRED_FOR_DEFAULT_NETWORK_CHECK",
    ):
        agent.inspect_network_environment(
            {
                "require_data_plane_default": True,
                "expected_default_network": "data-plane",
            }
        )


def test_network_inspection_requires_namespace_scope_for_workload_checks():
    with pytest.raises(
        agent.NodeAgentError,
        match="WORKLOAD_NAMESPACES_REQUIRED_FOR_ATTACHMENT_CHECK",
    ):
        agent.inspect_network_environment({"expected_attachments": ["app-ns/sriov-data"]})


def test_network_inspection_reports_namespace_rbac_denial_without_failing_global_inventory(
    monkeypatch,
):
    def fake_get(path):
        if path == "/version":
            return {"gitVersion": "v1.32.2"}
        if path == "/apis":
            return {"groups": []}
        raise AssertionError(path)

    def fake_list(path, max_items=100000):
        if path in {"/api/v1/nodes", "/api/v1/namespaces/kube-system/pods"}:
            return []
        if path == "/api/v1/namespaces/restricted/pods":
            raise agent.NodeAgentError("KUBERNETES_API_HTTP_403")
        raise AssertionError(path)

    monkeypatch.setattr(agent, "kubernetes_api_get", fake_get)
    monkeypatch.setattr(agent, "list_all_kubernetes_items", fake_list)
    monkeypatch.setattr(agent, "_pod_network_namespace_snapshot", lambda: {})

    result = agent.inspect_network_environment({"workload_namespaces": ["restricted"]})

    assert result["workload_namespace_results"] == [
        {
            "namespace": "restricted",
            "inspection_status": "unavailable",
            "error_code": "KUBERNETES_API_HTTP_403",
        }
    ]
    assert result["workload_attachment_validation"]["complete"] is False
    assert result["workload_attachment_validation"]["passed"] is None


def test_host_sysfs_inspection_reports_nic_sriov_pci_numa_and_hugepages(
    monkeypatch,
    tmp_path,
):
    root = tmp_path / "sys"
    interface = root / "class" / "net" / "eno1"
    pci = root / "bus" / "pci" / "devices" / "0000:03:00.0"
    driver = root / "bus" / "pci" / "drivers" / "ixgbe"
    hugepages = root / "kernel" / "mm" / "hugepages" / "hugepages-2Mi"
    numa = root / "devices" / "system" / "node" / "node0"
    node_hugepages = numa / "hugepages" / "hugepages-2Mi"
    for directory in [interface, pci / "net", driver, hugepages, node_hugepages]:
        directory.mkdir(parents=True, exist_ok=True)
    for path, value in [
        (interface / "address", "00:11:22:33:44:55"),
        (interface / "mtu", "9000"),
        (interface / "operstate", "up"),
        (pci / "vendor", "0x8086"),
        (pci / "device", "0x1572"),
        (pci / "class", "0x020000"),
        (pci / "numa_node", "0"),
        (pci / "sriov_totalvfs", "64"),
        (pci / "sriov_numvfs", "8"),
        (hugepages / "nr_hugepages", "512"),
        (hugepages / "free_hugepages", "480"),
        (node_hugepages / "nr_hugepages", "512"),
        (node_hugepages / "free_hugepages", "480"),
        (root / "devices" / "system" / "cpu" / "online", "0-31"),
        (root / "class" / "dmi" / "id" / "sys_vendor", "Example Systems"),
        (root / "class" / "dmi" / "id" / "product_name", "Rack Server"),
        (root / "class" / "dmi" / "id" / "product_uuid", "11111111-2222-3333-4444-555555555555"),
        (root / "class" / "dmi" / "id" / "product_serial", "SYS-SERIAL-001"),
        (root / "class" / "dmi" / "id" / "board_serial", "BOARD-SERIAL-001"),
        (root / "class" / "dmi" / "id" / "chassis_serial", "CHASSIS-SERIAL-001"),
        (numa / "cpulist", "0-31"),
    ]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="ascii")
    (pci / "net" / "eno1").mkdir()
    (pci / "driver").symlink_to(driver, target_is_directory=True)
    (pci / "iommu_group").symlink_to(pci, target_is_directory=True)
    (pci / "virtfn0").symlink_to(pci, target_is_directory=True)
    vf = root / "bus" / "pci" / "devices" / "0000:03:00.1"
    vf_driver = root / "bus" / "pci" / "drivers" / "vfio-pci"
    vf.mkdir(parents=True)
    vf_driver.mkdir(parents=True)
    for name, value in [("vendor", "0x8086"), ("device", "0x1572"), ("class", "0x020000")]:
        (vf / name).write_text(value, encoding="ascii")
    (vf / "physfn").symlink_to(pci, target_is_directory=True)
    (vf / "driver").symlink_to(vf_driver, target_is_directory=True)
    (interface / "device").symlink_to(
        "../../../bus/pci/devices/0000:03:00.0",
        target_is_directory=True,
    )
    monkeypatch.setenv("AGENT_HOST_SYSFS_PATH", str(root))

    result = agent.inspect_host_network_hardware()

    assert result["status"] == "observed"
    assert result["scope"] == "host_sysfs_read_only"
    assert result["system_identity"]["product_uuid"] == "11111111-2222-3333-4444-555555555555"
    assert result["system_identity"]["product_serial"] == "SYS-SERIAL-001"
    assert result["system_identity"]["identity_material_present"] is True
    assert result["system_identity"]["attestation"] == "unverified_agent_report"
    assert result["cpu_online"] == "0-31"
    assert result["interfaces"][0]["name"] == "eno1"
    assert result["interfaces"][0]["pci_address"] == "0000:03:00.0"
    assert result["interfaces"][0]["sriov_total_vfs"] == "64"
    assert result["pci_network_devices"][0]["driver"] == "ixgbe"
    assert result["pci_network_devices"][0]["iommu_group_present"] is True
    assert result["pci_network_devices"][1]["is_virtual_function"] is True
    assert result["pci_network_devices"][1]["driver"] == "vfio-pci"
    assert result["pci_network_devices"][0]["is_virtual_function"] is False
    assert result["pci_network_devices"][0]["virtual_function_count"] == 1
    assert result["hugepages"][0]["free"] == "480"
    assert result["numa_nodes"][0]["cpulist"] == "0-31"


def test_host_sysfs_inspection_is_explicitly_unavailable_without_mount(monkeypatch):
    monkeypatch.delenv("AGENT_HOST_SYSFS_PATH", raising=False)

    result = agent.inspect_host_network_hardware()

    assert result["status"] == "not_configured"
    assert result["scope"] == "host_sysfs"


def test_target_agent_aggregates_only_valid_per_node_hardware_reports(monkeypatch):
    reports = [
        {
            "metadata": {
                "name": agent.network_inventory_configmap_name("worker-01"),
                "labels": {"app.kubernetes.io/name": "network-inventory-agent"}
            },
            "data": {
                "inventory.json": json.dumps(
                    {
                        "schema_version": "v1",
                        "node_name": "worker-01",
                        "node_uid": "node-uid-01",
                        "collected_at": datetime.datetime.now(datetime.UTC).isoformat(),
                        "hardware": {
                            "status": "observed",
                            "interfaces": [{"name": "eno1"}],
                            "pci_network_devices": [{"pci_address": "0000:03:00.0"}],
                            "hugepages": [{"page_size": "hugepages-2Mi", "free": "512"}],
                            "numa_nodes": [{"node": "node0"}],
                        },
                    }
                )
            },
        },
        {
            "metadata": {
                "labels": {"app.kubernetes.io/name": "network-inventory-agent"}
            },
            "data": {"inventory.json": "{invalid-json"},
        },
        {
            "metadata": {"labels": {"app.kubernetes.io/name": "unrelated-configmap"}},
            "data": {},
        },
    ]

    monkeypatch.setattr(
        agent,
        "list_all_kubernetes_items",
        lambda path, max_items=100000: reports,
    )

    result = agent.inspect_cluster_node_hardware_inventory(
        [{"metadata": {"name": "worker-01", "uid": "node-uid-01"}}]
    )

    assert result["status"] == "observed"
    assert result["node_count"] == 1
    assert result["inventory_complete"] is True
    assert result["missing_node_names"] == []
    assert result["nodes"][0]["node_name"] == "worker-01"
    assert result["nodes"][0]["interfaces"][0]["name"] == "eno1"
    assert result["nodes"][0]["pci_network_devices"][0]["pci_address"] == "0000:03:00.0"


def test_target_agent_does_not_accept_stale_hardware_inventory_reports(monkeypatch):
    report = {
        "schema_version": "v1",
        "node_name": "worker-old",
        "node_uid": "node-uid-old",
        "collected_at": "2020-01-01T00:00:00+00:00",
        "hardware": {"status": "observed", "interfaces": []},
    }
    configmaps = [
        {
            "metadata": {
                "name": agent.network_inventory_configmap_name("worker-old"),
                "labels": {"app.kubernetes.io/name": "network-inventory-agent"}
            },
            "data": {"inventory.json": json.dumps(report)},
        }
    ]
    monkeypatch.setattr(
        agent,
        "list_all_kubernetes_items",
        lambda path, max_items=100000: configmaps,
    )

    result = agent.inspect_cluster_node_hardware_inventory(
        [{"metadata": {"name": "worker-old", "uid": "node-uid-old"}}]
    )

    assert result["node_count"] == 0
    assert result["inventory_complete"] is False
    assert result["missing_node_names"] == ["worker-old"]
    assert result["stale_node_names"] == ["worker-old"]



def test_worker_provisioning_preflight_is_target_agent_only():
    with pytest.raises(agent.NodeAgentError, match="CAPABILITY_NOT_CONFIGURED"):
        agent.execute_command(
            {"command_type": "cluster.capi.worker.preflight", "payload": {}},
            "eph_node",
        )


def test_target_worker_provisioning_preflight_requires_templates_hosts_and_dual_network(
    monkeypatch,
):
    profile = valid_dual_network_profile()
    profile["data_plane"]["hugepages"] = "1Gi"
    profile["data_plane"]["numa_policy"] = "single-numa-node"
    profile["data_plane"]["cpu_manager_policy"] = "static"
    profile["data_plane"]["cni"]["attachments"][1]["dpdk"] = True
    groups = {
        "cluster.x-k8s.io": (
            "v1beta1",
            ["clusters", "machines", "machinedeployments"],
        ),
        "bootstrap.cluster.x-k8s.io": ("v1beta1", ["kubeadmconfigtemplates"]),
        "infrastructure.cluster.x-k8s.io": (
            "v1beta1",
            ["metal3machines", "metal3machinetemplates",
             "metal3datatemplates", "metal3datas"],
        ),
        "metal3.io": ("v1alpha1", ["baremetalhosts"]),
    }
    calls = []

    def fake_get(path):
        calls.append(path)
        if path == "/apis":
            return {"groups": [
                {"name": name, "preferredVersion": {"version": version}}
                for name, (version, _) in groups.items()
            ]}
        for name, (version, resources) in groups.items():
            if path == f"/apis/{name}/{version}":
                return {"resources": [{"name": item} for item in resources]}
        if path.startswith("/apis/apps/v1/namespaces/") and "/deployments/" in path:
            deployment_name = path.rsplit("/", 1)[-1]
            return {
                "metadata": {"name": deployment_name, "generation": 1},
                "spec": {"replicas": 1},
                "status": {"availableReplicas": 1, "observedGeneration": 1},
            }
        if path.startswith("/apis/apps/v1/namespaces/") and "/deployments/" in path:
            deployment_name = path.rsplit("/", 1)[-1]
            return {
                "metadata": {"name": deployment_name, "generation": 1},
                "spec": {"replicas": 1},
                "status": {"availableReplicas": 1, "observedGeneration": 1},
            }
        if path.endswith("/clusters/edge-lab"):
            return {"metadata": {"uid": "cluster-uid-1"}}
        if path.endswith("/machinedeployments/edge-workers"):
            raise agent.NodeAgentError("KUBERNETES_API_HTTP_404")
        if path.endswith("/metal3datatemplates/worker-network-template"):
            return valid_metal3_data_template(profile)
        if path.endswith("/metal3machinetemplates/worker-template"):
            return {
                "apiVersion": TEST_INFRA_API_VERSION,
                "metadata": {
                    "name": "worker-template",
                    "namespace": "default",
                    "uid": "infra-template-uid",
                    "annotations": {
                        "ai-agent-stack.io/network-profile-hash": (
                            agent.canonical_hash(profile)
                        )
                    },
                },
                "spec": {
                    "template": {
                        "spec": {"hostSelector": {"matchLabels": {"pool": "workers"}},
"dataTemplate": {
    "name": "worker-network-template",
    "namespace": "default",
}}
                    }
                },
            }
        if path.endswith("/kubeadmconfigtemplates/worker-bootstrap"):
            return {
                "apiVersion": "bootstrap.cluster.x-k8s.io/v1beta1",
                "metadata": {
                    "name": "worker-bootstrap",
                    "namespace": "default",
                    "uid": "bootstrap-template-uid",
                },
            }
        if "/baremetalhosts?" in path or path.endswith("/baremetalhosts"):
            return {"items": [
                {"metadata": {"name": "bmh-01", "uid": "bmh-uid-01",
                              "labels": {"pool": "workers"}},
                 "spec": {"online": True},
                 "status": {"operationalStatus": "OK", "provisioning": {"state": "available"}}},
                {"metadata": {"name": "bmh-02", "uid": "bmh-uid-02",
                              "labels": {"pool": "workers"}},
                 "spec": {"online": True},
                 "status": {"operationalStatus": "OK",
                            "provisioning": {"state": "available"}}},
            ], "metadata": {}}
        raise AssertionError(path)

    monkeypatch.setattr(agent, "kubernetes_api_get", fake_get)
    result = agent.execute_command(
        {"command_type": "cluster.capi.worker.preflight", "payload": {
            "cluster_name": "edge-lab",
            "namespace": "default",
            "expected_worker_replicas": 2,
            "worker_provisioning": {
                "machine_deployment_name": "edge-workers",
                "infrastructure_template_name": "worker-template",
                "bootstrap_template_name": "worker-bootstrap",
                "baremetal_host_namespace": "metal3-system",
                "host_selector": {"pool": "workers"},
                "network_profile": profile,
            },
        }},
        "target_cluster",
    )

    assert result["ready"] is True
    assert result["execution_allowed"] is False
    assert result["worker_creation_performed"] is False
    assert result["available_baremetal_host_count"] == 2
    assert result["network_profile"]["dpdk_requested"] is True
    assert result["side_effects"] is False
    assert not any("POST" in call or "PATCH" in call for call in calls)


def test_target_worker_provisioning_preflight_rejects_insufficient_baremetal_hosts(monkeypatch):
    profile = valid_dual_network_profile()
    groups = {
        "cluster.x-k8s.io": (
            "v1beta1",
            ["clusters", "machines", "machinedeployments"],
        ),
        "bootstrap.cluster.x-k8s.io": ("v1beta1", ["kubeadmconfigtemplates"]),
        "infrastructure.cluster.x-k8s.io": (
            "v1beta1",
            ["metal3machines", "metal3machinetemplates",
             "metal3datatemplates", "metal3datas"],
        ),
        "metal3.io": ("v1alpha1", ["baremetalhosts"]),
    }

    def fake_get(path):
        if path == "/apis":
            return {"groups": [
                {"name": name, "preferredVersion": {"version": version}}
                for name, (version, _) in groups.items()
            ]}
        for name, (version, resources) in groups.items():
            if path == f"/apis/{name}/{version}":
                return {"resources": [{"name": item} for item in resources]}
        if path.startswith("/apis/apps/v1/namespaces/") and "/deployments/" in path:
            deployment_name = path.rsplit("/", 1)[-1]
            return {
                "metadata": {"name": deployment_name, "generation": 1},
                "spec": {"replicas": 1},
                "status": {"availableReplicas": 1, "observedGeneration": 1},
            }
        if path.endswith("/clusters/edge-lab"):
            return {"metadata": {"uid": "cluster-uid-1"}}
        if path.endswith("/machinedeployments/edge-workers"):
            raise agent.NodeAgentError("KUBERNETES_API_HTTP_404")
        if path.endswith("/metal3datatemplates/worker-network-template"):
            return valid_metal3_data_template(profile)
        if path.endswith("/metal3machinetemplates/worker-template"):
            return {
                "metadata": {
                    "name": "worker-template",
                    "namespace": "default",
                    "uid": "infra-template-uid",
                    "annotations": {
                        "ai-agent-stack.io/network-profile-hash": (
                            agent.canonical_hash(profile)
                        )
                    },
                },
                "spec": {
                    "template": {
                        "spec": {"hostSelector": {"matchLabels": {"pool": "workers"}},
"dataTemplate": {
    "name": "worker-network-template",
    "namespace": "default",
}}
                    }
                },
            }
        if path.endswith("/kubeadmconfigtemplates/worker-bootstrap"):
            return {
                "metadata": {
                    "name": "worker-bootstrap",
                    "namespace": "default",
                    "uid": "bootstrap-template-uid",
                },
            }
        if "/baremetalhosts?" in path or path.endswith("/baremetalhosts"):
            return {"items": [{
                "metadata": {"name": "bmh-01", "uid": "bmh-uid-01",
                             "labels": {"pool": "workers"}},
                "spec": {"online": True},
                "status": {"operationalStatus": "OK", "provisioning": {"state": "available"}},
            }], "metadata": {}}
        raise AssertionError(path)

    monkeypatch.setattr(agent, "kubernetes_api_get", fake_get)
    result = agent.execute_command(
        {"command_type": "cluster.capi.worker.preflight", "payload": {
            "cluster_name": "edge-lab",
            "namespace": "default",
            "expected_worker_replicas": 2,
            "worker_provisioning": {
                "machine_deployment_name": "edge-workers",
                "infrastructure_template_name": "worker-template",
                "bootstrap_template_name": "worker-bootstrap",
                "baremetal_host_namespace": "metal3-system",
                "host_selector": {"pool": "workers"},
                "network_profile": profile,
            },
        }},
        "target_cluster",
    )

    assert result["ready"] is False
    assert result["execution_allowed"] is False
    assert "insufficient_available_baremetal_hosts" in result["blockers"]


def test_target_worker_provisioning_preflight_rejects_bmh_error_message(monkeypatch):
    profile = valid_dual_network_profile()
    groups = {
        "cluster.x-k8s.io": (
            "v1beta1",
            ["clusters", "machines", "machinedeployments"],
        ),
        "bootstrap.cluster.x-k8s.io": ("v1beta1", ["kubeadmconfigtemplates"]),
        "infrastructure.cluster.x-k8s.io": (
            "v1beta1",
            ["metal3machines", "metal3machinetemplates",
             "metal3datatemplates", "metal3datas"],
        ),
        "metal3.io": ("v1alpha1", ["baremetalhosts"]),
    }

    def fake_get(path):
        if path == "/apis":
            return {"groups": [
                {"name": name, "preferredVersion": {"version": version}}
                for name, (version, _) in groups.items()
            ]}
        for name, (version, resources) in groups.items():
            if path == f"/apis/{name}/{version}":
                return {"resources": [{"name": item} for item in resources]}
        if path.startswith("/apis/apps/v1/namespaces/") and "/deployments/" in path:
            deployment_name = path.rsplit("/", 1)[-1]
            return {
                "metadata": {"name": deployment_name, "generation": 1},
                "spec": {"replicas": 1},
                "status": {"availableReplicas": 1, "observedGeneration": 1},
            }
        if path.endswith("/clusters/edge-lab"):
            return {"metadata": {"uid": "cluster-uid-1"}}
        if path.endswith("/machinedeployments/edge-workers"):
            raise agent.NodeAgentError("KUBERNETES_API_HTTP_404")
        if path.endswith("/metal3datatemplates/worker-network-template"):
            return valid_metal3_data_template(profile)
        if path.endswith("/metal3machinetemplates/worker-template"):
            return {
                "apiVersion": TEST_INFRA_API_VERSION,
                "metadata": {
                    "name": "worker-template",
                    "namespace": "default",
                    "uid": "infra-template-uid",
                    "annotations": {
                        "ai-agent-stack.io/network-profile-hash": (
                            agent.canonical_hash(profile)
                        )
                    },
                },
                "spec": {
                    "template": {
                        "spec": {"hostSelector": {"matchLabels": {"pool": "workers"}},
"dataTemplate": {
    "name": "worker-network-template",
    "namespace": "default",
}}
                    }
                },
            }
        if path.endswith("/kubeadmconfigtemplates/worker-bootstrap"):
            return {
                "apiVersion": "bootstrap.cluster.x-k8s.io/v1beta1",
                "metadata": {
                    "name": "worker-bootstrap",
                    "namespace": "default",
                    "uid": "bootstrap-template-uid",
                },
            }
        if "/baremetalhosts" in path:
            return {"items": [
                {
                    "metadata": {
                        "name": "bmh-01",
                        "uid": "bmh-uid-01",
                        "labels": {"pool": "workers"},
                    },
                    "spec": {"online": True},
                    "status": {
                        "operationalStatus": "error",
                        "errorMessage": "BMC inspection failed",
                        "provisioning": {"state": "available"},
                    },
                },
                {
                    "metadata": {
                        "name": "bmh-02",
                        "uid": "bmh-uid-02",
                        "labels": {"pool": "workers"},
                    },
                    "spec": {"online": True},
                    "status": {
                        "operationalStatus": "OK",
                        "provisioning": {"state": "available"},
                    },
                },
            ], "metadata": {}}
        raise AssertionError(path)

    monkeypatch.setattr(agent, "kubernetes_api_get", fake_get)
    result = agent.execute_command(
        {
            "command_type": "cluster.capi.worker.preflight",
            "payload": {
                "cluster_name": "edge-lab",
                "namespace": "default",
                "expected_worker_replicas": 2,
                "worker_provisioning": {
                    "machine_deployment_name": "edge-workers",
                    "infrastructure_template_name": "worker-template",
                    "bootstrap_template_name": "worker-bootstrap",
                    "baremetal_host_namespace": "metal3-system",
                    "host_selector": {"pool": "workers"},
                    "network_profile": profile,
                },
            },
        },
        "target_cluster",
    )

    assert result["ready"] is False
    assert result["available_baremetal_host_count"] == 1
    assert "insufficient_available_baremetal_hosts" in result["blockers"]
    assert "matching_baremetal_host_operational_error" in result["blockers"]
    assert result["side_effects"] is False
    assert result["execution_allowed"] is False
    assert result["worker_creation_performed"] is False

def test_verify_baremetal_host_network_macs_matches_consumer_and_hardware(monkeypatch):
    monkeypatch.setattr(
        agent,
        "kubernetes_api_get",
        lambda path: (_ for _ in ()).throw(agent.NodeAgentError("KUBERNETES_API_HTTP_404")),
    )
    profile = agent.validate_network_profile(valid_dual_network_profile())
    network_data = {
        "links": [
            {"id": "eno1", "ethernet_mac_address": "02:00:00:00:00:01"},
            {"id": "ens2f0", "ethernet_mac_address": "02:00:00:00:00:02"},
        ]
    }
    hosts = [{
        "metadata": {"name": "bmh-01", "namespace": "metal3-system", "uid": "bmh-uid-01"},
        "spec": {"consumerRef": {
            "apiVersion": TEST_INFRA_API_VERSION,
            "kind": "Metal3Machine",
            "name": "worker-infra-1",
            "namespace": "default",
        }},
        "status": {"hardware": {"nics": [
            {"name": "eno1", "mac": "02:00:00:00:00:01"},
            {"name": "ens2f0", "mac": "02:00:00:00:00:02"},
        ]}},
    }]

    result = agent.verify_baremetal_host_network_macs(
        profile,
        network_data,
        hosts,
        "worker-infra-1",
        "default",
        "metal3-system",
        "v1alpha1",
        TEST_INFRA_API_VERSION,
    )

    assert result["ready"] is True
    assert result["baremetal_host_name"] == "bmh-01"
    assert result["verified_interface_names"] == ["eno1", "ens2f0"]


def test_verify_baremetal_host_network_macs_fails_on_mismatch(monkeypatch):
    monkeypatch.setattr(
        agent,
        "kubernetes_api_get",
        lambda path: (_ for _ in ()).throw(agent.NodeAgentError("KUBERNETES_API_HTTP_404")),
    )
    profile = agent.validate_network_profile(valid_dual_network_profile())
    network_data = {
        "links": [
            {"id": "eno1", "ethernet_mac_address": "02:00:00:00:00:01"},
            {"id": "ens2f0", "ethernet_mac_address": "02:00:00:00:00:99"},
        ]
    }
    hosts = [{
        "metadata": {"name": "bmh-01", "namespace": "metal3-system", "uid": "bmh-uid-01"},
        "spec": {"consumerRef": {
            "apiVersion": TEST_INFRA_API_VERSION,
            "kind": "Metal3Machine",
            "name": "worker-infra-1",
            "namespace": "default",
        }},
        "status": {"hardware": {"nics": [
            {"name": "eno1", "mac": "02:00:00:00:00:01"},
            {"name": "ens2f0", "mac": "02:00:00:00:00:02"},
        ]}},
    }]

    with pytest.raises(agent.NodeAgentError, match="WORKER_RENDERED_NETWORK_MAC_MISMATCH"):
        agent.verify_baremetal_host_network_macs(
            profile,
        network_data,
        hosts,
        "worker-infra-1",
        "default",
        "metal3-system",
        "v1alpha1",
        TEST_INFRA_API_VERSION,
        )


def test_verify_baremetal_host_network_macs_waits_for_hardware_inventory(monkeypatch):
    monkeypatch.setattr(
        agent,
        "kubernetes_api_get",
        lambda path: (_ for _ in ()).throw(agent.NodeAgentError("KUBERNETES_API_HTTP_404")),
    )
    profile = agent.validate_network_profile(valid_dual_network_profile())
    hosts = [{
        "metadata": {"name": "bmh-01", "namespace": "metal3-system", "uid": "bmh-uid-01"},
        "spec": {"consumerRef": {
            "apiVersion": TEST_INFRA_API_VERSION,
            "kind": "Metal3Machine",
            "name": "worker-infra-1",
            "namespace": "default",
        }},
        "status": {"hardware": {"nics": []}},
    }]

    result = agent.verify_baremetal_host_network_macs(
        profile,
        {"links": []},
        hosts,
        "worker-infra-1",
        "default",
        "metal3-system",
        "v1alpha1",
        TEST_INFRA_API_VERSION,
    )

    assert result["ready"] is False
    assert result["reason"] == "baremetal_host_hardware_inventory_pending"


def test_verify_baremetal_host_network_macs_rejects_ambiguous_consumer(monkeypatch):
    monkeypatch.setattr(
        agent,
        "kubernetes_api_get",
        lambda path: (_ for _ in ()).throw(agent.NodeAgentError("KUBERNETES_API_HTTP_404")),
    )
    profile = agent.validate_network_profile(valid_dual_network_profile())
    host = {
        "metadata": {"name": "bmh-01", "namespace": "metal3-system", "uid": "bmh-uid-01"},
        "spec": {"consumerRef": {
            "apiVersion": TEST_INFRA_API_VERSION,
            "kind": "Metal3Machine",
            "name": "worker-infra-1",
            "namespace": "default",
        }},
    }

    with pytest.raises(
        agent.NodeAgentError,
        match="WORKER_BAREMETAL_HOST_CONSUMER_REFERENCE_AMBIGUOUS",
    ):
        agent.verify_baremetal_host_network_macs(
            profile,
            {"links": []},
            [host, dict(host)],
            "worker-infra-1",
            "default",
            "metal3-system",
            "v1alpha1",
            TEST_INFRA_API_VERSION,
        )


def test_verify_baremetal_host_network_macs_reads_owned_hardware_data(monkeypatch):
    profile = agent.validate_network_profile(valid_dual_network_profile())
    network_data = {
        "links": [
            {"id": "eno1", "ethernet_mac_address": "02:00:00:00:00:01"},
            {"id": "ens2f0", "ethernet_mac_address": "02:00:00:00:00:02"},
        ]
    }
    host = {
        "metadata": {
            "name": "bmh-01",
            "namespace": "metal3-system",
            "uid": "bmh-uid-01",
        },
        "spec": {"consumerRef": {
            "apiVersion": TEST_INFRA_API_VERSION,
            "kind": "Metal3Machine",
            "name": "worker-infra-1",
            "namespace": "default",
        }},
        "status": {"hardware": {"nics": [
            {"name": "eno1", "mac": "02:00:00:00:00:99"},
            {"name": "ens2f0", "mac": "02:00:00:00:00:98"},
        ]}},
    }
    requested = []

    def fake_get(path):
        requested.append(path)
        return {
            "apiVersion": "metal3.io/v1alpha1",
            "kind": "HardwareData",
            "metadata": {
                "name": "bmh-01",
                "namespace": "metal3-system",
                "uid": "hardware-data-uid-01",
                "ownerReferences": [{
                    "apiVersion": "metal3.io/v1alpha1",
                    "kind": "BareMetalHost",
                    "name": "bmh-01",
                    "uid": "bmh-uid-01",
                }],
            },
            "spec": {"hardware": {"nics": [
                {"name": "eno1", "mac": "02:00:00:00:00:01"},
                {"name": "ens2f0", "mac": "02:00:00:00:00:02"},
            ]}},
        }

    monkeypatch.setattr(agent, "kubernetes_api_get", fake_get)
    result = agent.verify_baremetal_host_network_macs(
        profile, network_data, [host], "worker-infra-1", "default",
        "metal3-system", "v1alpha1", TEST_INFRA_API_VERSION
    )

    assert result["ready"] is True
    assert requested == [
        "/apis/metal3.io/v1alpha1/namespaces/metal3-system/hardwaredata/bmh-01"
    ]


def test_verify_baremetal_host_network_macs_rejects_unowned_hardware_data(monkeypatch):
    profile = agent.validate_network_profile(valid_dual_network_profile())
    host = {
        "metadata": {
            "name": "bmh-01",
            "namespace": "metal3-system",
            "uid": "bmh-uid-01",
        },
        "spec": {"consumerRef": {
            "apiVersion": TEST_INFRA_API_VERSION,
            "kind": "Metal3Machine",
            "name": "worker-infra-1",
            "namespace": "default",
        }},
        "status": {},
    }
    monkeypatch.setattr(
        agent,
        "kubernetes_api_get",
        lambda path: {
            "apiVersion": "metal3.io/v1alpha1",
            "kind": "HardwareData",
            "metadata": {
                "name": "bmh-01",
                "namespace": "metal3-system",
                "uid": "hardware-data-uid-01",
                "ownerReferences": [{
                    "kind": "BareMetalHost", "name": "bmh-01", "uid": "wrong-bmh-uid"
                }],
            },
            "spec": {"hardware": {"nics": []}},
        },
    )

    with pytest.raises(
        agent.NodeAgentError,
        match="WORKER_HARDWARE_DATA_OWNERSHIP_MISMATCH",
    ):
        agent.verify_baremetal_host_network_macs(
            profile,
            {"links": []},
            [host],
            "worker-infra-1",
            "default",
            "metal3-system",
            "v1alpha1",
            TEST_INFRA_API_VERSION,
        )


def test_approved_worker_provisioning_only_runs_server_side_dry_run(
    monkeypatch,
):
    profile = valid_dual_network_profile()
    profile_hash = agent.canonical_hash(profile)
    versions = {
        "cluster.x-k8s.io": "v1beta1",
        "bootstrap.cluster.x-k8s.io": "v1beta1",
        "infrastructure.cluster.x-k8s.io": "v1beta1",
        "metal3.io": "v1alpha1",
    }
    created_manifests = []
    create_modes = []

    def fake_get(path):
        if path == "/apis":
            return {
                "groups": [
                    {"name": group, "preferredVersion": {"version": version}}
                    for group, version in versions.items()
                ]
            }
        if path == "/apis/infrastructure.cluster.x-k8s.io/v1beta1":
            return {
                "resources": [
                    {"name": name}
                    for name in [
                        "metal3machines",
                        "metal3machinetemplates",
                        "metal3datatemplates",
                        "metal3datas",
                    ]
                ]
            }
        if path.startswith(
            "/apis/infrastructure.cluster.x-k8s.io/v1beta1/namespaces/default/metal3machines/"
        ):
            machine_name = path.rsplit("/", 1)[-1]
            index = int(machine_name.rsplit("-", 1)[-1])
            return {
                "apiVersion": TEST_INFRA_API_VERSION,
                "kind": "Metal3Machine",
                "metadata": {
                    "name": machine_name,
                    "namespace": "default",
                    "uid": f"metal3-machine-uid-{index}",
                    "ownerReferences": [
                        {
                            "apiVersion": "cluster.x-k8s.io/v1beta1",
                            "kind": "Machine",
                            "name": f"worker-machine-{index}",
                            "uid": f"worker-machine-uid-{index}",
                        }
                    ],
                },
                "spec": {
                    "dataTemplate": {
                        "name": "worker-network-template",
                        "namespace": "default",
                    }
                },
                "status": {
                    "renderedData": {
                        "name": f"worker-network-template-{index - 1}",
                        "namespace": "default",
                    },
                    "networkData": {
                        "name": f"worker-machine-{index}-networkdata-0",
                        "namespace": "default",
                    },
                },
            }
        if path.startswith(
            "/apis/infrastructure.cluster.x-k8s.io/v1beta1/namespaces/default/metal3datas/"
        ):
            rendered_name = path.rsplit("/", 1)[-1]
            index = int(rendered_name.rsplit("-", 1)[-1])
            return {
                "apiVersion": TEST_INFRA_API_VERSION,
                "kind": "Metal3Data",
                "metadata": {
                    "name": rendered_name,
                    "namespace": "default",
                    "uid": f"rendered-data-uid-{index}",
                },
                "spec": {
                    "claim": {
                        "name": f"worker-infra-{index + 1}",
                        "namespace": "default",
                    },
                    "template": {
                        "name": "worker-network-template",
                        "namespace": "default",
                    },
                    "networkData": {
                        "name": f"worker-machine-{index + 1}-networkdata-0",
                        "namespace": "default",
                    },
                },
                "status": {"ready": True, "error": False},
            }
        if path.startswith(
            "/apis/metal3.io/v1alpha1/namespaces/metal3-system/hardwaredata/"
        ):
            host_name = path.rsplit("/", 1)[-1]
            index = int(host_name.rsplit("-", 1)[-1])
            return {
                "apiVersion": "metal3.io/v1alpha1",
                "kind": "HardwareData",
                "metadata": {
                    "name": host_name,
                    "namespace": "metal3-system",
                    "uid": f"hardware-data-uid-{index}",
                    "ownerReferences": [{
                        "apiVersion": "metal3.io/v1alpha1",
                        "kind": "BareMetalHost",
                        "name": host_name,
                        "uid": f"bmh-uid-{index}",
                    }],
                },
                "spec": {"hardware": {"nics": [
                    {"name": "eno1", "mac": "02:00:00:00:00:01"},
                    {"name": "ens2f0", "mac": "02:00:00:00:00:02"},
                ]}},
            }
        if path.startswith("/api/v1/namespaces/default/secrets/"):
            secret_name = path.rsplit("/", 1)[-1]
            index = int(secret_name.split("-networkdata-", 1)[1])
            network_data = {
                "links": [
                    {
                        "id": "eno1",
                        "type": "phy",
                        "mtu": 1500,
                        "ethernet_mac_address": "02:00:00:00:00:01",
                    },
                    {
                        "id": "ens2f0",
                        "type": "phy",
                        "mtu": 9000,
                        "ethernet_mac_address": "02:00:00:00:00:02",
                    },
                ]
            }
            encoded = base64.b64encode(
                json.dumps(network_data).encode("utf-8")
            ).decode("ascii")
            return {
                "metadata": {
                    "name": secret_name,
                    "namespace": "default",
                    "uid": f"network-secret-uid-{index}",
                },
                "type": "Opaque",
                "data": {"networkData": encoded},
            }
        if path.startswith("/apis/apps/v1/namespaces/") and "/deployments/" in path:
            deployment_name = path.rsplit("/", 1)[-1]
            return {
                "metadata": {"name": deployment_name, "generation": 1},
                "spec": {"replicas": 1},
                "status": {"availableReplicas": 1, "observedGeneration": 1},
            }
        if path.endswith("/clusters/edge-lab"):
            return {"metadata": {"uid": "cluster-uid-1"}}
        if path.endswith("/metal3datatemplates/worker-network-template"):
            return valid_metal3_data_template(profile)
        if path.endswith("/metal3machinetemplates/worker-template"):
            return {
                "apiVersion": TEST_INFRA_API_VERSION,
                "metadata": {
                    "uid": "infra-template-uid",
                    "annotations": {
                        "ai-agent-stack.io/network-profile-hash": profile_hash
                    },
                },
                "spec": {
                    "template": {
                        "spec": {"hostSelector": {"matchLabels": {"pool": "workers"}},
"dataTemplate": {
    "name": "worker-network-template",
    "namespace": "default",
}}
                    }
                },
            }
        if path.endswith("/kubeadmconfigtemplates/worker-bootstrap"):
            return {
                "apiVersion": "bootstrap.cluster.x-k8s.io/v1beta1",
                "metadata": {"uid": "bootstrap-template-uid"},
            }
        if path.endswith("/machinedeployments/edge-workers"):
            raise agent.NodeAgentError("KUBERNETES_API_HTTP_404")
        if path == "/api/v1/nodes":
            return {
                "items": [
                    {
                        "metadata": {"name": "worker-01"},
                        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    },
                    {
                        "metadata": {"name": "worker-02"},
                        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    },
                ]
            }
        raise AssertionError(path)

    baremetal_host_list_calls = 0

    def fake_list(path, max_items=100000):
        nonlocal baremetal_host_list_calls
        if "/baremetalhosts" in path:
            baremetal_host_list_calls += 1
            hosts = []
            for index in range(1, 3):
                host = {
                    "metadata": {
                        "name": f"bmh-{index}",
                        "namespace": "metal3-system",
                        "uid": f"bmh-uid-{index}",
                        "labels": {"pool": "workers"},
                    },
                    "spec": {"online": True},
                    "status": {
                        "operationalStatus": "OK",
                        "provisioning": {"state": "available"},
                    },
                }
                if baremetal_host_list_calls > 1:
                    host["spec"]["consumerRef"] = {
                        "apiVersion": TEST_INFRA_API_VERSION,
                        "kind": "Metal3Machine",
                        "name": f"worker-infra-{index}",
                        "namespace": "default",
                    }
                    host["status"]["hardware"] = {"nics": [
                        {"name": "eno1", "mac": "02:00:00:00:00:01"},
                        {"name": "ens2f0", "mac": "02:00:00:00:00:02"},
                    ]}
                hosts.append(host)
            return hosts
        if path.endswith("/machines"):
            return [
                {
                    "metadata": {
                        "name": f"worker-machine-{index}",
                        "uid": f"worker-machine-uid-{index}",
                        "labels": {"cluster.x-k8s.io/deployment-name": "edge-workers"},
                    },
                    "spec": {
                        "clusterName": "edge-lab",
                        "infrastructureRef": {
                            "apiVersion": TEST_INFRA_API_VERSION,
                            "kind": "Metal3Machine",
                            "name": f"worker-infra-{index}",
                        },
                    },
                    "status": {
                        "conditions": [{"type": "Ready", "status": "True"}],
                        "nodeRef": {"name": f"worker-{index:02d}"},
                    },
                }
                for index in range(1, 3)
            ]
        raise AssertionError(path)

    def fake_create(version, namespace, manifest, dry_run):
        create_modes.append(dry_run)
        created_manifests.append(manifest)
        metadata = {"name": "edge-workers", "namespace": namespace}
        return {
            "apiVersion": manifest["apiVersion"],
            "kind": "MachineDeployment",
            "metadata": metadata,
        }

    monkeypatch.setattr(agent, "kubernetes_api_get", fake_get)
    monkeypatch.setattr(agent, "list_all_kubernetes_items", fake_list)
    monkeypatch.setattr(
        agent,
        "kubernetes_api_create_machine_deployment",
        fake_create,
    )
    payload = {
        "deployment_id": "123e4567-e89b-12d3-a456-426614174000",
        "cluster_name": "edge-lab",
        "namespace": "default",
        "expected_worker_replicas": 2,
        "kubernetes_version": "v1.32.2",
        "worker_provisioning": {
            "machine_deployment_name": "edge-workers",
            "infrastructure_template_name": "worker-template",
            "bootstrap_template_name": "worker-bootstrap",
            "baremetal_host_namespace": "metal3-system",
            "host_selector": {"pool": "workers"},
            "network_profile": profile,
        },
        "expected_cluster_uid": "cluster-uid-1",
        "expected_cluster_api_version": "cluster.x-k8s.io/v1beta1",
        "expected_infrastructure_template_uid": "infra-template-uid",
        "expected_bootstrap_template_uid": "bootstrap-template-uid",
        "infrastructure_api_version": TEST_INFRA_API_VERSION,
        "bootstrap_api_version": "bootstrap.cluster.x-k8s.io/v1beta1",
    }
    payload["provisioning_plan_hash"] = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    result = agent.execute_command(
        {
            "command_type": "cluster.capi.worker.provision",
            "payload": payload,
        },
        "target_cluster",
    )

    assert create_modes == [True]
    manifest = created_manifests[-1]
    assert manifest["kind"] == "MachineDeployment"
    assert manifest["spec"]["replicas"] == 2
    assert manifest["spec"]["template"]["spec"]["bootstrap"]["configRef"] == {
        "apiVersion": "bootstrap.cluster.x-k8s.io/v1beta1",
        "kind": "KubeadmConfigTemplate",
        "name": "worker-bootstrap",
    }
    assert manifest["spec"]["template"]["spec"]["infrastructureRef"] == {
        "apiVersion": TEST_INFRA_API_VERSION,
        "kind": "Metal3MachineTemplate",
        "name": "worker-template",
    }
    assert result["created"] is False
    assert result["dry_run_passed"] is True
    assert result["execution_mode"] == "server_side_dry_run_simulation"
    assert result["simulation_status"] == "ready_for_manual_execution"
    assert result["execution_allowed"] is False
    assert result["worker_creation_performed"] is False
    assert result["worker_readiness_verified"] is False
    assert result["worker_machine_count"] == 0
    assert result["ready_worker_machine_count"] == 0
    assert result["worker_node_names"] == []
    assert result["machine_deployment_uid"] is None
    assert len(result["machine_deployment_manifest_hash"]) == 64
    assert result["side_effects"] is False



def test_machine_deployment_create_rejects_real_writes(monkeypatch):
    def unexpected_request(*args, **kwargs):
        raise AssertionError("Kubernetes API must not be called")

    monkeypatch.setattr(agent.urllib.request, "urlopen", unexpected_request)
    with pytest.raises(
        agent.NodeAgentError,
        match="MACHINE_DEPLOYMENT_REAL_WRITES_DISABLED",
    ):
        agent.kubernetes_api_create_machine_deployment(
            "v1beta1",
            "default",
            {
                "apiVersion": "cluster.x-k8s.io/v1beta1",
                "kind": "MachineDeployment",
                "metadata": {"namespace": "default"},
            },
            dry_run=False,
        )

def test_worker_provisioning_preflight_requires_sriov_dpdk(monkeypatch):
    profile = valid_dual_network_profile()
    profile["data_plane"]["cni"]["attachments"][1]["dpdk"] = False
    monkeypatch.setattr(
        agent,
        "kubernetes_api_get",
        lambda path: (_ for _ in ()).throw(AssertionError(path)),
    )

    with pytest.raises(
        agent.NodeAgentError,
        match="WORKER_DATA_PLANE_REQUIRES_SRIOV_DPDK",
    ):
        agent.execute_command(
            {
                "command_type": "cluster.capi.worker.preflight",
                "payload": {
                    "cluster_name": "edge-lab",
                    "namespace": "default",
                    "expected_worker_replicas": 2,
                    "worker_provisioning": {
                        "machine_deployment_name": "edge-workers",
                        "infrastructure_template_name": "worker-template",
                        "bootstrap_template_name": "worker-bootstrap",
                        "baremetal_host_namespace": "metal3-system",
                        "host_selector": {"pool": "workers"},
                        "network_profile": profile,
                    },
                },
            },
            "target_cluster",
        )

@pytest.mark.parametrize(
    ("dry_run", "expected_query"),
    [(True, "?dryRun=All&fieldValidation=Strict")],
)
def test_machine_deployment_create_uses_strict_api_validation(
    monkeypatch,
    dry_run,
    expected_query,
):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    monkeypatch.setenv("AGENT_SERVICE_ACCOUNT_TOKEN_FILE", "/tmp/agent-token")
    monkeypatch.setenv("AGENT_SERVICE_ACCOUNT_CA_FILE", "/tmp/agent-ca")
    monkeypatch.setattr(agent.os.path, "isfile", lambda path: True)
    monkeypatch.setattr(
        "builtins.open",
        lambda *args, **kwargs: io.StringIO("test-service-account-token"),
    )
    monkeypatch.setattr(agent.ssl, "create_default_context", lambda cafile: object())

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self, size):
            return json.dumps(
                {
                    "kind": "MachineDeployment",
                    "metadata": {"name": "edge-workers", "namespace": "default"},
                }
            ).encode("utf-8")

    class FakeOpener:
        def open(self, request, timeout):
            assert request.method == "POST"
            assert request.full_url.endswith(expected_query)
            assert request.get_header("Authorization") == (
                "Bearer test-service-account-token"
            )
            assert json.loads(request.data.decode("utf-8"))["kind"] == (
                "MachineDeployment"
            )
            assert timeout == 30
            return FakeResponse()

    captured_handlers = []

    def fake_build_opener(*handlers):
        captured_handlers.extend(handlers)
        return FakeOpener()

    monkeypatch.setattr(agent.urllib.request, "build_opener", fake_build_opener)
    result = agent.kubernetes_api_create_machine_deployment(
        "v1beta1",
        "default",
        {
            "apiVersion": "cluster.x-k8s.io/v1beta1",
            "kind": "MachineDeployment",
            "metadata": {"name": "edge-workers", "namespace": "default"},
            "spec": {},
        },
        dry_run=dry_run,
    )

    assert result["kind"] == "MachineDeployment"
    assert any(
        isinstance(handler, agent._KubernetesNoRedirectHandler)
        for handler in captured_handlers
    )


def test_kubernetes_api_get_rejects_redirects_without_forwarding_token(
    monkeypatch, tmp_path
):
    token_file = tmp_path / "token"
    ca_file = tmp_path / "ca.crt"
    token_file.write_text("test-service-account-token", encoding="utf-8")
    ca_file.write_text("test-ca", encoding="utf-8")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    monkeypatch.setenv("AGENT_SERVICE_ACCOUNT_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("AGENT_SERVICE_ACCOUNT_CA_FILE", str(ca_file))
    monkeypatch.setattr(agent.ssl, "create_default_context", lambda cafile: object())

    captured = {}

    class FakeOpener:
        def open(self, request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            raise agent.urllib.error.HTTPError(
                request.full_url,
                302,
                "Found",
                {"Location": "https://attacker.example/collect"},
                None,
            )

    def fake_build_opener(*handlers):
        captured["handlers"] = handlers
        return FakeOpener()

    monkeypatch.setattr(agent.urllib.request, "build_opener", fake_build_opener)

    with pytest.raises(
        agent.NodeAgentError,
        match="KUBERNETES_API_REDIRECT_REJECTED",
    ):
        agent.kubernetes_api_get("/version")

    assert captured["request"].get_header("Authorization") == (
        "Bearer test-service-account-token"
    )
    assert captured["request"].full_url == (
        "https://kubernetes.default.svc:443/version"
    )
    assert captured["timeout"] == 15
    assert any(
        isinstance(handler, agent._KubernetesNoRedirectHandler)
        for handler in captured["handlers"]
    )


def test_machine_deployment_dry_run_rejects_api_redirects(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    ca_file = tmp_path / "ca.crt"
    token_file.write_text("test-service-account-token", encoding="utf-8")
    ca_file.write_text("test-ca", encoding="utf-8")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    monkeypatch.setenv("AGENT_SERVICE_ACCOUNT_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("AGENT_SERVICE_ACCOUNT_CA_FILE", str(ca_file))
    monkeypatch.setattr(agent.ssl, "create_default_context", lambda cafile: object())

    class FakeOpener:
        def open(self, request, timeout):
            raise agent.urllib.error.HTTPError(
                request.full_url,
                302,
                "Found",
                {"Location": "https://attacker.example/collect"},
                None,
            )

    monkeypatch.setattr(
        agent.urllib.request,
        "build_opener",
        lambda *handlers: FakeOpener(),
    )

    with pytest.raises(
        agent.NodeAgentError,
        match="KUBERNETES_API_REDIRECT_REJECTED",
    ):
        agent.kubernetes_api_create_machine_deployment(
            "v1beta1",
            "default",
            {
                "apiVersion": "cluster.x-k8s.io/v1beta1",
                "kind": "MachineDeployment",
                "metadata": {"namespace": "default"},
            },
            dry_run=True,
        )


@pytest.mark.parametrize(
    "host,port",
    [
        ("https://attacker.example", "443"),
        ("kubernetes.default.svc/path", "443"),
        ("kubernetes.default.svc", "0"),
        ("kubernetes.default.svc", "65536"),
    ],
)
def test_machine_deployment_dry_run_rejects_invalid_api_endpoint(
    monkeypatch, host, port, tmp_path
):
    token_file = tmp_path / "token"
    ca_file = tmp_path / "ca.crt"
    token_file.write_text("test-service-account-token", encoding="utf-8")
    ca_file.write_text("test-ca", encoding="utf-8")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", host)
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT_HTTPS", port)
    monkeypatch.setenv("AGENT_SERVICE_ACCOUNT_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("AGENT_SERVICE_ACCOUNT_CA_FILE", str(ca_file))

    with pytest.raises(
        agent.NodeAgentError,
        match="KUBERNETES_SERVICE_ENDPOINT_INVALID",
    ):
        agent.kubernetes_api_create_machine_deployment(
            "v1beta1",
            "default",
            {
                "apiVersion": "cluster.x-k8s.io/v1beta1",
                "kind": "MachineDeployment",
                "metadata": {"namespace": "default"},
            },
            dry_run=True,
        )



@pytest.mark.parametrize(
    "configmap_name,interfaces",
    [
        ("unrelated-configmap", [{"name": "eno1"}]),
        (None, "not-a-list"),
    ],
)
def test_target_agent_rejects_misbound_or_malformed_network_inventory(
    monkeypatch,
    configmap_name,
    interfaces,
):
    report = {
        "schema_version": "v1",
        "node_name": "worker-01",
        "node_uid": "node-uid-01",
        "collected_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "hardware": {
            "status": "observed",
            "interfaces": interfaces,
            "pci_network_devices": [],
            "hugepages": [],
            "numa_nodes": [],
        },
    }
    metadata = {
        "labels": {"app.kubernetes.io/name": "network-inventory-agent"}
    }
    if configmap_name is not None:
        metadata["name"] = configmap_name
    else:
        metadata["name"] = agent.network_inventory_configmap_name("worker-01")
    configmaps = [
        {
            "metadata": metadata,
            "data": {"inventory.json": json.dumps(report)},
        }
    ]
    monkeypatch.setattr(
        agent,
        "list_all_kubernetes_items",
        lambda path, max_items=100000: configmaps,
    )

    result = agent.inspect_cluster_node_hardware_inventory(
        [{"metadata": {"name": "worker-01", "uid": "node-uid-01"}}]
    )

    assert result["node_count"] == 0
    assert result["inventory_complete"] is False
    assert result["missing_node_names"] == ["worker-01"]


def test_capi_pivot_postflight_combines_capi_metal3_and_ironic_evidence(monkeypatch):
    monkeypatch.setattr(
        agent,
        "run_capi_pivot_postflight",
        lambda payload: {
            "postflight_passed": True,
            "side_effects": False,
        },
    )
    monkeypatch.setattr(agent, "ironic_api_configured", lambda: False)
    monkeypatch.setattr(
        agent,
        "observe_cluster_metal3",
        lambda *args, **kwargs: {
            "ready": True,
            "machine_count": 1,
            "resolved_machine_count": 1,
            "ironic": {"status": "not_configured"},
            "side_effects": False,
        },
    )
    result = agent.execute_command(
        {
            "command_type": "cluster.capi.pivot.postflight",
            "payload": {"cluster_name": "edge-lab", "namespace": "default"},
        },
        "target_cluster",
    )

    assert result["postflight_passed"] is True
    assert result["verification_scope"] == "capi_metal3_without_direct_ironic_api"
    assert result["complete_hardware_verification"] is False
    assert result["ironic_verification"] == "not_configured"
    assert result["side_effects"] is False


def test_capi_pivot_postflight_fails_when_metal3_chain_is_not_resolved(monkeypatch):
    monkeypatch.setattr(
        agent,
        "run_capi_pivot_postflight",
        lambda payload: {"postflight_passed": True, "side_effects": False},
    )
    monkeypatch.setattr(agent, "ironic_api_configured", lambda: False)
    monkeypatch.setattr(
        agent,
        "observe_cluster_metal3",
        lambda *args, **kwargs: {
            "ready": False,
            "blockers": ["baremetal_host_consumer_not_found"],
            "ironic": {"status": "not_configured"},
            "side_effects": False,
        },
    )

    with pytest.raises(
        agent.NodeAgentError,
        match="CAPI_PIVOT_POSTFLIGHT_METAL3_OBSERVATION_FAILED",
    ):
        agent.execute_command(
            {
                "command_type": "cluster.capi.pivot.postflight",
                "payload": {"cluster_name": "edge-lab", "namespace": "default"},
            },
            "target_cluster",
        )



def test_control_plane_client_reports_host_inventory(monkeypatch):
    calls = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self, size=-1):
            return b'{"identity_status":"candidate"}'

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, request.data, request.get_method()))
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = agent.ControlPlaneClient("https://control.example", "node-1", "secret")
    result = client.report_inventory(
        {"status": "observed", "system_identity": {"product_uuid": "uuid"}}
    )

    assert result["identity_status"] == "candidate"
    assert calls[0][0] == "https://control.example/api/v1/agent-runtime/inventory/"
    assert calls[0][2] == "POST"
    assert b'"product_uuid": "uuid"' in calls[0][1]



def test_control_plane_client_uses_verified_tls_and_optional_client_certificate(monkeypatch):
    captured = {}

    class FakeSSLContext:
        check_hostname = False
        verify_mode = None

        def load_cert_chain(self, certfile, keyfile):
            captured["certfile"] = certfile
            captured["keyfile"] = keyfile

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self, size=-1):
            return b'{"ok":true}'

    def fake_create_default_context(cafile=None):
        captured["cafile"] = cafile
        return FakeSSLContext()

    def fake_urlopen(request, timeout, context=None):
        captured["context"] = context
        return FakeResponse()

    monkeypatch.setenv("AGENT_TLS_CA_FILE", "/run/secrets/control-ca.pem")
    monkeypatch.setenv("AGENT_TLS_CERT_FILE", "/run/secrets/node.crt")
    monkeypatch.setenv("AGENT_TLS_KEY_FILE", "/run/secrets/node.key")
    monkeypatch.setattr(agent.ssl, "create_default_context", fake_create_default_context)
    monkeypatch.setattr(agent.urllib.request, "urlopen", fake_urlopen)

    client = agent.ControlPlaneClient("https://control.example", "node-1", "node-token")
    result = client.request("/api/v1/agent-runtime/heartbeat/", {"node_id": "node-1"})

    assert result == {"ok": True}
    assert captured["cafile"] == "/run/secrets/control-ca.pem"
    assert captured["certfile"] == "/run/secrets/node.crt"
    assert captured["keyfile"] == "/run/secrets/node.key"
    assert captured["context"] is client.tls_context
    assert client.tls_context.check_hostname is True
    assert client.tls_context.verify_mode == agent.ssl.CERT_REQUIRED


def test_control_plane_client_rejects_partial_client_certificate_configuration(monkeypatch):
    monkeypatch.delenv("AGENT_TLS_CERT_FILE", raising=False)
    monkeypatch.setenv("AGENT_TLS_KEY_FILE", "/run/secrets/node.key")

    with pytest.raises(
        agent.NodeAgentError,
        match="AGENT_TLS_CLIENT_CERT_AND_KEY_MUST_BE_CONFIGURED_TOGETHER",
    ):
        agent.ControlPlaneClient("https://control.example", "node-1", "node-token")


def test_control_plane_client_requires_https_when_custom_tls_is_configured(monkeypatch):
    monkeypatch.setenv("AGENT_TLS_CA_FILE", "/run/secrets/control-ca.pem")

    with pytest.raises(
        agent.NodeAgentError,
        match="AGENT_TLS_CONFIGURATION_REQUIRES_HTTPS",
    ):
        agent.ControlPlaneClient("http://control.example", "node-1", "node-token")



def test_control_plane_tls_requires_client_certificate_when_mtls_is_required(monkeypatch):
    monkeypatch.delenv("AGENT_TLS_CA_FILE", raising=False)
    monkeypatch.delenv("AGENT_TLS_CERT_FILE", raising=False)
    monkeypatch.delenv("AGENT_TLS_KEY_FILE", raising=False)
    monkeypatch.setenv("AGENT_TLS_CLIENT_CERT_REQUIRED", "true")

    with pytest.raises(
        agent.NodeAgentError,
        match="AGENT_TLS_CLIENT_CERTIFICATE_REQUIRED",
    ):
        agent.build_control_plane_tls_context()


@pytest.mark.parametrize("value", ["maybe", "enabled", "2"])
def test_control_plane_tls_rejects_ambiguous_client_certificate_requirement(
    monkeypatch,
    value,
):
    monkeypatch.delenv("AGENT_TLS_CA_FILE", raising=False)
    monkeypatch.delenv("AGENT_TLS_CERT_FILE", raising=False)
    monkeypatch.delenv("AGENT_TLS_KEY_FILE", raising=False)
    monkeypatch.setenv("AGENT_TLS_CLIENT_CERT_REQUIRED", value)

    with pytest.raises(
        agent.NodeAgentError,
        match="AGENT_TLS_CLIENT_CERT_REQUIRED_INVALID",
    ):
        agent.build_control_plane_tls_context()



def test_client_certificate_fingerprint_uses_sha256_of_der_certificate(
    monkeypatch,
    tmp_path,
):
    certificate_file = tmp_path / "client.crt"
    certificate_file.write_text(
        "-----BEGIN CERTIFICATE-----\\nZmFrZQ==\\n-----END CERTIFICATE-----\\n",
        encoding="ascii",
    )
    monkeypatch.setattr(
        agent.ssl,
        "PEM_cert_to_DER_cert",
        lambda certificate_pem: b"certificate-der",
    )

    fingerprint = agent.client_certificate_fingerprint(str(certificate_file))

    assert fingerprint == hashlib.sha256(b"certificate-der").hexdigest()


def test_client_certificate_fingerprint_rejects_missing_file(tmp_path):
    with pytest.raises(
        agent.NodeAgentError,
        match="AGENT_TLS_CLIENT_CERTIFICATE_INVALID",
    ):
        agent.client_certificate_fingerprint(str(tmp_path / "missing.crt"))


def test_worker_provision_requires_matching_durable_deployment_checkpoint():
    command = {
        "command_type": "cluster.capi.worker.provision",
        "payload": {
            "deployment_id": "deployment-123",
            "cluster_name": "edge-lab",
        },
    }
    checkpoint = {
        "deployment_id": "deployment-123",
        "generation": 1,
        "sequence": 4,
        "phase": "worker_provisioning",
        "data": {
            "cluster_name": "edge-lab",
            "completed_steps": ["worker_provisioning_preflight"],
            "pending_step": "worker_provisioning",
        },
        "resume_available": True,
    }

    agent.validate_deployment_checkpoint_for_command(
        command,
        "target_cluster",
        {"deployment-123": checkpoint},
    )


@pytest.mark.parametrize(
    "checkpoint,error_code",
    [
        (None, "DEPLOYMENT_CHECKPOINT_REQUIRED"),
        (
            {
                "deployment_id": "deployment-123",
                "generation": 1,
                "sequence": 3,
                "phase": "capi_pivot_verified",
                "data": {
                    "cluster_name": "edge-lab",
                    "completed_steps": [],
                    "pending_step": "worker_provisioning",
                },
                "resume_available": True,
            },
            "DEPLOYMENT_CHECKPOINT_PHASE_MISMATCH",
        ),
        (
            {
                "deployment_id": "deployment-123",
                "generation": 1,
                "sequence": 5,
                "phase": "worker_provisioning",
                "data": {
                    "cluster_name": "edge-lab",
                    "completed_steps": ["worker_provisioning"],
                    "pending_step": "postflight",
                },
                "resume_available": True,
            },
            "DEPLOYMENT_CHECKPOINT_STEP_ALREADY_COMPLETED_MANUAL_RECONCILIATION_REQUIRED",
        ),
    ],
)
def test_worker_provision_fails_closed_for_missing_or_stale_checkpoint(
    checkpoint,
    error_code,
):
    command = {
        "command_type": "cluster.capi.worker.provision",
        "payload": {
            "deployment_id": "deployment-123",
            "cluster_name": "edge-lab",
        },
    }
    checkpoints = {"deployment-123": checkpoint} if checkpoint else {}

    with pytest.raises(agent.NodeAgentError, match=error_code):
        agent.validate_deployment_checkpoint_for_command(
            command,
            "target_cluster",
            checkpoints,
        )


def test_checkpoint_guard_does_not_block_read_only_or_eph_commands():
    command = {
        "command_type": "cluster.capi.pivot.target.preflight",
        "payload": {"deployment_id": "deployment-123"},
    }

    agent.validate_deployment_checkpoint_for_command(command, "target_cluster", {})
    agent.validate_deployment_checkpoint_for_command(
        {
            "command_type": "cluster.capi.worker.provision",
            "payload": {"deployment_id": "deployment-123", "cluster_name": "edge-lab"},
        },
        "eph_node",
        {},
    )


def test_capi_pivot_process_is_killed_when_lease_is_cancelled(monkeypatch):
    cancellation_event = agent.threading.Event()

    class FakeProcess:
        pid = 12345
        returncode = -9

        def communicate(self, timeout=None):
            if timeout is not None:
                cancellation_event.set()
                raise capi_pivot.subprocess.TimeoutExpired(["kubectl"], timeout)
            return "", ""

    process = FakeProcess()
    killed_process_groups = []
    monkeypatch.setattr(capi_pivot.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        "node_agent.capi_pivot.os.killpg",
        lambda pid, sig: killed_process_groups.append((pid, sig)),
    )

    runner = agent.run_capi_pivot.__globals__["_cancellable_process_runner"](
        cancellation_event
    )
    with pytest.raises(
        agent.CapiPivotError,
        match="CAPI_PIVOT_CANCELLED_DURING_PROCESS",
    ):
        runner(
            ["kubectl", "get", "nodes"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            stdin=capi_pivot.subprocess.DEVNULL,
            env={},
        )

    assert killed_process_groups == [
        (12345, agent.run_capi_pivot.__globals__["signal"].SIGKILL)
    ]


def test_lease_loss_takes_precedence_over_cancelled_command_error(monkeypatch):
    class FakeClient:
        def renew_lease(self, command_id, lease_token):
            raise agent.NodeAgentError("LEASE_EXPIRED")

    def execute_until_cancelled(command, node_type, cancellation_event=None):
        assert cancellation_event.wait(1)
        raise agent.NodeAgentError("CAPI_PIVOT_CANCELLED_DURING_PROCESS")

    monkeypatch.setattr(agent, "execute_command", execute_until_cancelled)
    monkeypatch.setattr(agent, "touch_health_files", lambda **kwargs: None)

    with pytest.raises(agent.NodeAgentError, match="COMMAND_LEASE_LOST"):
        agent.execute_command_with_lease(
            FakeClient(),
            {"id": "command-01", "lease_token": "lease-secret"},
            "target_cluster",
            renew_interval=0.001,
        )


def test_run_once_does_not_submit_terminal_result_after_lease_loss(monkeypatch):
    class FakeClient:
        deployment_checkpoints = {}

        def heartbeat(self, node_type):
            return {"active_deployments": []}

        def poll(self):
            return {
                "command": {
                    "id": "command-02",
                    "lease_token": "lease-secret",
                    "command_type": "node.health",
                    "payload": {},
                }
            }

        def report_inventory(self, inventory):
            raise AssertionError("inventory should not be reported")

        def submit_result(self, *args, **kwargs):
            raise AssertionError("expired lease must not submit a terminal result")

    monkeypatch.setattr(
        agent,
        "inspect_host_network_hardware",
        lambda: {"status": "unavailable"},
    )
    monkeypatch.setattr(
        agent,
        "execute_command_with_lease",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            agent.NodeAgentError("COMMAND_LEASE_LOST")
        ),
    )
    monkeypatch.setattr(agent, "touch_health_files", lambda **kwargs: None)

    assert agent.run_once(FakeClient(), "target_cluster") is True


def test_execute_command_rejects_command_when_lease_is_already_cancelled():
    cancellation_event = agent.threading.Event()
    cancellation_event.set()

    with pytest.raises(agent.NodeAgentError, match="AGENT_COMMAND_CANCELLED"):
        agent.execute_command(
            {"command_type": "node.health", "payload": {}},
            "eph_node",
            cancellation_event=cancellation_event,
        )


def test_capi_postflight_stops_before_observation_after_lease_cancellation(monkeypatch):
    cancellation_event = agent.threading.Event()

    def cancel_during_postflight(payload):
        cancellation_event.set()
        return {"postflight_passed": True, "side_effects": False}

    monkeypatch.setattr(agent, "run_capi_pivot_postflight", cancel_during_postflight)
    monkeypatch.setattr(agent, "ironic_api_configured", lambda: False)
    observation_calls = []
    monkeypatch.setattr(
        agent,
        "observe_cluster_metal3",
        lambda *args, **kwargs: observation_calls.append(True),
    )

    with pytest.raises(agent.NodeAgentError, match="AGENT_COMMAND_CANCELLED"):
        agent.execute_command(
            {
                "command_type": "cluster.capi.pivot.postflight",
                "payload": {"cluster_name": "edge-lab", "namespace": "default"},
            },
            "target_cluster",
            cancellation_event=cancellation_event,
        )

    assert observation_calls == []


def test_long_running_leased_command_keeps_agent_liveness_fresh(monkeypatch):
    liveness_updates = []

    class FakeClient:
        def renew_lease(self, command_id, lease_token):
            return {"id": command_id}

    def slow_command(command, node_type, cancellation_event=None):
        time.sleep(0.06)
        return {"ready": True}

    monkeypatch.setattr(agent, "execute_command", slow_command)
    monkeypatch.setattr(
        agent,
        "touch_health_files",
        lambda readiness=False: liveness_updates.append(readiness),
    )

    result = agent.execute_command_with_lease(
        FakeClient(),
        {"id": "command-liveness", "lease_token": "lease-liveness"},
        "eph_node",
        renew_interval=0.01,
    )

    assert result == {"ready": True}
    assert liveness_updates
    assert all(readiness is False for readiness in liveness_updates)


@pytest.mark.parametrize("response_body", [b"[]", b'"not-an-object"', b"null", b"invalid-json"])
def test_control_plane_client_rejects_non_object_or_invalid_json_responses(
    monkeypatch,
    response_body,
):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self, size=-1):
            return response_body

    monkeypatch.setattr(
        agent.urllib.request,
        "urlopen",
        lambda request, timeout: Response(),
    )
    client = agent.ControlPlaneClient(
        "https://control.example",
        "node-response-validation",
        "agent-token",
    )

    with pytest.raises(agent.NodeAgentError, match="CONTROL_PLANE_RESPONSE_INVALID"):
        client.request("/api/v1/agent-runtime/heartbeat/")


def test_control_plane_client_rejects_oversized_responses(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self, size=-1):
            assert size == 4 * 1024 * 1024 + 1
            return b"x" * size

    monkeypatch.setattr(
        agent.urllib.request,
        "urlopen",
        lambda request, timeout: Response(),
    )
    client = agent.ControlPlaneClient(
        "https://control.example",
        "node-response-size",
        "agent-token",
    )

    with pytest.raises(agent.NodeAgentError, match="CONTROL_PLANE_RESPONSE_TOO_LARGE"):
        client.request("/api/v1/agent-runtime/heartbeat/")


def test_capi_pivot_process_group_is_killed_on_timeout(monkeypatch):
    class FakeProcess:
        pid = 54321
        returncode = -9

        def __init__(self):
            self.communicate_calls = 0

        def communicate(self, timeout=None):
            self.communicate_calls += 1
            return "", ""

    process = FakeProcess()
    killed_process_groups = []
    monkeypatch.setattr(capi_pivot.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        "node_agent.capi_pivot.os.killpg",
        lambda pid, sig: killed_process_groups.append((pid, sig)),
    )

    runner = agent.run_capi_pivot.__globals__["_cancellable_process_runner"](None)
    with pytest.raises(capi_pivot.subprocess.TimeoutExpired):
        runner(
            ["kubectl", "get", "nodes"],
            check=False,
            capture_output=True,
            text=True,
            timeout=0,
            stdin=capi_pivot.subprocess.DEVNULL,
            env={},
        )

    assert killed_process_groups == [
        (54321, agent.run_capi_pivot.__globals__["signal"].SIGKILL)
    ]
    assert process.communicate_calls == 1
