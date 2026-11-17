pathlib = __import__("pathlib")
yaml = __import__("yaml")


ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_cloud_api_and_cloud_agent_images_exclude_node_agent_source():
    for relative_path in ("Dockerfile", "Dockerfile.cloud-agent"):
        dockerfile = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "COPY apps/platform-api ./apps/platform-api" in dockerfile
        assert "COPY apps/worker ./apps/worker" in dockerfile
        assert "COPY apps ./apps" not in dockerfile
        assert "apps/node_agent" not in dockerfile


def test_cloud_agent_has_a_dedicated_image_and_worker_entrypoint():
    dockerfile = (ROOT / "Dockerfile.cloud-agent").read_text(encoding="utf-8")
    compose = yaml.safe_load(
        (ROOT / "deploy/docker-compose.cloud.yml").read_text(encoding="utf-8")
    )
    worker = compose["services"]["worker"]
    assert 'CMD ["python", "-m", "worker_app.main"]' in dockerfile
    assert worker["build"]["dockerfile"] == "Dockerfile.cloud-agent"
    assert worker["image"] == "${AI_AGENT_CLOUD_AGENT_IMAGE:-ai-agent-cloud-agent:local}"


def test_local_compose_runs_cloud_services_but_not_node_agents():
    compose = yaml.safe_load(
        (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    )
    services = compose["services"]
    assert services["worker"]["build"]["dockerfile"] == "Dockerfile.cloud-agent"
    cloud_image = "${AI_AGENT_CLOUD_AGENT_IMAGE:-ai-agent-cloud-agent:local}"
    assert services["worker"]["image"] == cloud_image
    assert "eph-node-agent" not in services
    assert "target-cluster-agent" not in services


def test_node_agent_image_contains_only_cluster_runtime_and_minimal_dependency():
    dockerfile = (ROOT / "Dockerfile.node-agent").read_text(encoding="utf-8")
    assert 'RUN pip install --no-cache-dir "PyYAML>=6.0,<7.0"' in dockerfile
    assert "COPY apps/node_agent ./apps/node_agent" in dockerfile
    assert "COPY apps/eph_agent ./apps/eph_agent" in dockerfile
    assert "COPY apps/target_agent ./apps/target_agent" in dockerfile
    assert "COPY apps ./apps" not in dockerfile
    assert "COPY pyproject.toml" not in dockerfile
    assert "COPY apps/platform-api" not in dockerfile
    assert "COPY apps/worker" not in dockerfile
    assert 'CMD ["python", "-m", "apps.target_agent.main"' in dockerfile


def test_cloud_and_cluster_agent_entrypoints_are_not_the_same():
    cloud = (ROOT / "Dockerfile.cloud-agent").read_text(encoding="utf-8")
    node = (ROOT / "Dockerfile.node-agent").read_text(encoding="utf-8")
    assert "worker_app.main" in cloud
    assert "apps.target_agent.main" in node


def test_three_agent_roles_have_distinct_entrypoints_and_target_runs_on_control_plane():
    eph_entrypoint = (ROOT / "apps/eph_agent/main.py").read_text(encoding="utf-8")
    target_entrypoint = (ROOT / "apps/target_agent/main.py").read_text(encoding="utf-8")
    eph_manifest = yaml.safe_load(
        (ROOT / "deploy/agents/eph-node-agent.yaml").read_text(encoding="utf-8")
    )
    target_manifest = yaml.safe_load(
        (ROOT / "deploy/agents/target-cluster-agent.yaml").read_text(encoding="utf-8")
    )

    assert 'expected_node_type="eph_node"' in eph_entrypoint
    assert 'expected_node_type="target_cluster"' in target_entrypoint
    assert not (ROOT / "apps/node_agent/agent.py").exists()
    eph_command = eph_manifest["spec"]["template"]["spec"]["containers"][0]["command"]
    assert "apps.eph_agent.main" in str(eph_command)
    target_command = target_manifest["spec"]["template"]["spec"]["containers"][0]["command"]
    assert "apps.target_agent.main" in str(target_command)
    assert target_manifest["spec"]["template"]["spec"]["nodeSelector"] == {
        "node-role.kubernetes.io/control-plane": ""
    }
