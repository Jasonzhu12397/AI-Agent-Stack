# ruff: noqa: I001
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import override_settings

from core.http_connector import register_connector_tool
from core.mcp_connector import McpClient, execute_mcp_operation, register_mcp_tool
from core.tool_router import ToolExecutionError, ToolRouter

ENDPOINT = "https://mcp.example.com/rpc"


class FakeResponse:
    def __init__(self, status_code=200, body=b"{}", content_type="application/json", headers=None):
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        if headers:
            self.headers.update(headers)
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def iter_bytes(self):
        # Split the body so the size accounting path is exercised.
        midpoint = len(self.body) // 2
        yield self.body[:midpoint]
        yield self.body[midpoint:]


class FakeClient:
    def __init__(self, responses, calls):
        self.responses = list(responses)
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def close(self):
        return None

    def stream(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError("Unexpected MCP request")
        return self.responses.pop(0)


def rpc_result(request_id, result):
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}).encode("utf-8")


def rpc_error(request_id, code, message):
    return json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    ).encode("utf-8")


def sse(payload):
    return b"event: message\ndata: " + payload + b"\n\n"


def handshake_responses(*extra, protocol_version="2025-06-18"):
    return [
        FakeResponse(
            body=rpc_result(
                1,
                {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "fake-mcp", "version": "1.0.0"},
                },
            ),
            headers={"Mcp-Session-Id": "session-1"},
        ),
        FakeResponse(
            status_code=202,
            body=b"",
            content_type="application/json",
        ),
        *extra,
    ]


def context(manifest=None, auth_type="none", credential_ref=""):
    return SimpleNamespace(
        plugin_manifest=manifest
        or {
            "mcp": {
                "endpoint_url": ENDPOINT,
                "tool_map": {"search": "remote_search"},
            },
            "timeout_seconds": 30,
        },
        connection_auth_type=auth_type,
        credential_ref=credential_ref,
    )


def tool(name="search", connector_type="mcp", plugin_key="fake-mcp", version="1.0.0"):
    return SimpleNamespace(
        name=name,
        timeout_seconds=30,
        plugin=SimpleNamespace(
            connector_type=connector_type,
            plugin_key=plugin_key,
            version=version,
        ),
    )


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(
        "core.mcp_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )


def run(monkeypatch, responses, argument=None, calls=None):
    calls = calls if calls is not None else []
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["mcp.example.com"]):
        with patch("core.mcp_connector.httpx.Client", return_value=responses):
            result = execute_mcp_operation(
                argument or {"query": "edge"},
                context(),
                tool(),
            )
    return result, calls


def decode_requests(calls):
    return [json.loads(call["content"].decode("utf-8")) for call in calls]


@pytest.mark.usefixtures("public_dns")
def test_initialize_then_call_tool_over_sse(monkeypatch):
    calls = []
    responses = FakeClient(
        handshake_responses(
            FakeResponse(
                body=sse(
                    rpc_result(
                        3,
                        {
                            "content": [{"type": "text", "text": "3 hosts ready"}],
                            "isError": False,
                            "structuredContent": {"ready": 3},
                        },
                    )
                ),
                content_type="text/event-stream",
            )
        ),
        calls,
    )
    result, captured = run(monkeypatch, responses, argument={"query": "edge"}, calls=calls)

    assert result == {
        "content": [{"type": "text", "text": "3 hosts ready"}],
        "isError": False,
        "structuredContent": {"ready": 3},
    }
    requests = decode_requests(captured)
    assert [request.get("method") for request in requests] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]
    assert requests[0]["params"]["clientInfo"]["name"] == "ai-agent-stack"
    assert requests[2]["params"] == {"name": "remote_search", "arguments": {"query": "edge"}}
    assert captured[0]["headers"]["Accept"] == "application/json, text/event-stream"
    assert captured[0]["headers"].get("Mcp-Session-Id") is None
    assert captured[2]["headers"]["Mcp-Session-Id"] == "session-1"
    assert captured[2]["headers"]["MCP-Protocol-Version"] == "2025-06-18"
    assert captured[0]["url"] == ENDPOINT


@pytest.mark.usefixtures("public_dns")
def test_tools_list_paginates_until_next_cursor_is_absent(monkeypatch):
    calls = []
    pages = [
        FakeResponse(
            body=sse(rpc_result(3, {"tools": [{"name": "remote_search"}], "nextCursor": "t2"})),
            content_type="text/event-stream",
        ),
        FakeResponse(
            body=sse(rpc_result(4, {"tools": [{"name": "remote_cleanup"}]})),
            content_type="text/event-stream",
        ),
    ]
    responses = FakeClient(handshake_responses(*pages), calls)
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["mcp.example.com"]):
        with patch("core.mcp_connector.httpx.Client", return_value=responses):
            with McpClient(ENDPOINT, {}, 30) as client:
                client.initialize()
                tools = client.list_tools()

    assert [entry["name"] for entry in tools] == ["remote_search", "remote_cleanup"]
    requests = decode_requests(calls)
    assert requests[2]["params"] == {}
    assert requests[3]["params"] == {"cursor": "t2"}


@pytest.mark.usefixtures("public_dns")
def test_server_error_is_reported_without_leaking_server_details(monkeypatch):
    calls = []
    responses = FakeClient(
        handshake_responses(
            FakeResponse(
                body=sse(rpc_error(3, -32602, "invalid params for tenant secret-tenant")),
                content_type="text/event-stream",
            )
        ),
        calls,
    )
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["mcp.example.com"]):
        with patch("core.mcp_connector.httpx.Client", return_value=responses):
            with pytest.raises(ToolExecutionError, match=r"MCP server error \(-32602\)"):
                execute_mcp_operation({"query": "edge"}, context(), tool())


@pytest.mark.usefixtures("public_dns")
def test_redirects_and_oversized_payloads_are_rejected(monkeypatch):
    for response, reason in (
        (
            FakeResponse(status_code=307, body=b"{}"),
            "redirects",
        ),
        (
            FakeResponse(body=b"x" * 1200000),
            "exceeds 1 MiB",
        ),
    ):
        calls = []
        responses = FakeClient([response], calls)
        with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["mcp.example.com"]):
            with patch("core.mcp_connector.httpx.Client", return_value=responses):
                with pytest.raises(ToolExecutionError, match=reason):
                    execute_mcp_operation({"query": "edge"}, context(), tool())


def test_endpoint_must_be_https_and_allowlisted():
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["mcp.example.com"]):
        for endpoint in (
            "http://mcp.example.com/rpc",
            "https://mcp.example.com:8443/rpc",
            "https://user:pass@mcp.example.com/rpc",
            "https://other.example.com/rpc",
        ):
            manifest = {"mcp": {"endpoint_url": endpoint}, "timeout_seconds": 30}
            with pytest.raises(ToolExecutionError):
                execute_mcp_operation({"query": "edge"}, context(manifest), tool())


@pytest.mark.usefixtures("public_dns")
def test_unsupported_manifest_and_arguments_are_rejected(monkeypatch):
    with pytest.raises(ToolExecutionError, match="manifest"):
        execute_mcp_operation({"query": "edge"}, context({"http": {}}), tool())
    with pytest.raises(ToolExecutionError, match="JSON object"):
        execute_mcp_operation([], context(), tool())
    invalid = context({"mcp": {"endpoint_url": ENDPOINT, "tool_map": {"search": 5}}})
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["mcp.example.com"]):
        with pytest.raises(ToolExecutionError, match="tool mapping"):
            execute_mcp_operation({"query": "edge"}, invalid, tool())


def test_register_mcp_tool_is_scoped_to_mcp_plugins():
    router = ToolRouter()
    assert register_mcp_tool(router, tool(connector_type="http")) is False
    assert router.has_handler("fake-mcp", "search", plugin_version="1.0.0") is False
    assert register_mcp_tool(router, tool()) is True
    assert router.has_handler("fake-mcp", "search", plugin_version="1.0.0") is True
    assert register_mcp_tool(router, tool()) is False


def test_register_connector_tool_dispatches_mcp_plugins():
    router = ToolRouter()
    assert register_connector_tool(router, tool(connector_type="mcp")) is True
    assert router.has_handler("fake-mcp", "search", plugin_version="1.0.0") is True
    assert register_connector_tool(router, tool(connector_type="custom")) is False
