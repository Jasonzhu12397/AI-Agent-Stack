import json
import urllib.error

import pytest

from apps.node_agent.ironic_client import (
    IronicObservationError,
    get_ironic_node,
    ironic_api_configured,
)


NODE_UUID = "12345678-1234-1234-1234-123456789abc"


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, size):
        return self.payload


def configured_environment():
    return {
        "IRONIC_API_URL": "https://ironic.example.test",
        "IRONIC_API_TOKEN": "test-token",
        "IRONIC_API_MICROVERSION": "1.82",
    }


def test_ironic_api_configuration_requires_endpoint_and_token():
    assert ironic_api_configured({}) is False
    assert ironic_api_configured(configured_environment()) is True
    with pytest.raises(
        IronicObservationError,
        match="IRONIC_API_CONFIGURATION_INCOMPLETE",
    ):
        ironic_api_configured({"IRONIC_API_URL": "https://ironic.example.test"})


def test_get_ironic_node_uses_tls_token_and_read_only_get(monkeypatch):
    response = {
        "uuid": NODE_UUID,
        "provision_state": "active",
        "maintenance": False,
        "target_provision_state": None,
        "last_error": None,
        "maintenance_reason": None,
    }
    captured = {}

    def fake_urlopen(request, context, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse(json.dumps(response).encode("utf-8"))

    monkeypatch.setattr(
        "apps.node_agent.ironic_client.urllib.request.urlopen",
        fake_urlopen,
    )
    result = get_ironic_node(NODE_UUID, environ=configured_environment())
    request = captured["request"]
    assert request.full_url.endswith("/v1/nodes/" + NODE_UUID)
    assert request.get_method() == "GET"
    assert request.get_header("X-auth-token") == "test-token"
    assert request.get_header("X-openstack-ironic-api-version") == "1.82"
    assert captured["timeout"] == 10
    assert result["provision_state"] == "active"
    assert result["maintenance"] is False
    assert "token" not in result


def test_get_ironic_node_rejects_non_tls_endpoint():
    environment = configured_environment()
    environment["IRONIC_API_URL"] = "http://ironic.example.test"
    with pytest.raises(IronicObservationError, match="IRONIC_API_URL_INVALID"):
        get_ironic_node(NODE_UUID, environ=environment)


def test_get_ironic_node_rejects_invalid_uuid():
    with pytest.raises(IronicObservationError, match="IRONIC_NODE_UUID_INVALID"):
        get_ironic_node("not-a-uuid", environ=configured_environment())


def test_get_ironic_node_rejects_identity_mismatch(monkeypatch):
    response = {
        "uuid": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "provision_state": "active",
        "maintenance": False,
    }
    monkeypatch.setattr(
        "apps.node_agent.ironic_client.urllib.request.urlopen",
        lambda *args, **kwargs: FakeResponse(json.dumps(response).encode("utf-8")),
    )
    with pytest.raises(
        IronicObservationError,
        match="IRONIC_NODE_IDENTITY_MISMATCH",
    ):
        get_ironic_node(NODE_UUID, environ=configured_environment())


def test_get_ironic_node_maps_http_errors_to_safe_codes(monkeypatch):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://ironic.example.test",
            503,
            "unavailable",
            {},
            None,
        )

    monkeypatch.setattr(
        "apps.node_agent.ironic_client.urllib.request.urlopen",
        fail,
    )
    with pytest.raises(IronicObservationError, match="IRONIC_API_HTTP_503"):
        get_ironic_node(NODE_UUID, environ=configured_environment())


def test_get_ironic_node_rejects_invalid_json(monkeypatch):
    monkeypatch.setattr(
        "apps.node_agent.ironic_client.urllib.request.urlopen",
        lambda *args, **kwargs: FakeResponse(b"not-json"),
    )
    with pytest.raises(
        IronicObservationError,
        match="IRONIC_API_RESPONSE_INVALID_JSON",
    ):
        get_ironic_node(NODE_UUID, environ=configured_environment())


def test_get_ironic_node_rejects_invalid_timeout():
    environment = configured_environment()
    environment["IRONIC_API_TIMEOUT_SECONDS"] = "0"
    with pytest.raises(
        IronicObservationError,
        match="IRONIC_API_TIMEOUT_INVALID",
    ):
        get_ironic_node(NODE_UUID, environ=environment)
