from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_node_agent_build_script_pins_both_kubernetes_tools_and_image_digest():
    script = (ROOT / "deploy/agents/build-node-agent.sh").read_text(encoding="utf-8")

    for required in (
        "CLUSTERCTL_VERSION_REQUIRED",
        "CLUSTERCTL_SHA256_INVALID",
        "KUBECTL_VERSION_REQUIRED",
        "KUBECTL_SHA256_INVALID",
        "MUTABLE_LATEST_TAG_NOT_ALLOWED",
        "--push",
        "--metadata-file",
        "containerimage.digest",
        "AGENT_IMAGE_REF=",
    ):
        assert required in script
