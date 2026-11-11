import pathlib
import runpy
import uuid

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = ROOT / "eph-iso/bootstrap.py"
BOOTSTRAP = runpy.run_path(str(BOOTSTRAP_PATH))


def valid_config():
    return {
        "control_url": "https://agent-control.example.com",
        "node_id": str(uuid.uuid4()),
        "enrollment_token": "a" * 64,
        "agent_image": "registry.example.com/ai-agent@sha256:" + "a" * 64,
    }


def test_eph_iso_builder_pins_k3s_and_verifies_checksum():
    script = (ROOT / "eph-iso/build.sh").read_text(encoding="utf-8")
    assert "K3S_VERSION_INVALID" in script
    assert "K3S_SHA256_AMD64_INVALID" in script
    assert "sha256sum -c -" in script
    assert "boot=live components toram" in script
    assert "iso-hybrid" in script
    assert "v1.33|v1.34|v1.35|v1.36" in script
    assert "KUBERNETES_MINOR_VERSION_NOT_SUPPORTED" in script
    assert "EPH_ISO_METADATA" in script


def test_eph_iso_uses_kubernetes_pod_for_agent_not_compose():
    service_path = ROOT / "eph-iso/config/includes.chroot/etc/systemd/system/eph-bootstrap.service"
    service = service_path.read_text(encoding="utf-8")
    agent_path = ROOT / "deploy/agents/eph-node-agent.yaml"
    agent_manifest = agent_path.read_text(encoding="utf-8")
    assert "eph-bootstrap.py" in service
    assert "kind: Deployment" in agent_manifest
    assert "kind: Pod" not in agent_manifest
    assert "docker compose" not in service


def test_eph_k3s_and_kubelet_state_are_ram_backed():
    root = ROOT / "eph-iso/config/includes.chroot/etc/systemd/system"
    for name in ("var-lib-rancher-k3s.mount", "var-lib-kubelet.mount"):
        content = (root / name).read_text(encoding="utf-8")
        assert "What=tmpfs" in content
        assert "Type=tmpfs" in content
    k3s = (root / "k3s.service").read_text(encoding="utf-8")
    assert "k3s server" in k3s
    assert "WantedBy=multi-user.target" in k3s


def test_bootstrap_config_accepts_pinned_image_and_https_endpoint():
    assert BOOTSTRAP["validate_config"](valid_config())["node_id"]


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"control_url": "http://agent-control.example.com"}, "CONTROL_URL_INVALID"),
        ({"control_url": "https://user:pass@example.com"}, "CONTROL_URL_INVALID"),
        ({"node_id": "not-a-uuid"}, "NODE_ID_INVALID"),
        ({"enrollment_token": "short"}, "ENROLLMENT_TOKEN_INVALID"),
        ({"agent_image": "registry.example.com/agent:latest"}, "AGENT_IMAGE_NOT_PINNED_BY_DIGEST"),
    ],
)
def test_bootstrap_config_rejects_unsafe_values(change, expected):
    config = valid_config()
    config.update(change)
    with pytest.raises(BOOTSTRAP["BootstrapError"], match=expected):
        BOOTSTRAP.validate_config(config)


def test_bootstrap_does_not_accept_missing_fields():
    with pytest.raises(BOOTSTRAP.BootstrapError, match="BOOTSTRAP_CONFIG_FIELDS_MISSING"):
        BOOTSTRAP.validate_config({"node_id": str(uuid.uuid4())})


def test_systemd_starts_k3s_and_bootstrap_from_multi_user_target():
    dropin = (
        ROOT
        / "eph-iso/config/includes.chroot/etc/systemd/system/multi-user.target.d/eph.conf"
    )
    content = dropin.read_text(encoding="utf-8")
    assert "Wants=k3s.service eph-bootstrap.service" in content


def test_bootstrap_agent_manifest_is_bundled_into_iso():
    script = (ROOT / "eph-iso/build.sh").read_text(encoding="utf-8")
    assert "deploy/agents/namespace.yaml" in script
    assert "eph-rbac.yaml" in script
    assert "deploy/agents/eph-node-agent.yaml" in script
    assert "usr/share/ai-agent-studio/eph-manifests" in script


def test_eph_boot_media_mount_and_network_configuration_are_ordered_before_k3s():
    mount = ROOT / "eph-iso/config/includes.chroot/etc/systemd/system/mnt-eph\\x2dbootstrap.mount"
    local_fs = ROOT / "eph-iso/config/includes.chroot/etc/systemd/system/local-fs.target.d/eph.conf"
    network_service = (
        ROOT
        / "eph-iso/config/includes.chroot/etc/systemd/system/eph-network-config.service"
    )
    network_dropin = (
        ROOT
        / "eph-iso/config/includes.chroot/etc/systemd/system/systemd-networkd.service.d/eph.conf"
    )
    assert "Where=/mnt/eph-bootstrap" in mount.read_text(encoding="utf-8")
    assert "mnt-eph\\x2dbootstrap.mount" in local_fs.read_text(encoding="utf-8")
    assert "Before=systemd-networkd.service" in network_service.read_text(encoding="utf-8")
    assert "Requires=eph-network-config.service" in network_dropin.read_text(encoding="utf-8")


def test_eph_agent_capi_pivot_secret_is_optional_until_target_cluster_exists():
    manifest = (ROOT / "deploy/agents/eph-node-agent.yaml").read_text(encoding="utf-8")
    assert "secretName: capi-pivot-kubeconfigs" in manifest
    assert "optional: true" in manifest
    bootstrap = (ROOT / "eph-iso/bootstrap.py").read_text(encoding="utf-8")
    assert "CAPI_PIVOT_KUBECONFIG_SET_INCOMPLETE" in bootstrap
    assert "if capi_files:" in bootstrap
