"""Guard that a rendered EPH Agent Deployment can actually boot the transport."""

import os
from pathlib import Path

import pytest

from apps.eph_agent.bootstrap_renderer import render_agent_deployment
from packages.agent_transport.config import AgentSettings, ConfigurationError

AGENT_ID = "0b8a5dc2-6f6f-4b4e-9a4c-3f6a1b7c8d90"
INSTALLATION_ID = "1f0c2a44-8b1d-4c3e-8f5a-2b6c9d0e1f22"


def _spec(**agent_overrides: object) -> dict:
    agent = {
        "control_endpoint": "https://cloud-control.example.test",
        "image": "oci://registry.example.test/eph-agent:v1",
        "mtls_secret_ref": "eph-agent-mtls",
        "agent_id": AGENT_ID,
        "installation_id": INSTALLATION_ID,
    }
    agent.update(agent_overrides)
    return {
        "node_name": "eph-bootstrap-01",
        "kubernetes_version": "v1.31.2",
        "node_ip": "192.0.2.10",
        "pod_cidr": "10.244.0.0/16",
        "service_cidr": "10.96.0.0/12",
        "control_plane_endpoint": "192.0.2.10",
        "control_plane_port": 6443,
        "agent": agent,
    }


def _render_env(spec: dict) -> dict[str, str]:
    container = render_agent_deployment(spec)["spec"]["template"]["spec"]["containers"][0]
    return {item["name"]: item["value"] for item in container["env"]}


def _materialize_secret(tmp_path: Path, env: dict[str, str]) -> dict[str, str]:
    files = {
        "AGENT_TLS_CA_FILE": "ca.crt",
        "AGENT_TLS_CERT_FILE": "tls.crt",
        "AGENT_TLS_KEY_FILE": "tls.key",
    }
    resolved = {}
    for variable, filename in files.items():
        path = tmp_path / filename
        path.write_text("placeholder\n", encoding="utf-8")
        assert env[variable].endswith("/" + filename)
        resolved[variable] = str(path)
    return resolved


def test_rendered_environment_is_accepted_by_agent_settings(tmp_path, monkeypatch):
    spec = _spec()
    env = _render_env(spec)
    file_env = _materialize_secret(tmp_path, env)
    for key, value in env.items():
        if key.startswith("AGENT_TLS_"):
            continue
        monkeypatch.setenv(key, value)
    for key, value in file_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AGENT_CLUSTER_ID", raising=False)

    settings = AgentSettings.from_environment("eph")

    assert settings.role == "eph"
    assert str(settings.agent_id) == AGENT_ID
    assert str(settings.installation_id) == INSTALLATION_ID
    assert settings.instance_id == "eph-bootstrap-01"
    assert settings.cluster_id is None
    assert settings.control_url == "https://cloud-control.example.test"
    assert settings.poll_seconds == 5.0
    assert settings.heartbeat_seconds == 10.0
    assert settings.request_timeout_seconds == 15.0


def test_rendered_environment_never_sets_target_only_cluster_id(monkeypatch):
    env = _render_env(_spec())
    assert "AGENT_CLUSTER_ID" not in env
    monkeypatch.setenv("AGENT_CLUSTER_ID", INSTALLATION_ID)
    monkeypatch.setenv("AGENT_CONTROL_URL", env["AGENT_CONTROL_URL"])
    monkeypatch.setenv("AGENT_ROLE", env["AGENT_ROLE"])
    monkeypatch.setenv("AGENT_ID", env["AGENT_ID"])
    monkeypatch.setenv("AGENT_INSTALLATION_ID", env["AGENT_INSTALLATION_ID"])
    monkeypatch.setenv("AGENT_INSTANCE_ID", env["AGENT_INSTANCE_ID"])
    monkeypatch.setenv("AGENT_TLS_CA_FILE", __file__)
    monkeypatch.setenv("AGENT_TLS_CERT_FILE", __file__)
    monkeypatch.setenv("AGENT_TLS_KEY_FILE", __file__)

    with pytest.raises(ConfigurationError, match="must not be assigned"):
        AgentSettings.from_environment("eph")


def test_rendered_tls_paths_live_inside_the_mounted_secret():
    deployment = render_agent_deployment(_spec())
    pod_spec = deployment["spec"]["template"]["spec"]
    mount = pod_spec["containers"][0]["volumeMounts"][0]
    assert mount["readOnly"] is True
    env = {item["name"]: item["value"] for item in pod_spec["containers"][0]["env"]}
    for variable in ("AGENT_TLS_CA_FILE", "AGENT_TLS_CERT_FILE", "AGENT_TLS_KEY_FILE"):
        assert env[variable].startswith(mount["mountPath"] + "/")
    secret_name = pod_spec["volumes"][0]["secret"]["secretName"]
    assert secret_name == "eph-agent-mtls"


def test_instance_id_override_round_trips_through_settings(tmp_path, monkeypatch):
    env = _render_env(_spec(instance_id="eph-live-iso-node-7"))
    assert env["AGENT_INSTANCE_ID"] == "eph-live-iso-node-7"
    file_env = _materialize_secret(tmp_path, env)
    for key, value in env.items():
        if key.startswith("AGENT_TLS_"):
            continue
        monkeypatch.setenv(key, value)
    for key, value in file_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AGENT_CLUSTER_ID", raising=False)

    settings = AgentSettings.from_environment("eph")
    assert settings.instance_id == "eph-live-iso-node-7"
    assert os.path.basename(settings.ca_file) == "ca.crt"
