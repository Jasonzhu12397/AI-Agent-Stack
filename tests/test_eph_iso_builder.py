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
        BOOTSTRAP["validate_config"](config)


def test_bootstrap_does_not_accept_missing_fields():
    with pytest.raises(BOOTSTRAP["BootstrapError"], match="BOOTSTRAP_CONFIG_FIELDS_MISSING"):
        BOOTSTRAP["validate_config"]({"node_id": str(uuid.uuid4())})


def test_systemd_starts_k3s_and_bootstrap_from_multi_user_target():
    dropin = (
        ROOT
        / "eph-iso/config/includes.chroot/etc/systemd/system/multi-user.target.d/eph.conf"
    )
    content = dropin.read_text(encoding="utf-8")
    assert "k3s.service eph-bootstrap.service" in content


def test_bootstrap_agent_manifest_is_bundled_into_iso():
    script = (ROOT / "eph-iso/build.sh").read_text(encoding="utf-8")
    assert "deploy/agents/namespace.yaml" in script
    assert "eph-rbac.yaml" in script
    assert "deploy/agents/eph-node-agent.yaml" in script
    assert "usr/share/ai-agent-studio/eph-manifests" in script


def test_eph_cloud_init_nocloud_seed_configures_node_before_agent_bootstrap():
    package_list = (
        ROOT / "eph-iso/config/package-lists/eph-node.list.chroot"
    ).read_text(encoding="utf-8")
    datasource = (
        ROOT
        / "eph-iso/config/includes.chroot/etc/cloud/cloud.cfg.d/90-eph-nocloud.cfg"
    ).read_text(encoding="utf-8")
    service = (
        ROOT
        / "eph-iso/config/includes.chroot/etc/systemd/system/eph-bootstrap.service"
    ).read_text(encoding="utf-8")
    multi_user = (
        ROOT
        / "eph-iso/config/includes.chroot/etc/systemd/system/multi-user.target.d/eph.conf"
    ).read_text(encoding="utf-8")
    assert "cloud-init" in package_list
    assert "NoCloud" in datasource
    assert "CIDATA" in datasource
    assert "cloud-init.target" in service
    assert "After=cloud-init.target" in service
    assert "cloud-init status --wait" in service
    assert "k3s.service eph-bootstrap.service" in multi_user
    assert 'MEDIA_ROOT = pathlib.Path("/etc/ai-agent-studio/eph-bootstrap")' in (
        ROOT / "eph-iso/bootstrap.py"
    ).read_text(encoding="utf-8")
    for name in ("user-data", "meta-data", "network-config"):
        assert (ROOT / "eph-iso/examples/cloud-init" / name).is_file()


def test_eph_agent_capi_pivot_secret_is_optional_until_target_cluster_exists():
    manifest = (ROOT / "deploy/agents/eph-node-agent.yaml").read_text(encoding="utf-8")
    assert "secretName: capi-pivot-kubeconfigs" in manifest
    assert "optional: true" in manifest
    bootstrap = (ROOT / "eph-iso/bootstrap.py").read_text(encoding="utf-8")
    assert "CAPI_PIVOT_KUBECONFIG_SET_INCOMPLETE" in bootstrap
    assert "if capi_files:" in bootstrap



def test_bootstrap_generates_local_key_and_exchanges_csr_for_node_identity(
    monkeypatch,
    tmp_path,
):
    import json

    certificate = "-----BEGIN CERTIFICATE-----\nMAA=\n-----END CERTIFICATE-----\n"
    fingerprint = "a" * 64
    response_body = json.dumps(
        {
            "node_id": valid_config()["node_id"],
            "client_certificate": certificate,
            "ca_chain": certificate,
            "fingerprint_sha256": fingerprint,
            "agent_token": "b" * 64,
            "token_consumed": True,
        }
    ).encode("utf-8")
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self, limit):
            return response_body

    class FakeOpener:
        def open(self, request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse()

    config = valid_config()
    response_data = json.loads(response_body)
    response_data["node_id"] = config["node_id"]
    response_body = json.dumps(response_data).encode("utf-8")
    key_path = tmp_path / "tls.key"
    csr_path = tmp_path / "tls.csr"

    def fake_run(arguments, input_text=None):
        if arguments[1] == "genpkey":
            key_path.write_text("LOCAL PRIVATE KEY", encoding="ascii")
        elif arguments[1] == "req":
            csr_path.write_text(
                "-----BEGIN CERTIFICATE REQUEST-----\nMAA=\n-----END CERTIFICATE REQUEST-----\n",
                encoding="ascii",
            )

    output_values = iter(
        [
            "sha256 Fingerprint=" + ":".join(
                fingerprint[index:index + 2].upper()
                for index in range(0, len(fingerprint), 2)
            ),
            "PUBLIC KEY",
            "PUBLIC KEY",
        ]
    )
    bootstrap_globals = BOOTSTRAP["_request_client_certificate"].__globals__
    monkeypatch.setitem(bootstrap_globals, "_run", fake_run)
    monkeypatch.setitem(
        bootstrap_globals,
        "_run_output",
        lambda arguments: next(output_values),
    )
    monkeypatch.setattr(
        BOOTSTRAP["ssl"],
        "create_default_context",
        lambda cafile: BOOTSTRAP["ssl"].SSLContext(
            BOOTSTRAP["ssl"].PROTOCOL_TLS_CLIENT
        ),
    )
    monkeypatch.setattr(
        BOOTSTRAP["urllib"].request,
        "build_opener",
        lambda *handlers: FakeOpener(),
    )

    control_ca_path = tmp_path / "control-ca.crt"
    control_ca_path.write_text(certificate, encoding="ascii")
    result = BOOTSTRAP["_request_client_certificate"](
        config,
        tmp_path,
        control_ca_path,
    )

    request = captured["request"]
    payload = json.loads(request.data)
    assert request.full_url.endswith(
        "/api/v1/agent-runtime/nodes/{}/certificate/".format(config["node_id"])
    )
    assert request.get_header("Authorization") == "Bearer " + config["enrollment_token"]
    assert payload["csr_pem"].startswith("-----BEGIN CERTIFICATE REQUEST-----")
    assert "LOCAL PRIVATE KEY" not in request.data.decode("utf-8")
    assert result["agent_token"] == "b" * 64
    assert result["tls.key"] == key_path
    assert result["tls.crt"].read_text(encoding="ascii") == certificate + certificate
    assert key_path.stat().st_mode & 0o777 == 0o600

@pytest.mark.parametrize(
    "response",
    [None, [], "{}", 1, {"agent_token": "a" * 64}],
)
def test_bootstrap_rejects_non_object_or_incomplete_certificate_response(response):
    with pytest.raises(BOOTSTRAP["BootstrapError"], match="CLIENT_CERTIFICATE_RESPONSE_INVALID"):
        BOOTSTRAP["_validate_certificate_exchange_response"](
            response,
            str(uuid.uuid4()),
        )



@pytest.mark.parametrize(
    "change",
    [
        {"node_id": str(uuid.uuid4())},
        {"token_consumed": False},
        {"agent_token": "short"},
        {"agent_token": None},
        {"client_certificate": None},
        {"ca_chain": []},
        {"fingerprint_sha256": "invalid"},
    ],
)
def test_bootstrap_rejects_untrusted_certificate_exchange_fields(change):
    node_id = str(uuid.uuid4())
    response = {
        "node_id": node_id,
        "client_certificate": (
            "-----BEGIN CERTIFICATE-----\\nMAA=\\n-----END CERTIFICATE-----\\n"
        ),
        "ca_chain": (
            "-----BEGIN CERTIFICATE-----\\nMAA=\\n-----END CERTIFICATE-----\\n"
        ),
        "fingerprint_sha256": "a" * 64,
        "agent_token": "b" * 64,
        "token_consumed": True,
    }
    response.update(change)
    with pytest.raises(
        BOOTSTRAP["BootstrapError"],
        match="CLIENT_CERTIFICATE_RESPONSE_INVALID",
    ):
        BOOTSTRAP["_validate_certificate_exchange_response"](response, node_id)
