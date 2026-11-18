from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_cloud_control_and_agent_images_are_separate_from_legacy_runtime():
    cloud = (ROOT / "Dockerfile.cloud-control-agent").read_text(encoding="utf-8")
    eph = (ROOT / "Dockerfile.eph-agent").read_text(encoding="utf-8")
    target = (ROOT / "Dockerfile.target-agent").read_text(encoding="utf-8")
    for dockerfile in (cloud, eph, target):
        assert "apps/node_agent" not in dockerfile
        assert "ai-agent-node-agent" not in dockerfile
    assert "apps/cloud_control_agent" in cloud
    assert "apps/eph_agent" in eph
    assert "apps/target_agent" in target


def test_new_agent_entrypoints_do_not_delegate_to_legacy_runtime():
    for relative in (
        "apps/cloud_control_agent/main.py",
        "apps/eph_agent/main.py",
        "apps/target_agent/main.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "apps.node_agent.runtime" not in source
        assert "run_node_agent" not in source


def test_target_deployment_has_no_worker_daemonset():
    manifests = list(yaml.safe_load_all(
        (ROOT / "deploy/agents/target-cluster-agent.yaml").read_text(encoding="utf-8")
    ))
    assert not any(item.get("kind") == "DaemonSet" for item in manifests)
    assert not (ROOT / "deploy/agents/network-inventory-agent.yaml").exists()


def test_active_deployment_artifacts_no_longer_include_legacy_node_agent():
    assert not (ROOT / "Dockerfile.node-agent").exists()
    assert not (ROOT / "deploy/agents/build-node-agent.sh").exists()
    assert not (ROOT / "deploy/agents/eph-node-agent.yaml").exists()
    assert not (ROOT / "deploy/agents/network-inventory-agent.yaml").exists()
    assert (ROOT / "apps/node_agent/agent.py").exists() is False


def test_local_cloud_stack_remains_separate_from_cluster_agents():
    compose = yaml.safe_load(
        (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    )
    services = compose["services"]
    assert "eph-agent" not in services
    assert "target-agent" not in services
