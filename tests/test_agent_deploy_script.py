from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_three_agent_deploy_requires_three_distinct_contexts_and_digest_images():
    script = (ROOT / "deploy/agents/deploy.sh").read_text(encoding="utf-8")
    assert "CLOUD_CONTEXT_REQUIRED" in script
    assert "EPH_CONTEXT_REQUIRED" in script
    assert "TARGET_CONTEXT_REQUIRED" in script
    assert "CLOUD_AND_EPH_CONTEXTS_MUST_DIFFER" in script
    assert "EPH_AND_TARGET_CONTEXTS_MUST_DIFFER" in script
    assert "CLOUD_CONTROL_IMAGE_MUST_BE_PINNED_BY_SHA256_DIGEST" in script
    assert "EPH_AGENT_IMAGE_MUST_BE_PINNED_BY_SHA256_DIGEST" in script
    assert "TARGET_AGENT_IMAGE_MUST_BE_PINNED_BY_SHA256_DIGEST" in script
    assert "apply --dry-run=server" in script
    assert "THREE_AGENT_DEPLOYMENT_READY" in script
    assert "network-inventory-agent" not in script
    assert "DEPLOYMENT_ID_MUST_BE_A_DNS_LABEL" in script
    assert "TARGET_CAPI_NAMESPACE_MUST_BE_A_DNS_LABEL" in script
    assert "REPLACE_WITH_TARGET_CAPI_NAMESPACE" in script
    assert 'mark_eph_cleanup_resources "$EPH_CONTEXT" serviceaccount eph-agent' in script
    assert 'mark_eph_cleanup_resources "$EPH_CONTEXT" clusterrole eph-agent-preflight' in script
    assert (
        'mark_eph_cleanup_resources "$EPH_CONTEXT" clusterrolebinding eph-agent-preflight'
        in script
    )
    assert "ai-agent-node-agent" not in script


def test_three_agent_deploy_validates_role_specific_secrets():
    script = (ROOT / "deploy/agents/deploy.sh").read_text(encoding="utf-8")
    assert "cloud-control-agent-database database-url" in script
    assert "cloud-control-agent-mtls tls.crt tls.key client-ca.crt" in script
    assert "eph-agent-credentials control-url agent-id installation-id" in script
    assert (
        "target-agent-credentials control-url agent-id installation-id cluster-id "
        "ironic-api-url ironic-api-token"
    ) in script
    assert "capi-pivot-source-kubeconfig kubeconfig" in script
    assert 'require_secret_keys "$EPH_CONTEXT" capi-pivot-target-kubeconfig' not in script
    assert "capi-pivot-target-kubeconfig" in script
    assert 'ai-agent-stack.io/credential-scope=eph-agent' in script


def test_target_agent_is_three_replicas_with_control_plane_anti_affinity_and_leases():
    manifest = list(yaml.safe_load_all(
        (ROOT / "deploy/agents/target-cluster-agent.yaml").read_text(encoding="utf-8")
    ))
    deployment = next(item for item in manifest if item.get("kind") == "Deployment")
    spec = deployment["spec"]
    pod_spec = spec["template"]["spec"]
    assert spec["replicas"] == 3
    assert pod_spec["nodeSelector"] == {"node-role.kubernetes.io/control-plane": ""}
    assert pod_spec["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    assert any(item.get("kind") == "Role" for item in manifest)
    assert any(item.get("kind") == "ClusterRole" for item in manifest)
    assert any(item.get("kind") == "PodDisruptionBudget" for item in manifest)
    assert "target-agent-leader" in str(deployment)
    metal3_operator_role = next(
        item for item in manifest
        if item.get("kind") == "Role"
        and item["metadata"]["name"] == "target-agent-metal3-operator"
    )
    metal3_operator_binding = next(
        item for item in manifest
        if item.get("kind") == "RoleBinding"
        and item["metadata"]["name"] == "target-agent-metal3-operator"
    )
    assert metal3_operator_role["metadata"]["namespace"] == "REPLACE_WITH_TARGET_CAPI_NAMESPACE"
    assert {"configmaps", "secrets"}.issubset(
        set(metal3_operator_role["rules"][0]["resources"])
    )
    assert set(metal3_operator_role["rules"][0]["verbs"]) == {
        "get", "list", "watch", "create", "patch", "update", "delete"
    }
    assert metal3_operator_binding["roleRef"]["name"] == "target-agent-metal3-operator"
    assert metal3_operator_binding["subjects"][0]["namespace"] == "ai-agent-system"
    assert "ai-agent-target-agent:VERSION" in pod_spec["containers"][0]["image"]


def test_eph_agent_boots_without_target_kubeconfig_secret():
    manifest = list(yaml.safe_load_all(
        (ROOT / "deploy/agents/eph-agent.yaml").read_text(encoding="utf-8")
    ))
    deployment = next(item for item in manifest if item.get("kind") == "Deployment")
    pod_spec = deployment["spec"]["template"]["spec"]
    assert deployment["spec"]["replicas"] == 1
    assert "source-kubeconfig" in str(pod_spec["volumes"])
    target_volume = next(
        item for item in pod_spec["volumes"]
        if item["name"] == "target-kubeconfig"
    )
    assert target_volume["secret"]["optional"] is True


def test_cloud_control_agent_is_separate_mtls_service():
    manifest = list(yaml.safe_load_all(
        (ROOT / "deploy/agents/cloud-control-agent.yaml").read_text(encoding="utf-8")
    ))
    deployment = next(item for item in manifest if item.get("kind") == "Deployment")
    service = next(item for item in manifest if item.get("kind") == "Service")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "REPLACE_WITH_REGISTRY/ai-agent-cloud-control-agent:VERSION"
    assert service["spec"]["type"] == "LoadBalancer"
    assert container["ports"][0]["containerPort"] == 8443


def test_new_build_script_builds_three_independent_images():
    script = (ROOT / "deploy/agents/build-agents.sh").read_text(encoding="utf-8")
    assert "Dockerfile.cloud-control-agent" in script
    assert "Dockerfile.eph-agent" in script
    assert "Dockerfile.target-agent" in script
    assert "Dockerfile.node-agent" not in script
    assert "ONLY_LINUX_AMD64_IS_CURRENTLY_VERIFIED" in script


def test_target_agent_receives_ironic_credentials_only_from_kubernetes_secret():
    manifest = list(yaml.safe_load_all(
        (ROOT / "deploy/agents/target-cluster-agent.yaml").read_text(encoding="utf-8")
    ))
    deployment = next(item for item in manifest if item.get("kind") == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    environment = {item["name"]: item for item in container["env"]}
    assert environment["IRONIC_API_URL"]["valueFrom"]["secretKeyRef"]["key"] == "ironic-api-url"
    assert (
        environment["IRONIC_API_TOKEN"]["valueFrom"]["secretKeyRef"]["key"]
        == "ironic-api-token"
    )
    assert environment["IRONIC_API_MICROVERSION"]["value"] == "1.82"

def test_legacy_shared_node_agent_runtime_is_absent():
    assert not (ROOT / "apps/node_agent").exists()
    assert not (ROOT / "Dockerfile.node-agent").exists()
    assert not (ROOT / "deploy/agents/node-agent.yaml").exists()
    assert (ROOT / "apps/cloud_control_agent/main.py").is_file()
    assert (ROOT / "apps/eph_agent/main.py").is_file()
    assert (ROOT / "apps/target_agent/main.py").is_file()
