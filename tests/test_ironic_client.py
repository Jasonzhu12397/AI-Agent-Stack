import json
import urllib.error

import pytest
from node_agent.ironic_client import (
    IronicObservationError,
    _NoRedirectHandler,
    get_ironic_node,
    ironic_api_configured,
)

NODE_UUID = "12345678-1234-1234-1234-123456789abc"


class FakeHeaders(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class FakeResponse:
    def __init__(self, payload, content_type="application/json"):
        self.payload = payload
        self.headers = FakeHeaders({"Content-Type": content_type})

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


def active_node():
    return {
        "uuid": NODE_UUID,
        "provision_state": "active",
        "maintenance": False,
        "target_provision_state": None,
        "last_error": None,
        "maintenance_reason": None,
    }


def test_ironic_api_configuration_requires_endpoint_and_token():
    assert ironic_api_configured({}) is False
    assert ironic_api_configured(configured_environment()) is True
    with pytest.raises(
        IronicObservationError,
        match="IRONIC_API_CONFIGURATION_INCOMPLETE",
    ):
        ironic_api_configured({"IRONIC_API_URL": "https://ironic.example.test"})


def test_get_ironic_node_uses_tls_token_and_read_only_get():
    captured = {}

    def fake_opener(request, context, timeout):
        captured["request"] = request
        captured["context"] = context
        captured["timeout"] = timeout
        return FakeResponse(json.dumps(active_node()).encode("utf-8"))

    result = get_ironic_node(
        NODE_UUID,
        environ=configured_environment(),
        opener=fake_opener,
    )
    request = captured["request"]
    assert request.full_url.endswith("/v1/nodes/" + NODE_UUID)
    assert request.get_method() == "GET"
    assert request.get_header("X-auth-token") == "test-token"
    assert request.get_header("X-openstack-ironic-api-version") == "1.82"
    assert captured["timeout"] == 10
    assert captured["context"].check_hostname is True
    assert result["provision_state"] == "active"
    assert result["maintenance"] is False
    assert "token" not in result


def test_redirect_handler_never_forwards_authenticated_request():
    handler = _NoRedirectHandler()
    request = __import__("urllib.request").request.Request(
        "https://ironic.example.test/v1/nodes/" + NODE_UUID,
        headers={"X-Auth-Token": "secret"},
    )
    redirected = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://other.example.test/steal-token",
    )
    assert redirected is None


def test_get_ironic_node_rejects_non_tls_endpoint():
    environment = configured_environment()
    environment["IRONIC_API_URL"] = "http://ironic.example.test"
    with pytest.raises(IronicObservationError, match="IRONIC_API_URL_INVALID"):
        get_ironic_node(NODE_UUID, environ=environment)


def test_get_ironic_node_rejects_invalid_uuid():
    with pytest.raises(IronicObservationError, match="IRONIC_NODE_UUID_INVALID"):
        get_ironic_node("not-a-uuid", environ=configured_environment())


def test_get_ironic_node_rejects_identity_mismatch():
    response = active_node()
    response["uuid"] = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    with pytest.raises(
        IronicObservationError,
        match="IRONIC_NODE_IDENTITY_MISMATCH",
    ):
        get_ironic_node(
            NODE_UUID,
            environ=configured_environment(),
            opener=lambda *args, **kwargs: FakeResponse(
                json.dumps(response).encode("utf-8")
            ),
        )


def test_get_ironic_node_rejects_non_string_identity():
    response = active_node()
    response["uuid"] = None
    with pytest.raises(
        IronicObservationError,
        match="IRONIC_NODE_IDENTITY_INVALID",
    ):
        get_ironic_node(
            NODE_UUID,
            environ=configured_environment(),
            opener=lambda *args, **kwargs: FakeResponse(
                json.dumps(response).encode("utf-8")
            ),
        )


def test_get_ironic_node_maps_http_errors_to_safe_codes():
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://ironic.example.test",
            503,
            "unavailable",
            {},
            None,
        )

    with pytest.raises(IronicObservationError, match="IRONIC_API_HTTP_503"):
        get_ironic_node(
            NODE_UUID,
            environ=configured_environment(),
            opener=fail,
        )


def test_get_ironic_node_rejects_invalid_json():
    with pytest.raises(
        IronicObservationError,
        match="IRONIC_API_RESPONSE_INVALID_JSON",
    ):
        get_ironic_node(
            NODE_UUID,
            environ=configured_environment(),
            opener=lambda *args, **kwargs: FakeResponse(b"not-json"),
        )


def test_get_ironic_node_rejects_non_standard_json_constant():
    with pytest.raises(
        IronicObservationError,
        match="IRONIC_API_RESPONSE_INVALID_JSON",
    ):
        get_ironic_node(
            NODE_UUID,
            environ=configured_environment(),
            opener=lambda *args, **kwargs: FakeResponse(
                (
                    '{"uuid":"' + NODE_UUID + '","provision_state":"active",'
                    '"maintenance":false,"last_error":NaN}'
                ).encode("utf-8")
            ),
        )


def test_get_ironic_node_requires_json_content_type():
    with pytest.raises(
        IronicObservationError,
        match="IRONIC_API_CONTENT_TYPE_INVALID",
    ):
        get_ironic_node(
            NODE_UUID,
            environ=configured_environment(),
            opener=lambda *args, **kwargs: FakeResponse(
                json.dumps(active_node()).encode("utf-8"),
                content_type="text/html",
            ),
        )


@pytest.mark.parametrize("timeout", ["0", "61", "NaN", "Infinity", "-Infinity"])
def test_get_ironic_node_rejects_invalid_timeout(timeout):
    environment = configured_environment()
    environment["IRONIC_API_TIMEOUT_SECONDS"] = timeout
    with pytest.raises(
        IronicObservationError,
        match="IRONIC_API_TIMEOUT_INVALID",
    ):
        get_ironic_node(NODE_UUID, environ=environment)


def test_get_ironic_node_rejects_header_injection_token():
    environment = configured_environment()
    environment["IRONIC_API_TOKEN"] = "token\\r\\nX-Evil: yes"
    with pytest.raises(
        IronicObservationError,
        match="IRONIC_API_TOKEN_INVALID",
    ):
        get_ironic_node(NODE_UUID, environ=environment)
