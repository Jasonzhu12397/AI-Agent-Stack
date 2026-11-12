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
    assert "console=ttyS0,115200n8" in script
    assert "systemd.show_status=1" in script
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


def test_eph_k3s_service_requires_ram_mounts_before_start():
    root = ROOT / "eph-iso/config/includes.chroot/etc/systemd/system"
    k3s = (root / "k3s.service").read_text(encoding="utf-8")
    rancher = (root / "var-lib-rancher-k3s.mount").read_text(encoding="utf-8")
    kubelet = (root / "var-lib-kubelet.mount").read_text(encoding="utf-8")

    assert "Requires=var-lib-rancher-k3s.mount var-lib-kubelet.mount" in k3s
    assert "After=network-online.target var-lib-rancher-k3s.mount var-lib-kubelet.mount" in k3s
    assert "Where=/var/lib/rancher/k3s" in rancher
    assert "Where=/var/lib/kubelet" in kubelet
    assert "Options=mode=0750,size=60%,nosuid,nodev" in rancher
    assert "Options=mode=0750,size=15%,nosuid,nodev" in kubelet
    assert "Before=k3s.service" in rancher
    assert "Before=k3s.service" in kubelet


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
    assert "Restart=on-failure" in service
    assert "RestartSec=30s" in service
    assert "TimeoutStartSec=40min" in service
    assert "StartLimitBurst=10" in service
    assert "k3s.service eph-bootstrap.service" in multi_user
    assert 'MEDIA_ROOT = pathlib.Path("/etc/ai-agent-studio/eph-bootstrap")' in (
        ROOT / "eph-iso/bootstrap.py"
    ).read_text(encoding="utf-8")
    for name in ("user-data", "meta-data", "network-config"):
        assert (ROOT / "eph-iso/examples/cloud-init" / name).is_file()
    user_data = (ROOT / "eph-iso/examples/cloud-init/user-data").read_text(encoding="utf-8")
    assert "agent-ca.crt" in user_data
    assert "REPLACE_WITH_INDEPENDENT_EPH_AGENT_ISSUING_CA_ROOT" in user_data


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

    captured_commands = []

    def fake_run(arguments, input_text=None):
        captured_commands.append(list(arguments))
        if arguments[1] == "genpkey":
            key_path.write_text("LOCAL PRIVATE KEY", encoding="ascii")
        elif arguments[1] == "req":
            csr_path.write_text(
                "-----BEGIN CERTIFICATE REQUEST-----\nMAA=\n-----END CERTIFICATE REQUEST-----\n",
                encoding="ascii",
            )

    output_values = iter(
        [
            "subject=CN=agent-node-{}.agents.internal".format(config["node_id"]),
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
    captured["handlers"] = []
    monkeypatch.setattr(
        BOOTSTRAP["urllib"].request,
        "build_opener",
        lambda *handlers: captured["handlers"].extend(handlers) or FakeOpener(),
    )

    control_ca_path = tmp_path / "control-ca.crt"
    control_ca_path.write_text(certificate, encoding="ascii")
    issuer_ca_path = tmp_path / "issuer-ca.crt"
    issuer_ca_path.write_text(certificate, encoding="ascii")
    result = BOOTSTRAP["_request_client_certificate"](
        config,
        tmp_path,
        control_ca_path,
        issuer_ca_path,
    )

    request = captured["request"]
    payload = json.loads(request.data)
    redirect_handler = next(
        handler for handler in captured["handlers"]
        if isinstance(handler, BOOTSTRAP["_NoRedirectHandler"])
    )
    assert redirect_handler.redirect_request(
        None, None, 302, "Found", {}, "https://attacker.example/"
    ) is None
    assert request.full_url.endswith(
        "/api/v1/agent-runtime/nodes/{}/certificate/".format(config["node_id"])
    )
    assert request.get_header("Authorization") == "Bearer " + config["enrollment_token"]
    assert payload["csr_pem"].startswith("-----BEGIN CERTIFICATE REQUEST-----")
    assert "LOCAL PRIVATE KEY" not in request.data.decode("utf-8")
    assert result["agent_token"] == "b" * 64
    assert any(
        command[:2] == ["openssl", "verify"]
        and "-purpose" in command
        and "sslclient" in command
        and "-x509_strict" in command
        and "-verify_depth" in command
        and str(issuer_ca_path) in command
        for command in captured_commands
    )
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



def _prepare_bootstrap_runtime(monkeypatch, tmp_path, fail_rollout=False):
    import json
    import subprocess

    media_root = tmp_path / "media"
    manifest_root = tmp_path / "manifests"
    (media_root / "tls").mkdir(parents=True)
    manifest_root.mkdir()
    (media_root / "bootstrap.json").write_text(
        json.dumps(valid_config()),
        encoding="utf-8",
    )
    (media_root / "tls" / "ca.crt").write_text(
        "CONTROL CA",
        encoding="ascii",
    )
    (media_root / "tls" / "agent-ca.crt").write_text(
        "INDEPENDENT AGENT CA",
        encoding="ascii",
    )
    for name in ("namespace.yaml", "rbac.yaml", "eph-node-agent.yaml"):
        (manifest_root / name).write_text(name, encoding="utf-8")

    secret_files = {}
    for name, content in (
        ("ca.crt", "CONTROL CA"),
        ("client-ca.crt", "INDEPENDENT AGENT CA"),
        ("tls.crt", "CLIENT CERTIFICATE"),
        ("tls.key", "LOCAL PRIVATE KEY"),
    ):
        path = tmp_path / name
        path.write_text(content, encoding="ascii")
        secret_files[name] = path
    secret_files["agent_token"] = "b" * 64

    events = []

    def fake_run(arguments, input_text=None):
        events.append(("run", list(arguments), input_text))
        if fail_rollout and "rollout" in arguments:
            raise subprocess.CalledProcessError(1, arguments)

    def fake_create_secret(name, namespace, files):
        events.append(("secret", name, namespace, sorted(files)))

    def fake_apply_file(path, image=None):
        events.append(("manifest", pathlib.Path(path).name, image))

    bootstrap_globals = BOOTSTRAP["bootstrap"].__globals__
    monkeypatch.setitem(bootstrap_globals, "MEDIA_ROOT", media_root)
    monkeypatch.setitem(bootstrap_globals, "MANIFEST_ROOT", manifest_root)
    monkeypatch.setitem(bootstrap_globals, "_run", fake_run)
    monkeypatch.setitem(
        bootstrap_globals,
        "_wait_for_kubernetes_api",
        lambda: events.append(("api_ready",)),
    )
    monkeypatch.setitem(bootstrap_globals, "_create_secret", fake_create_secret)
    monkeypatch.setitem(
        bootstrap_globals,
        "_wait_for_agent_pod",
        lambda: events.append(("agent_pod_ready",)),
    )
    monkeypatch.setitem(bootstrap_globals, "_apply_file", fake_apply_file)
    monkeypatch.setitem(
        bootstrap_globals,
        "_request_client_certificate",
        lambda config, secret_dir, control_ca_path, issuer_ca_path: dict(secret_files),
    )
    return events


def test_bootstrap_applies_agent_resources_and_waits_for_deployment_rollout(
    monkeypatch,
    tmp_path,
):
    events = _prepare_bootstrap_runtime(monkeypatch, tmp_path)

    BOOTSTRAP["bootstrap"]()

    assert events[0] == ("api_ready",)
    assert events[1][0:2] == ("run", [
        "k3s",
        "kubectl",
        "--kubeconfig",
        "/etc/rancher/k3s/k3s.yaml",
        "wait",
        "--for=condition=Ready",
        "nodes",
        "--all",
        "--timeout=300s",
    ])
    assert events[2][0:2] == ("run", [
        "k3s",
        "kubectl",
        "--kubeconfig",
        "/etc/rancher/k3s/k3s.yaml",
        "apply",
        "-f",
        str(tmp_path / "manifests" / "namespace.yaml"),
    ])
    assert events[3][0:2] == ("run", [
        "k3s",
        "kubectl",
        "--kubeconfig",
        "/etc/rancher/k3s/k3s.yaml",
        "apply",
        "-f",
        str(tmp_path / "manifests" / "rbac.yaml"),
    ])
    assert ("secret", "eph-node-agent-credentials", "ai-agent-system", [
        "control-url",
        "node-id",
        "token",
    ]) in events
    assert ("secret", "agent-control-plane-mtls", "ai-agent-system", [
        "ca.crt",
        "client-ca.crt",
        "tls.crt",
        "tls.key",
    ]) in events
    assert any(
        event[0] == "manifest"
        and event[1] == "eph-node-agent.yaml"
        and event[2].endswith("@sha256:" + "a" * 64)
        for event in events
    )
    rollout = [
        event for event in events
        if event[0] == "run" and "rollout" in event[1]
    ]
    assert len(rollout) == 1
    assert rollout[0][1][-3:] == [
        "status",
        "deployment/eph-node-agent",
        "--timeout=10m",
    ]
    assert ("agent_pod_ready",) in events


def test_bootstrap_requires_explicit_agent_pod_readiness(monkeypatch):
    bootstrap_globals = BOOTSTRAP["_wait_for_agent_pod"].__globals__
    commands = []
    outputs = iter(["eph-node-agent-7d9f"])
    monkeypatch.setitem(
        bootstrap_globals,
        "_run",
        lambda command, input_text=None: commands.append(list(command)),
    )
    monkeypatch.setitem(
        bootstrap_globals,
        "_run_output",
        lambda command, timeout=None: commands.append(list(command))
        or next(outputs),
    )

    BOOTSTRAP["_wait_for_agent_pod"]()

    assert commands[0][-5:] == [
        "wait",
        "--for=condition=Ready",
        "pod",
        "--selector=app.kubernetes.io/name=eph-node-agent",
        "--timeout=300s",
    ]
    assert commands[1][-6:] == [
        "get",
        "pods",
        "--selector=app.kubernetes.io/name=eph-node-agent",
        "--field-selector=status.phase=Running",
        "-o",
        "jsonpath={.items[0].metadata.name}",
    ]


def test_bootstrap_rejects_agent_pod_without_ready_running_instance(monkeypatch):
    bootstrap_globals = BOOTSTRAP["_wait_for_agent_pod"].__globals__
    monkeypatch.setitem(bootstrap_globals, "_run", lambda command, input_text=None: None)
    monkeypatch.setitem(bootstrap_globals, "_run_output", lambda command, timeout=None: "")
    with pytest.raises(
        BOOTSTRAP["BootstrapError"],
        match="EPH_AGENT_POD_NOT_READY",
    ):
        BOOTSTRAP["_wait_for_agent_pod"]()


def test_bootstrap_fails_when_agent_deployment_rollout_fails(
    monkeypatch,
    tmp_path,
):
    import subprocess

    _prepare_bootstrap_runtime(monkeypatch, tmp_path, fail_rollout=True)

    with pytest.raises(subprocess.CalledProcessError):
        BOOTSTRAP["bootstrap"]()


def test_bootstrap_rejects_partial_capi_pivot_kubeconfig_pair(
    monkeypatch,
    tmp_path,
):
    media_root = tmp_path / "media"
    events = _prepare_bootstrap_runtime(monkeypatch, tmp_path)
    (media_root / "capi").mkdir()
    (media_root / "capi" / "source.kubeconfig").write_text(
        "source kubeconfig",
        encoding="utf-8",
    )

    with pytest.raises(
        BOOTSTRAP["BootstrapError"],
        match="CAPI_PIVOT_KUBECONFIG_SET_INCOMPLETE",
    ):
        BOOTSTRAP["bootstrap"]()

    assert not any(
        event[0] == "secret" and event[1] == "capi-pivot-kubeconfigs"
        for event in events
    )


def test_bootstrap_creates_capi_pivot_secret_only_when_both_kubeconfigs_exist(
    monkeypatch,
    tmp_path,
):
    media_root = tmp_path / "media"
    events = _prepare_bootstrap_runtime(monkeypatch, tmp_path)
    (media_root / "capi").mkdir()
    for name in ("source.kubeconfig", "target.kubeconfig"):
        (media_root / "capi" / name).write_text(name, encoding="utf-8")

    BOOTSTRAP["bootstrap"]()

    assert (
        "secret",
        "capi-pivot-kubeconfigs",
        "ai-agent-system",
        ["source.kubeconfig", "target.kubeconfig"],
    ) in events


def test_ci_k3s_smoke_callback_requires_ready_node_and_workload():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "path: /usr/local/sbin/eph-ci-smoke.sh" in workflow
    assert "set -eu" in workflow
    assert 'k3s kubectl wait --for=condition=Ready "node/$NODE_NAME" --timeout=180s' in workflow
    assert "rollout status deployment/workload-smoke --timeout=180s" in workflow
    assert "wait --for=condition=Ready pod --selector=app=workload-smoke --timeout=180s" in workflow
    assert "test -n \"$READY_PODS\"" in workflow
    assert "K3S_API_READY=true" in workflow
    assert "K3S_WORKLOAD_SMOKE_READY=true" in workflow
    assert "EPH_AGENT_POD_READY=NOT_TESTED_NO_ENROLLMENT_SEED" in workflow
    callback = "curl --fail --silent --show-error --max-time 10 "
    callback += "http://10.0.2.2:18080/cloud-init-ready"
    assert callback in workflow
    assert "- [bash, /usr/local/sbin/eph-ci-smoke.sh]" in workflow


def test_eph_network_config_runs_before_systemd_networkd():
    root = ROOT / "eph-iso/config/includes.chroot/etc/systemd/system"
    service = (root / "eph-network-config.service").read_text(encoding="utf-8")
    drop_in = (
        root / "systemd-networkd.service.d/eph.conf"
    ).read_text(encoding="utf-8")
    assert "Wants=mnt-eph\\x2dbootstrap.mount" in service
    assert "After=mnt-eph\\x2dbootstrap.mount" in service
    assert "Before=systemd-networkd.service" in service
    assert "Requires=eph-network-config.service" in drop_in
    assert "After=eph-network-config.service" in drop_in
    mount = (root / "mnt-eph\\x2dbootstrap.mount").read_text(encoding="utf-8")
    assert "x-systemd.device-timeout=10s" in mount



def test_bootstrap_retries_when_kubernetes_api_probe_times_out(monkeypatch):
    import subprocess

    bootstrap_globals = BOOTSTRAP["_wait_for_kubernetes_api"].__globals__
    attempts = []

    def fake_run_output(command, timeout=None):
        attempts.append((list(command), timeout))
        if len(attempts) == 1:
            raise subprocess.TimeoutExpired(command, timeout)
        return "ok"

    monkeypatch.setitem(bootstrap_globals, "_run_output", fake_run_output)
    monkeypatch.setattr(bootstrap_globals["time"], "sleep", lambda seconds: None)

    BOOTSTRAP["_wait_for_kubernetes_api"]()

    assert len(attempts) == 2
    assert all(item[1] == 10 for item in attempts)

def test_bootstrap_waits_for_kubernetes_api_readiness(monkeypatch):
    bootstrap_globals = BOOTSTRAP["_wait_for_kubernetes_api"].__globals__
    responses = iter(["not-ready", "ok"])
    commands = []
    monkeypatch.setitem(
        bootstrap_globals,
        "_run_output",
        lambda command, timeout=None: commands.append((list(command), timeout))
        or next(responses),
    )
    monkeypatch.setattr(bootstrap_globals["time"], "sleep", lambda seconds: None)

    BOOTSTRAP["_wait_for_kubernetes_api"]()

    assert len(commands) == 2
    assert commands[0][0][-1] == "--raw=/readyz"
    assert commands[1][0][-1] == "--raw=/readyz"
    assert all(command[1] == 10 for command in commands)
