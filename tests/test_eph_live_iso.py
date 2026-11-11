import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE_ROOT = ROOT / "images" / "eph-live"


def test_live_iso_boots_ram_and_runs_agent_as_kubernetes_deployment():
    config = (IMAGE_ROOT / "auto" / "config").read_text(encoding="utf-8")
    bootstrap = (
        IMAGE_ROOT
        / "config"
        / "includes.chroot"
        / "usr/local/sbin/eph-k8s-bootstrap"
    ).read_text(encoding="utf-8")

    assert "toram" in config
    assert "kubeadm init --config" in bootstrap
    assert "kubectl apply -f /run/eph-bootstrap/eph-agent.yaml" in bootstrap
    assert "kind: Deployment" in bootstrap
    assert "docker run" not in bootstrap
    assert "docker-compose" not in bootstrap
    assert "AGENT_TLS_CLIENT_CERT_REQUIRED" in bootstrap
    assert "KUBERNETES_VERSION_MUST_MATCH_IMAGE" in bootstrap
    assert "AGENT_HOST_SYSFS_PATH" in bootstrap


def test_live_iso_requires_digest_pinned_agent_and_verified_cni():
    build = (IMAGE_ROOT / "build.sh").read_text(encoding="utf-8")
    bootstrap = (
        IMAGE_ROOT
        / "config"
        / "includes.chroot"
        / "usr/local/sbin/eph-k8s-bootstrap"
    ).read_text(encoding="utf-8")

    assert "NODE_AGENT_IMAGE_MUST_BE_DIGEST_PINNED" in build
    assert "FLANNEL_MANIFEST_SHA256_REQUIRED" in build
    assert "sha256sum -c -" in bootstrap
    assert "@sha256:[a-f0-9]{64}" in build


def test_eph_agent_rbac_is_read_only_and_scoped():
    rbac = (
        IMAGE_ROOT
        / "config"
        / "includes.chroot"
        / "usr/local/share/eph/node-agent-rbac.yaml"
    ).read_text(encoding="utf-8")

    assert "name: eph-node-agent-readonly" in rbac
    assert "verbs: [\"get\", \"list\", \"watch\"]" in rbac
    assert "create" not in rbac
    assert "update" not in rbac
    assert "delete" not in rbac


def test_seed_generator_requires_short_lived_identity_and_mtls_files():
    seed = (IMAGE_ROOT / "create-seed.sh").read_text(encoding="utf-8")

    for required in (
        "NODE_TOKEN_INVALID",
        "NODE_AGENT_IMAGE_MUST_BE_DIGEST_PINNED",
        "CA_CERT_FILE",
        "CLIENT_CERT_FILE",
        "CLIENT_KEY_FILE",
        "cloud-localds",
    ):
        assert required in seed


def test_eph_live_shell_scripts_have_valid_syntax():
    scripts = [
        IMAGE_ROOT / "build.sh",
        IMAGE_ROOT / "create-seed.sh",
        IMAGE_ROOT / "auto" / "config",
        IMAGE_ROOT
        / "config"
        / "includes.chroot"
        / "usr/local/sbin/eph-k8s-bootstrap",
        IMAGE_ROOT
        / "config"
        / "hooks/normal/0100-eph-setup.hook.chroot",
    ]
    for script in scripts:
        result = subprocess.run(
            ["bash", "-n", str(script)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
