pathlib = __import__("pathlib")


ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_node_agent_build_script_pins_both_kubernetes_tools_and_image_digest():
    script = (ROOT / "deploy/agents/build-node-agent.sh").read_text(encoding="utf-8")

    for required in (
        "CLUSTERCTL_VERSION_INVALID",
        "CLUSTERCTL_SHA256_AMD64_INVALID",
        "CLUSTERCTL_SHA256_ARM64_INVALID",
        "KUBECTL_VERSION_INVALID",
        "KUBECTL_SHA256_AMD64_INVALID",
        "KUBECTL_SHA256_ARM64_INVALID",
        "MUTABLE_LATEST_TAG_NOT_ALLOWED",
        "--push",
        "--metadata-file",
        "containerimage.digest",
        "AGENT_IMAGE_REF=",
    ):
        assert required in script
