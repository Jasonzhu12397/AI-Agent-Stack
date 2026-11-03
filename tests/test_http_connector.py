from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import override_settings

from core.http_connector import execute_http_operation, register_http_tool
from core.tool_router import ToolExecutionError, ToolRouter


class FakeResponse:
    def __init__(self, status_code=200, chunks=None):
        self.status_code = status_code
        self.chunks = chunks or [b'{"items": ["ok"]}']

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def iter_bytes(self):
        yield from self.chunks


class FakeClient:
    def __init__(self, response, captured):
        self.response = response
        self.captured = captured

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def stream(self, method, url, **kwargs):
        self.captured.update({"method": method, "url": url, **kwargs})
        return self.response


def context(manifest=None, auth_type="none", credential_ref=""):
    return SimpleNamespace(
        plugin_manifest=manifest or {
            "http": {
                "base_url": "https://api.example.com",
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
        connection_auth_type=auth_type,
        credential_ref=credential_ref,
    )


def tool():
    return SimpleNamespace(name="search", timeout_seconds=5)


@pytest.mark.parametrize("status_code", [302, 307])
def test_http_connector_rejects_redirects(monkeypatch, status_code):
    captured = {}
    monkeypatch.setattr(
        "core.http_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443))
        ],
    )
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["api.example.com"]):
        with patch(
            "core.http_connector.httpx.Client",
            return_value=FakeClient(FakeResponse(status_code=status_code), captured),
        ):
            with pytest.raises(ToolExecutionError, match="redirects"):
                execute_http_operation(
                    {"item_id": "item-1", "query": "status"},
                    context(),
                    tool(),
                )


def test_http_connector_builds_bounded_request_and_parses_json(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "core.http_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443))
        ],
    )
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["api.example.com"]):
        with patch(
            "core.http_connector.httpx.Client",
            return_value=FakeClient(FakeResponse(), captured),
        ):
            result = execute_http_operation(
                {"item_id": "a/b?x", "query": "service ready"},
                context(),
                tool(),
            )

    assert result == {"items": ["ok"]}
    assert captured["method"] == "GET"
    assert captured["url"] == "https://api.example.com/v1/items/a%2Fb%3Fx"
    assert captured["params"] == [("q", "service ready")]
    assert captured["follow_redirects"] is False if "follow_redirects" in captured else True


def test_http_connector_rejects_non_public_dns_results(monkeypatch):
    monkeypatch.setattr(
        "core.http_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("127.0.0.1", 443))
        ],
    )
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["api.example.com"]):
        with pytest.raises(ToolExecutionError, match="non-public"):
            execute_http_operation(
                {"item_id": "item-1", "query": "status"},
                context(),
                tool(),
            )


def test_http_connector_requires_exact_host_allowlist(monkeypatch):
    monkeypatch.setattr(
        "core.http_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443))
        ],
    )
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["other.example.com"]):
        with pytest.raises(ToolExecutionError, match="allowlist"):
            execute_http_operation(
                {"item_id": "item-1", "query": "status"},
                context(),
                tool(),
            )


def test_http_connector_rejects_oversized_response(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "core.http_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443))
        ],
    )
    large_chunks = [b"x" * 600000, b"x" * 500000]
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["api.example.com"]):
        with patch(
            "core.http_connector.httpx.Client",
            return_value=FakeClient(FakeResponse(chunks=large_chunks), captured),
        ):
            with pytest.raises(ToolExecutionError, match="1 MiB"):
                execute_http_operation(
                    {"item_id": "item-1", "query": "status"},
                    context(),
                    tool(),
                )


def test_http_connector_requires_configured_credential_resolver():
    with override_settings(PLUGIN_CREDENTIAL_RESOLVER=""):
        with pytest.raises(ToolExecutionError, match="credential resolver"):
            from core.http_connector import _resolve_auth_headers

            _resolve_auth_headers(
                context(auth_type="credential_ref", credential_ref="vault://api/token")
            )


def test_http_tool_registration_is_version_scoped():
    plugin = SimpleNamespace(
        plugin_key="sample",
        version="1.0.0",
        connector_type="http",
    )
    tool_model = SimpleNamespace(plugin=plugin, name="search")
    router = ToolRouter()

    assert register_http_tool(router, tool_model) is True
    assert router.has_handler("sample", "search", plugin_version="1.0.0") is True
    assert router.has_handler("sample", "search", plugin_version="2.0.0") is False
    assert register_http_tool(router, tool_model) is False
