import json
from uuid import uuid4

import httpx
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


def test_agent_client_renews_lease_with_current_identity_and_token(tmp_path, monkeypatch):
    from packages.agent_transport.client import AgentControlClient

    ca, cert, key = _write_cert_files(tmp_path)
    agent_id = uuid4()
    installation_id = uuid4()
    cluster_id = uuid4()
    values = {
        "AGENT_CONTROL_URL": "https://control.example.test",
        "AGENT_ID": str(agent_id),
        "AGENT_INSTALLATION_ID": str(installation_id),
        "AGENT_INSTANCE_ID": "target-master-0",
        "AGENT_ROLE": "target",
        "AGENT_CLUSTER_ID": str(cluster_id),
        "AGENT_TLS_CA_FILE": ca,
        "AGENT_TLS_CERT_FILE": cert,
        "AGENT_TLS_KEY_FILE": key,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = AgentSettings.from_environment("target")
    def handler(request):
        assert request.url.path == "/api/v1/agent-control/commands/lease/renew"
        payload = json.loads(request.content)
        assert payload["agent_id"] == str(agent_id)
        assert payload["lease_token"] == "x" * 40
        return httpx.Response(200, json={"renewed": True, "expires_at": "2026-11-17T12:00:00Z"})
    client = AgentControlClient(settings, transport=httpx.MockTransport(handler))
    try:
        result = client.renew_lease({
            "command_id": str(uuid4()),
            "lease": {"lease_id": str(uuid4()), "token": "x" * 40},
        })
        assert result["renewed"] is True
    finally:
        client.close()



def test_agent_client_persists_checkpoint_with_active_lease(tmp_path, monkeypatch):
    import json

    from packages.agent_transport.client import AgentControlClient

    ca, cert, key = _write_cert_files(tmp_path)
    agent_id = uuid4()
    cluster_id = uuid4()
    values = {
        "AGENT_CONTROL_URL": "https://control.example.test",
        "AGENT_ID": str(agent_id),
        "AGENT_INSTALLATION_ID": str(uuid4()),
        "AGENT_INSTANCE_ID": "target-master-0",
        "AGENT_ROLE": "target",
        "AGENT_CLUSTER_ID": str(cluster_id),
        "AGENT_TLS_CA_FILE": ca,
        "AGENT_TLS_CERT_FILE": cert,
        "AGENT_TLS_KEY_FILE": key,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = AgentSettings.from_environment("target")

    def handler(request):
        assert request.url.path == "/api/v1/agent-control/deployments/checkpoints"
        payload = json.loads(request.content)
        assert payload["agent_id"] == str(agent_id)
        assert payload["expected_phase"] == "eph_single_node_ready"
        assert payload["expected_sequence"] == 1
        assert payload["lease_token"] == "x" * 40
        return httpx.Response(
            200,
            json={
                "persisted": True,
                "already_applied": False,
                "phase": "target_control_plane_ready",
                "sequence": 2,
            },
        )

    client = AgentControlClient(settings, transport=httpx.MockTransport(handler))
    try:
        result = client.persist_checkpoint(
            {
                "deployment_id": str(uuid4()),
                "command_id": str(uuid4()),
                "lease": {"lease_id": str(uuid4()), "token": "x" * 40},
                "deployment_checkpoint": {
                    "phase": "eph_single_node_ready",
                    "sequence": 1,
                    "state": {},
                },
            },
            "target_control_plane_ready",
            {
                "target_control_plane_ready": True,
                "ready_master_count": 3,
                "ironic_internal_ntp_ip": "10.130.60.84",
                "target_control_plane_ntp_profile_verified": True,
                "target_control_plane_ntp_external_sync_verified": True,
                "ironic_internal_ntp_service_reachable": True,
            },
        )
        assert result["persisted"] is True
    finally:
        client.close()



def test_eph_agent_client_persists_only_initial_bootstrap_checkpoint(tmp_path, monkeypatch):
    from packages.agent_transport.client import AgentControlClient

    ca, cert, key = _write_cert_files(tmp_path)
    agent_id = uuid4()
    values = {
        "AGENT_CONTROL_URL": "https://control.example.test",
        "AGENT_ID": str(agent_id),
        "AGENT_INSTALLATION_ID": str(uuid4()),
        "AGENT_INSTANCE_ID": "eph-single-node-agent",
        "AGENT_ROLE": "eph",
        "AGENT_TLS_CA_FILE": ca,
        "AGENT_TLS_CERT_FILE": cert,
        "AGENT_TLS_KEY_FILE": key,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = AgentSettings.from_environment("eph")

    def handler(request):
        assert request.url.path == "/api/v1/agent-control/deployments/checkpoints"
        payload = json.loads(request.content)
        assert payload["agent_id"] == str(agent_id)
        assert payload["role"] == "eph"
        assert payload["cluster_id"] is None
        assert payload["expected_phase"] == "not_started"
        assert payload["expected_sequence"] == 0
        assert payload["phase"] == "eph_single_node_ready"
        return httpx.Response(
            200,
            json={
                "persisted": True,
                "already_applied": False,
                "phase": "eph_single_node_ready",
                "sequence": 1,
            },
        )

    client = AgentControlClient(settings, transport=httpx.MockTransport(handler))
    try:
        result = client.persist_checkpoint(
            {
                "deployment_id": str(uuid4()),
                "command_id": str(uuid4()),
                "lease": {"lease_id": str(uuid4()), "token": "x" * 40},
                "deployment_checkpoint": {
                    "phase": "not_started",
                    "sequence": 0,
                    "state": {},
                },
            },
            "eph_single_node_ready",
            {
                "eph_ready": True,
                "eph_node_count": 1,
                "eph_source_preflight_passed": True,
            },
        )
        assert result["persisted"] is True
    finally:
        client.close()
