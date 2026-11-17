from uuid import uuid4

import pytest

from packages.agent_transport.config import AgentSettings, ConfigurationError


def _write_cert_files(tmp_path):
    paths = []
    for name in ("ca.crt", "tls.crt", "tls.key"):
        path = tmp_path / name
        path.write_text("test", encoding="utf-8")
        paths.append(str(path))
    return paths


def test_settings_require_https_and_valid_agent_scope(monkeypatch, tmp_path):
    ca, cert, key = _write_cert_files(tmp_path)
    values = {
        "AGENT_CONTROL_URL": "https://control.example.test",
        "AGENT_ID": str(uuid4()),
        "AGENT_INSTALLATION_ID": str(uuid4()),
        "AGENT_INSTANCE_ID": "eph-pod-0",
        "AGENT_ROLE": "eph",
        "AGENT_TLS_CA_FILE": ca,
        "AGENT_TLS_CERT_FILE": cert,
        "AGENT_TLS_KEY_FILE": key,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = AgentSettings.from_environment("eph")
    assert settings.role == "eph"
    assert settings.cluster_id is None


def test_settings_reject_http_control_endpoint(monkeypatch, tmp_path):
    ca, cert, key = _write_cert_files(tmp_path)
    values = {
        "AGENT_CONTROL_URL": "http://control.example.test",
        "AGENT_ID": str(uuid4()),
        "AGENT_INSTALLATION_ID": str(uuid4()),
        "AGENT_INSTANCE_ID": "target-master-0",
        "AGENT_ROLE": "target",
        "AGENT_CLUSTER_ID": str(uuid4()),
        "AGENT_TLS_CA_FILE": ca,
        "AGENT_TLS_CERT_FILE": cert,
        "AGENT_TLS_KEY_FILE": key,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError):
        AgentSettings.from_environment("target")


def test_settings_rejects_target_without_cluster_id(monkeypatch, tmp_path):
    ca, cert, key = _write_cert_files(tmp_path)
    values = {
        "AGENT_CONTROL_URL": "https://control.example.test",
        "AGENT_ID": str(uuid4()),
        "AGENT_INSTALLATION_ID": str(uuid4()),
        "AGENT_INSTANCE_ID": "target-master-0",
        "AGENT_ROLE": "target",
        "AGENT_TLS_CA_FILE": ca,
        "AGENT_TLS_CERT_FILE": cert,
        "AGENT_TLS_KEY_FILE": key,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError):
        AgentSettings.from_environment("target")
