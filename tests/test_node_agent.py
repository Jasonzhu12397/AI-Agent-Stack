# ruff: noqa: I001
import datetime
import json
import time

import pytest

from node_agent import agent


def test_node_health_is_available_for_both_agent_roles():
    for node_type in ["eph_node", "target_cluster"]:
        result = agent.execute_command(
            {"command_type": "node.health", "payload": {}},
            node_type,
        )
        assert result["agent_status"] == "ready"
        assert result["node_type"] == node_type


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
            ["metal3clusters", "metal3machines", "metal3machinetemplates"],
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
            "metal3machinetemplates",
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
            "metal3machinetemplates",
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


def test_control_plane_client_restores_durable_deployment_checkpoint(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self):
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

        def read(self):
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


def test_target_agent_restores_assigned_deployment_checkpoint_before_polling(
    monkeypatch,
    capsys,
):
    client = agent.ControlPlaneClient("https://control.example", "node-123", "agent-token")
    deployment_id = "deployment-123"
    client.heartbeat = lambda node_type: {
        "active_deployments": [{"deployment_id": deployment_id}]
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
            "metal3machinetemplates",
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
            ["metal3clusters", "metal3machines", "metal3machinetemplates"],
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

    def slow_command(command, node_type):
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


def test_command_is_not_reported_successful_after_lease_renewal_fails(monkeypatch):
    class FakeClient:
        def renew_lease(self, command_id, lease_token):
            raise agent.NodeAgentError("CONTROL_PLANE_UNAVAILABLE")

    def slow_command(command, node_type):
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
                    "status": {
                        "conditions": [{"type": "Ready", "status": "True"}]
                    },
                }
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
    assert result["machine_inventory_ready"] is True
    assert result["controllers_ready"] is True
    assert len(result["controllers"]) == 4
    assert result["side_effects"] is False


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
            "cni": {
                "primary": "calico",
                "meta": "multus",
                "attachments": [
                    {"name": "ovs-data", "type": "ovs"},
                    {
                        "name": "sriov-data",
                        "type": "sriov",
                        "resource_name": "intel.com/sriov_netdevice",
                    },
                ],
            },
        },
    }


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
        (numa / "cpulist", "0-31"),
    ]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="ascii")
    (pci / "net" / "eno1").mkdir()
    (pci / "driver").symlink_to(driver, target_is_directory=True)
    (pci / "iommu_group").symlink_to(pci, target_is_directory=True)
    (pci / "virtfn0").symlink_to(pci, target_is_directory=True)
    (interface / "device").symlink_to(
        "../../../bus/pci/devices/0000:03:00.0",
        target_is_directory=True,
    )
    monkeypatch.setenv("AGENT_HOST_SYSFS_PATH", str(root))

    result = agent.inspect_host_network_hardware()

    assert result["status"] == "observed"
    assert result["scope"] == "host_sysfs_read_only"
    assert result["cpu_online"] == "0-31"
    assert result["interfaces"][0]["name"] == "eno1"
    assert result["interfaces"][0]["pci_address"] == "0000:03:00.0"
    assert result["interfaces"][0]["sriov_total_vfs"] == "64"
    assert result["pci_network_devices"][0]["driver"] == "ixgbe"
    assert result["pci_network_devices"][0]["iommu_group_present"] is True
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
