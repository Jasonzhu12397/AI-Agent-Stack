# ruff: noqa: I001
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
        if path.endswith("/cluster.x-k8s.io/v1beta1"):
            return {"resources": [{"name": name} for name in ["clusters", "machines", "machinedeployments"]]}
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
