import pytest

from apps.node_agent import agent


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
