import pathlib

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_pivot_agent_image_installs_checksum_pinned_cluster_tools():
    dockerfile = (ROOT / "Dockerfile.node-agent").read_text(encoding="utf-8")

    for argument in (
        "CLUSTERCTL_VERSION",
        "CLUSTERCTL_SHA256",
        "KUBECTL_VERSION",
        "KUBECTL_SHA256",
    ):
        assert f"ARG {argument}" in dockerfile
        assert 'test -n "$' + argument + '"' in dockerfile

    assert "clusterctl-linux-${ASSET_ARCH}" in dockerfile
    assert "/bin/linux/${ASSET_ARCH}/kubectl" in dockerfile
    assert "sha256sum -c -" in dockerfile
    assert "COPY --from=clusterctl /usr/local/bin/clusterctl" in dockerfile
    assert "COPY --from=clusterctl /usr/local/bin/kubectl" in dockerfile


def test_agent_deployments_use_the_pivot_capable_image():
    paths = (
        "deploy/agents/eph-node-agent.yaml",
        "deploy/agents/target-cluster-agent.yaml",
        "deploy/agents/network-inventory-agent.yaml",
    )

    for relative_path in paths:
        documents = list(
            yaml.safe_load_all((ROOT / relative_path).read_text(encoding="utf-8"))
        )
        containers = []
        for document in documents:
            spec = document.get("spec", {})
            pod_spec = spec.get("template", {}).get("spec", {})
            containers.extend(pod_spec.get("containers", []))

        assert containers
        assert all(
            item.get("image") == "REPLACE_WITH_REGISTRY/ai-agent-node-agent:VERSION"
            for item in containers
        )
