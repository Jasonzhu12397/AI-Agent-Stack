import pytest

from apps.eph_agent.bootstrap_renderer import (
    render_agent_deployment,
    render_cloud_init_seed,
    render_kubeadm_init,
)
from apps.eph_agent.metal3_skill import Metal3SkillError

AGENT_ID = "0b8a5dc2-6f6f-4b4e-9a4c-3f6a1b7c8d90"
INSTALLATION_ID = "1f0c2a44-8b1d-4c3e-8f5a-2b6c9d0e1f22"


def _spec(**overrides: object) -> dict:
    spec: dict = {
        "node_name": "eph-bootstrap-01",
        "kubernetes_version": "v1.31.2",
        "node_ip": "192.0.2.10",
        "pod_cidr": "10.244.0.0/16",
        "service_cidr": "10.96.0.0/12",
        "control_plane_endpoint": "192.0.2.10",
        "control_plane_port": 6443,
        "ntp_servers": ["192.0.2.1"],
        "agent": {
            "control_url": "https://cloud-control.example.test",
            "image": "oci://registry.example.test/eph-agent:v1",
            "mtls_secret_ref": "eph-agent-mtls",
            "poll_seconds": 15,
            "agent_id": AGENT_ID,
            "installation_id": INSTALLATION_ID,
        },
    }
    spec.update(overrides)
    return spec


def test_kubeadm_init_renders_two_documents_with_networking():
    documents = render_kubeadm_init(_spec())
    assert [doc["kind"] for doc in documents] == ["InitConfiguration", "ClusterConfiguration"]
    init_config, cluster_config = documents
    assert init_config["localAPIEndpoint"]["advertiseAddress"] == "192.0.2.10"
    assert init_config["nodeRegistration"]["name"] == "eph-bootstrap-01"
    assert cluster_config["kubernetesVersion"] == "v1.31.2"
    assert cluster_config["controlPlaneEndpoint"] == "192.0.2.10:6443"
    assert cluster_config["networking"]["podSubnet"] == "10.244.0.0/16"
    assert cluster_config["networking"]["serviceSubnet"] == "10.96.0.0/12"


def test_cloud_init_uses_preinstalled_kubernetes_binaries_and_skips_kube_proxy():
    seed = render_cloud_init_seed(_spec())
    assert seed["meta-data"]["local-hostname"] == "eph-bootstrap-01"
    user_data = seed["user-data"]
    assert user_data["packages"] == []
    assert any(
        file["path"] == "/etc/kubernetes/kubeadm-init.yaml" for file in user_data["write_files"]
    )
    assert user_data["ntp"]["servers"] == ["192.0.2.1"]
    init = next(cmd for cmd in user_data["runcmd"] if "kubeadm init" in cmd)
    assert "--skip-phases=addon/kube-proxy" in init


def test_agent_deployment_injects_endpoint_role_and_mtls_volume():
    deployment = render_agent_deployment(_spec())
    assert deployment["kind"] == "Deployment"
    assert deployment["spec"]["replicas"] == 1
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["AGENT_CONTROL_URL"] == "https://cloud-control.example.test"
    assert env["AGENT_ROLE"] == "eph"
    assert env["AGENT_POLL_SECONDS"] == "15"
    assert env["AGENT_ID"] == AGENT_ID
    assert env["AGENT_INSTALLATION_ID"] == INSTALLATION_ID
    assert env["AGENT_INSTANCE_ID"] == "eph-bootstrap-01"
    assert env["AGENT_TLS_CA_FILE"] == "/etc/agent/mtls/ca.crt"
    volume = deployment["spec"]["template"]["spec"]["volumes"][0]
    assert volume["secret"]["secretName"] == "eph-agent-mtls"


def test_optional_ntp_and_instance_id_are_omitted_when_absent():
    spec = _spec(ntp_servers=None)
    spec["agent"] = {
        "control_endpoint": "https://cloud-control.example.test",
        "image": "oci://registry.example.test/eph-agent:v1",
        "mtls_secret_ref": "eph-agent-mtls",
        "poll_seconds": 15,
        "agent_id": AGENT_ID,
        "installation_id": INSTALLATION_ID,
    }
    seed = render_cloud_init_seed(spec)
    assert "ntp" not in seed["user-data"]
    deployment = render_agent_deployment(spec)
    env = {
        item["name"]: item["value"]
        for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["AGENT_INSTANCE_ID"] == "eph-bootstrap-01"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("kubernetes_version", "1.31.2", "EPH_BOOTSTRAP_K8S_VERSION_INVALID"),
        ("node_ip", "256.0.0.1", "EPH_BOOTSTRAP_NODE_IP_INVALID"),
        ("pod_cidr", "10.244.0.0/33", "EPH_BOOTSTRAP_POD_CIDR_INVALID"),
        ("service_cidr", "not-a-cidr", "EPH_BOOTSTRAP_SERVICE_CIDR_INVALID"),
        ("control_plane_endpoint", "bad host!", "EPH_BOOTSTRAP_ENDPOINT_INVALID"),
        ("control_plane_port", 70000, "EPH_BOOTSTRAP_PORT_INVALID"),
    ],
)
def test_renderer_rejects_invalid_node_fields(field, value, code):
    with pytest.raises(Metal3SkillError, match=code):
        render_kubeadm_init(_spec(**{field: value}))


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("control_endpoint", "ftp://host", "EPH_BOOTSTRAP_AGENT_ENDPOINT_INVALID"),
        (
            "control_endpoint",
            "http://cloud-control.example.test",
            "EPH_BOOTSTRAP_AGENT_ENDPOINT_INVALID",
        ),
        ("image", "registry.example.test/agent:v1", "EPH_BOOTSTRAP_AGENT_IMAGE_INVALID"),
        ("mtls_secret_ref", "Bad Secret", "EPH_BOOTSTRAP_AGENT_MTLS_SECRET_REF_INVALID"),
        ("poll_seconds", 0, "EPH_BOOTSTRAP_AGENT_POLL_SECONDS_INVALID"),
        ("poll_seconds", 301, "EPH_BOOTSTRAP_AGENT_POLL_SECONDS_INVALID"),
        ("heartbeat_seconds", 0, "EPH_BOOTSTRAP_AGENT_HEARTBEAT_SECONDS_INVALID"),
        ("request_timeout_seconds", 0, "EPH_BOOTSTRAP_AGENT_REQUEST_TIMEOUT_SECONDS_INVALID"),
        ("agent_id", "eph-bootstrap-01", "EPH_BOOTSTRAP_AGENT_ID_INVALID"),
        ("installation_id", "not-a-uuid", "EPH_BOOTSTRAP_AGENT_INSTALLATION_ID_INVALID"),
        ("instance_id", "", "EPH_BOOTSTRAP_AGENT_INSTANCE_ID_INVALID"),
    ],
)
def test_renderer_rejects_invalid_agent_block(field, value, code):
    spec = _spec()
    spec["agent"] = {**spec["agent"], field: value}
    with pytest.raises(Metal3SkillError, match=code):
        render_agent_deployment(spec)


def test_renderer_rejects_non_mapping_spec():
    with pytest.raises(Metal3SkillError, match="EPH_BOOTSTRAP_SPEC_INVALID"):
        render_kubeadm_init(["not", "a", "dict"])


def test_renderer_rejects_missing_agent_block():
    spec = _spec()
    del spec["agent"]
    with pytest.raises(Metal3SkillError, match="EPH_BOOTSTRAP_AGENT_BLOCK_REQUIRED"):
        render_agent_deployment(spec)
