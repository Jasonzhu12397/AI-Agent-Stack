import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_dual_agent_deploy_script_requires_two_contexts_and_digest_image():
    script = (ROOT / "deploy/agents/deploy.sh").read_text(encoding="utf-8")

    assert "EPH_CONTEXT_REQUIRED" in script
    assert "TARGET_CONTEXT_REQUIRED" in script
    assert "EPH_AND_TARGET_CONTEXTS_MUST_DIFFER" in script
    assert "AGENT_IMAGE_MUST_BE_PINNED_BY_SHA256_DIGEST" in script
    assert "apply --dry-run=server" in script
    assert "rollout status deployment/eph-node-agent" in script
    assert "rollout status deployment/target-cluster-agent" in script
    assert "rollout status daemonset/network-inventory-agent" in script


def test_dual_agent_deploy_script_requires_pivot_and_ironic_secrets():
    script = (ROOT / "deploy/agents/deploy.sh").read_text(encoding="utf-8")

    assert "capi-pivot-kubeconfigs" in script
    assert "source.kubeconfig target.kubeconfig" in script
    assert "ironic-api-credentials api-url token" in script
    assert "eph-node-agent-credentials control-url node-id token" in script
    assert "target-cluster-agent-credentials control-url node-id token" in script
