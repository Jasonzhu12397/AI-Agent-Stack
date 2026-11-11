pathlib = __import__("pathlib")


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


def test_both_agent_deployments_require_mtls_and_mount_node_credentials():
    for filename in ("eph-node-agent.yaml", "target-cluster-agent.yaml"):
        manifest = (ROOT / "deploy/agents" / filename).read_text(encoding="utf-8")

        assert "AGENT_TLS_CLIENT_CERT_REQUIRED" in manifest
        assert 'value: "true"' in manifest
        assert "AGENT_TLS_CA_FILE" in manifest
        assert "AGENT_TLS_CERT_FILE" in manifest
        assert "AGENT_TLS_KEY_FILE" in manifest
        assert "secretName: agent-control-plane-mtls" in manifest
        assert "key: ca.crt" in manifest
        assert "key: tls.crt" in manifest
        assert "key: tls.key" in manifest
        assert "mountPath: /var/run/secrets/ai-agent/control-plane-tls" in manifest


def test_node_agents_are_kubernetes_workloads_and_not_compose_services():
    compose = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    eph = (ROOT / "deploy/agents/eph-node-agent.yaml").read_text(encoding="utf-8")
    target = (ROOT / "deploy/agents/target-cluster-agent.yaml").read_text(encoding="utf-8")
    docs = (ROOT / "deploy/agents/README.md").read_text(encoding="utf-8")
    root_readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "eph-node-agent:" not in compose
    assert "target-cluster-agent:" not in compose
    assert 'profiles: ["node-agents"]' not in compose
    assert "kind: Deployment" in eph
    assert "replicas: 1" in eph
    assert "kind: Deployment" in target
    assert "serviceAccountName: eph-node-agent" in eph
    assert "AGENT_NODE_TYPE" in eph and "eph_node" in eph
    assert "EPH Agent runs inside a temporary single-node Kubernetes cluster" in docs
    assert (
        "always a Kubernetes Pod inside the temporary single-node Kubernetes cluster"
        in root_readme
    )
    agent_docs_start = root_readme.index(
        "### Deploy the EPH and Target Agents as Kubernetes Pods"
    )
    agent_docs_end = root_readme.index("## Local checks")
    assert "docker compose" not in root_readme[agent_docs_start:agent_docs_end]
