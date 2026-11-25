# ruff: noqa: I001
"""Connector-level fault taxonomy tests.

The shared HTTP and MCP connectors attach a stable fault category to every
transport failure: 4xx, 5xx, timeout and invalid/non-JSON response are
distinguishable at the connector layer, before any protocol adapter maps the
category onto a capability error code. All transports are faked; no network
call is made.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from django.test import override_settings

from core.http_connector import execute_http_operation
from core.mcp_connector import execute_mcp_operation
from core.tool_router import (
    FAULT_AUTH_REJECTED,
    FAULT_CLIENT_ERROR,
    FAULT_INVALID_RESPONSE,
    FAULT_SERVER_ERROR,
    FAULT_TIMEOUT,
    FAULT_UNREACHABLE,
    ToolExecutionError,
)

MCP_ENDPOINT = "https://mcp.example.com/rpc"
HTTP_BASE_URL = "https://api.example.com"


# ---------------------------------------------------------------------------
# HTTP connector fakes
# ---------------------------------------------------------------------------


class _HttpResponse:
    def __init__(self, status_code=200, body=b'{"items": ["ok"]}', content_type="application/json"):
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        yield self._body


class _HttpFakeClient:
    def __init__(self, response=None, side_effect=None):
        self.response = response
        self.side_effect = side_effect

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method, url, **kwargs):
        if self.side_effect is not None:
            raise self.side_effect
        return self.response


def _http_context():
    return SimpleNamespace(
        plugin_manifest={
            "http": {
                "base_url": HTTP_BASE_URL,
                "operations": {
                    "search": {
                        "method": "GET",
                        "path": "/v1/items/{item_id}",
                        "path_params": {"item_id": "item_id"},
                        "query_params": {"q": "query"},
                    }
                },
            }
        },
        connection_auth_type="none",
        credential_ref="",
    )


def _http_tool():
    return SimpleNamespace(name="search", timeout_seconds=5)


def _run_http(monkeypatch, response=None, side_effect=None):
    monkeypatch.setattr(
        "core.http_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["api.example.com"]):
        with patch(
            "core.http_connector.httpx.Client",
            return_value=_HttpFakeClient(response=response, side_effect=side_effect),
        ):
            with pytest.raises(ToolExecutionError) as exc:
                execute_http_operation(
                    {"item_id": "item-1", "query": "status"},
                    _http_context(),
                    _http_tool(),
                )
    return exc.value


def test_http_4xx_maps_to_client_error(monkeypatch):
    error = _run_http(monkeypatch, response=_HttpResponse(400, b'{"error":"bad"}'))
    assert error.code == FAULT_CLIENT_ERROR


@pytest.mark.parametrize("status_code", [401, 403])
def test_http_credential_rejection_is_not_a_generic_client_error(monkeypatch, status_code):
    error = _run_http(
        monkeypatch,
        response=_HttpResponse(status_code, b'{"error":"credential rejected"}'),
    )
    assert error.code == FAULT_AUTH_REJECTED


def test_http_5xx_maps_to_server_error(monkeypatch):
    error = _run_http(monkeypatch, response=_HttpResponse(500, b'{"error":"boom"}'))
    assert error.code == FAULT_SERVER_ERROR


def test_http_timeout_maps_to_timeout(monkeypatch):
    error = _run_http(monkeypatch, side_effect=httpx.TimeoutException("timed out"))
    assert error.code == FAULT_TIMEOUT


def test_http_non_json_content_type_maps_to_invalid_response(monkeypatch):
    error = _run_http(
        monkeypatch,
        response=_HttpResponse(200, b'{"items": ["ok"]}', content_type="text/html"),
    )
    assert error.code == FAULT_INVALID_RESPONSE


def test_http_malformed_json_maps_to_invalid_response(monkeypatch):
    error = _run_http(monkeypatch, response=_HttpResponse(200, b"not-json"))
    assert error.code == FAULT_INVALID_RESPONSE


def test_http_connect_error_maps_to_unreachable(monkeypatch):
    error = _run_http(monkeypatch, side_effect=httpx.ConnectError("connection refused"))
    assert error.code == FAULT_UNREACHABLE


def test_http_too_many_redirects_maps_to_unreachable(monkeypatch):
    error = _run_http(monkeypatch, side_effect=httpx.TooManyRedirects("redirect loop"))
    assert error.code == FAULT_UNREACHABLE


# ---------------------------------------------------------------------------
# MCP connector fakes
# ---------------------------------------------------------------------------


class _McpResponse:
    def __init__(
        self,
        status_code=200,
        body=b"{}",
        content_type="application/json",
        headers=None,
    ):
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        if headers:
            self.headers.update(headers)
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        midpoint = len(self.body) // 2
        yield self.body[:midpoint]
        yield self.body[midpoint:]


class _McpFakeClient:
    def __init__(self, responses, side_effect=None):
        self.responses = list(responses)
        self.side_effect = side_effect

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        return None

    def stream(self, method, url, **kwargs):
        if self.side_effect is not None:
            raise self.side_effect
        if not self.responses:
            raise AssertionError("Unexpected MCP request")
        return self.responses.pop(0)


def _mcp_context():
    return SimpleNamespace(
        plugin_manifest={
            "mcp": {"endpoint_url": MCP_ENDPOINT, "tool_map": {"search": "remote_search"}},
            "timeout_seconds": 30,
        },
        connection_auth_type="none",
        credential_ref="",
    )


def _mcp_tool():
    return SimpleNamespace(
        name="search",
        timeout_seconds=30,
        plugin=SimpleNamespace(connector_type="mcp", plugin_key="fake-mcp", version="1.0.0"),
    )


def _rpc_result(request_id, result):
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}).encode("utf-8")


def _initialize_response():
    return _McpResponse(
        body=_rpc_result(
            1,
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": False}},
            },
        ),
        headers={"Mcp-Session-Id": "session-1"},
    )


def _ack_response():
    return _McpResponse(status_code=202, body=b"")


def _run_mcp(monkeypatch, responses, side_effect=None):
    monkeypatch.setattr(
        "core.mcp_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["mcp.example.com"]):
        with patch(
            "core.mcp_connector.httpx.Client",
            return_value=_McpFakeClient(responses, side_effect=side_effect),
        ):
            with pytest.raises(ToolExecutionError) as exc:
                execute_mcp_operation({"query": "edge"}, _mcp_context(), _mcp_tool())
    return exc.value


def test_mcp_initialize_4xx_maps_to_client_error(monkeypatch):
    error = _run_mcp(monkeypatch, [_McpResponse(400, b"{}")])
    assert error.code == FAULT_CLIENT_ERROR


def test_mcp_initialize_5xx_maps_to_server_error(monkeypatch):
    error = _run_mcp(monkeypatch, [_McpResponse(500, b"{}")])
    assert error.code == FAULT_SERVER_ERROR


def test_mcp_tools_call_4xx_maps_to_client_error(monkeypatch):
    responses = [
        _initialize_response(),
        _ack_response(),
        _McpResponse(400, b"{}"),
    ]
    error = _run_mcp(monkeypatch, responses)
    assert error.code == FAULT_CLIENT_ERROR


def test_mcp_tools_call_5xx_maps_to_server_error(monkeypatch):
    responses = [
        _initialize_response(),
        _ack_response(),
        _McpResponse(503, b"{}"),
    ]
    error = _run_mcp(monkeypatch, responses)
    assert error.code == FAULT_SERVER_ERROR


def test_mcp_timeout_maps_to_timeout(monkeypatch):
    error = _run_mcp(monkeypatch, [], side_effect=httpx.TimeoutException("timed out"))
    assert error.code == FAULT_TIMEOUT


def test_mcp_malformed_json_maps_to_invalid_response(monkeypatch):
    responses = [
        _initialize_response(),
        _ack_response(),
        _McpResponse(200, b"not-json"),
    ]
    error = _run_mcp(monkeypatch, responses)
    assert error.code == FAULT_INVALID_RESPONSE


def test_mcp_connect_error_maps_to_unreachable(monkeypatch):
    error = _run_mcp(monkeypatch, [], side_effect=httpx.ConnectError("connection refused"))
    assert error.code == FAULT_UNREACHABLE


def test_mcp_too_many_redirects_maps_to_unreachable(monkeypatch):
    error = _run_mcp(monkeypatch, [], side_effect=httpx.TooManyRedirects("redirect loop"))
    assert error.code == FAULT_UNREACHABLE
