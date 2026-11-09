from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


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


def test_node_agent_image_contains_only_cluster_runtime_and_minimal_dependency():
    dockerfile = (ROOT / "Dockerfile.node-agent").read_text(encoding="utf-8")
    assert 'RUN pip install --no-cache-dir "PyYAML>=6.0,<7.0"' in dockerfile
    assert "COPY apps/node_agent ./apps/node_agent" in dockerfile
    assert "COPY apps ./apps" not in dockerfile
    assert "COPY pyproject.toml" not in dockerfile
    assert "COPY apps/platform-api" not in dockerfile
    assert "COPY apps/worker" not in dockerfile
    assert "CMD [\"python\", \"-m\", \"apps.node_agent.agent\"" in dockerfile


def test_cloud_and_cluster_agent_entrypoints_are_not_the_same():
    cloud = (ROOT / "Dockerfile.cloud-agent").read_text(encoding="utf-8")
    node = (ROOT / "Dockerfile.node-agent").read_text(encoding="utf-8")
    assert "worker_app.main" in cloud
    assert "apps.node_agent.agent" in node
