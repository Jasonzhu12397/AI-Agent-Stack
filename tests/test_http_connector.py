# ruff: noqa: I001
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import override_settings

from core.http_connector import (
    _resolve_auth_headers,
    execute_http_operation,
    register_http_tool,
)
from core.tool_router import ToolExecutionError, ToolRouter


class FakeResponse:
    def __init__(self, status_code=200, chunks=None):
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
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
        def fake_client(**options):
            captured["client_options"] = options
            return FakeClient(FakeResponse(), captured)

        with patch(
            "core.http_connector.httpx.Client",
            side_effect=fake_client,
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
    assert captured["client_options"]["follow_redirects"] is False
    assert captured["client_options"]["trust_env"] is False


def test_http_connector_allows_omitted_optional_query_arguments(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "core.http_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443))
        ],
    )
    manifest = {
        "http": {
            "base_url": "https://api.example.com",
            "operations": {
                "search": {
                    "method": "GET",
                    "path": "/v1/places",
                    "path_params": {},
                    "query_params": {
                        "keywords": "keywords",
                        "city": "city",
                        "citylimit": "city_limit",
                    },
                }
            },
        }
    }
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["api.example.com"]):
        with patch(
            "core.http_connector.httpx.Client",
            return_value=FakeClient(FakeResponse(), captured),
        ):
            result = execute_http_operation(
                {"keywords": "coffee"},
                context(manifest=manifest),
                tool(),
            )

    assert result == {"items": ["ok"]}
    assert captured["params"] == [("keywords", "coffee")]


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


def test_http_connector_resolves_bearer_credentials_from_server_side_reference(monkeypatch):
    monkeypatch.setattr(
        "core.http_connector.import_string",
        lambda path: lambda reference: {
            "type": "bearer",
            "token": "server-side-secret",
        },
    )
    with override_settings(PLUGIN_CREDENTIAL_RESOLVER="platform.secrets.resolve"):
        headers = _resolve_auth_headers(
            context(
                auth_type="credential_ref",
                credential_ref="vault://platform/github",
            )
        )
    assert headers == {"Authorization": "Bearer server-side-secret"}


def test_http_connector_rejects_auth_required_without_credential(monkeypatch):
    monkeypatch.setattr(
        "core.http_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443))
        ],
    )
    manifest = {
        "http": {
            "base_url": "https://api.example.com",
            "auth_required": True,
            "operations": {
                "search": {
                    "method": "GET",
                    "path": "/v1/items/{item_id}",
                    "path_params": {"item_id": "item_id"},
                    "query_params": {"q": "query"},
                }
            },
        }
    }
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["api.example.com"]):
        with pytest.raises(ToolExecutionError, match="authenticated connection"):
            execute_http_operation(
                {"item_id": "item-1", "query": "status"},
                context(manifest=manifest),
                tool(),
            )


def test_http_connector_blocks_mutating_post_operations(monkeypatch):
    monkeypatch.setattr(
        "core.http_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443))
        ],
    )
    manifest = {
        "http": {
            "base_url": "https://api.example.com",
            "operations": {
                "search": {
                    "method": "POST",
                    "path": "/v1/items/search",
                    "path_params": {},
                    "query_params": {},
                    "body_param": "payload",
                    "read_only": False,
                }
            },
        }
    }
    selected_tool = SimpleNamespace(
        name="search",
        timeout_seconds=5,
        idempotency_mode="none",
    )
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["api.example.com"]):
        with pytest.raises(ToolExecutionError, match="read-only"):
            execute_http_operation(
                {"payload": {"query": "status"}},
                context(manifest=manifest),
                selected_tool,
            )


def test_version_pinned_http_handler_executes_through_tool_router(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "core.http_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443))
        ],
    )

    def fake_client(**options):
        captured["client_options"] = options
        return FakeClient(FakeResponse(), captured)

    plugin = SimpleNamespace(
        id="plugin-http",
        plugin_key="sample-http",
        version="1.0.0",
        connector_type="http",
        enabled=True,
        review_status="approved",
        manifest={
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
    )
    installation = SimpleNamespace(
        id="installation-http",
        tenant_id="tenant-http",
        project_id="project-http",
        plugin_id=plugin.id,
        plugin=plugin,
        plugin_version=plugin.version,
        status="installed",
        granted_permissions=["sample:read"],
    )
    tool_model = SimpleNamespace(
        plugin_id=plugin.id,
        plugin=plugin,
        name="search",
        enabled=True,
        risk_level="low",
        required_permissions=["sample:read"],
        idempotency_mode="read_only",
        timeout_seconds=5,
        input_schema={
            "type": "object",
            "properties": {
                "item_id": {"type": "string"},
                "query": {"type": "string"},
            },
            "required": ["item_id", "query"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["items"],
            "additionalProperties": False,
        },
    )
    router = ToolRouter()
    assert register_http_tool(router, tool_model) is True

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["api.example.com"]):
        with patch(
            "core.http_connector.httpx.Client",
            side_effect=fake_client,
        ):
            result = router.execute(
                installation=installation,
                tool=tool_model,
                arguments={"item_id": "42", "query": "ready"},
                granted_permissions=["sample:read"],
            )

    assert result == {
        "ok": True,
        "tool": "search",
        "result": {"items": ["ok"]},
    }
    assert captured["method"] == "GET"


def test_http_connector_rejects_malformed_base_url_before_network(monkeypatch):
    from core.http_connector import _validate_base_url

    monkeypatch.setattr(
        "core.http_connector.socket.getaddrinfo",
        lambda *args, **kwargs: pytest.fail("DNS must not run for malformed URLs"),
    )
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["api.example.com"]):
        with pytest.raises(ToolExecutionError, match="malformed"):
            _validate_base_url("https://api.example.com:not-a-port")


@pytest.mark.parametrize(
    "content_type,chunks,error",
    [
        ("text/html", [b'{"items": ["ok"]}'], "content type"),
        ("application/json", [b'{"value": NaN}'], "UTF-8 JSON"),
    ],
)
def test_http_connector_rejects_non_json_response_formats(
    monkeypatch,
    content_type,
    chunks,
    error,
):
    captured = {}
    monkeypatch.setattr(
        "core.http_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443))
        ],
    )
    response = FakeResponse(chunks=chunks)
    response.headers["content-type"] = content_type
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["api.example.com"]):
        with patch(
            "core.http_connector.httpx.Client",
            return_value=FakeClient(response, captured),
        ):
            with pytest.raises(ToolExecutionError, match=error):
                execute_http_operation(
                    {"item_id": "item-1", "query": "status"},
                    context(),
                    tool(),
                )



def test_http_connector_adds_safe_static_headers(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "core.http_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443))
        ],
    )
    manifest = {
        "http": {
            "base_url": "https://api.example.com",
            "operations": {
                "search": {
                    "method": "POST",
                    "path": "/v1/places:searchText",
                    "path_params": {},
                    "query_params": {},
                    "body_param": "request",
                    "read_only": True,
                    "headers": {
                        "X-Goog-FieldMask": "places.id,places.displayName"
                    },
                }
            },
        }
    }
    selected_tool = SimpleNamespace(
        name="search",
        timeout_seconds=5,
        idempotency_mode="read_only",
    )
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["api.example.com"]):
        with patch(
            "core.http_connector.httpx.Client",
            return_value=FakeClient(FakeResponse(), captured),
        ):
            result = execute_http_operation(
                {"request": {"textQuery": "coffee"}},
                context(manifest=manifest),
                selected_tool,
            )

    assert result == {"items": ["ok"]}
    assert captured["headers"]["X-Goog-FieldMask"] == "places.id,places.displayName"
    assert captured["json"] == {"textQuery": "coffee"}


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer attacker-controlled"},
        {"X-Goog-Api-Key": "plaintext-secret"},
        {"X-Test": "bad\nheader"},
    ],
)
def test_http_connector_rejects_unsafe_static_headers(headers):
    manifest = {
        "http": {
            "base_url": "https://api.example.com",
            "operations": {
                "search": {
                    "method": "GET",
                    "path": "/v1/items",
                    "path_params": {},
                    "query_params": {},
                    "headers": headers,
                }
            },
        }
    }
    with patch(
        "core.http_connector._validate_base_url",
        return_value="https://api.example.com",
    ):
        with pytest.raises(ToolExecutionError, match="unsafe static header"):
            from core.http_connector import _operation_config

            _operation_config(
                context(manifest=manifest),
                SimpleNamespace(name="search"),
            )


def test_http_connector_injects_server_side_query_credentials_without_secret_in_url(
    monkeypatch,
):
    captured = {}
    monkeypatch.setattr(
        "core.http_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [
            (2, 1, 6, "", ("93.184.216.34", 443))
        ],
    )
    monkeypatch.setattr(
        "core.http_connector.import_string",
        lambda path: lambda reference: {
            "type": "query",
            "name": "key",
            "value": "server-side-api-secret",
        },
    )
    manifest = {
        "http": {
            "base_url": "https://api.example.com",
            "auth_required": True,
            "operations": {
                "search": {
                    "method": "GET",
                    "path": "/v1/places",
                    "path_params": {},
                    "query_params": {"q": "query"},
                    "auth_query_parameter": "key",
                }
            },
        }
    }
    selected_tool = SimpleNamespace(
        name="search",
        timeout_seconds=5,
        idempotency_mode="read_only",
    )
    with override_settings(
        PLUGIN_HTTP_ALLOWED_HOSTS=["api.example.com"],
        PLUGIN_CREDENTIAL_RESOLVER="platform.secrets.resolve",
    ):
        with patch(
            "core.http_connector.httpx.Client",
            return_value=FakeClient(FakeResponse(), captured),
        ):
            result = execute_http_operation(
                {"query": "coffee"},
                context(
                    manifest=manifest,
                    auth_type="credential_ref",
                    credential_ref="vault://maps/api-key",
                ),
                selected_tool,
            )

    assert result == {"items": ["ok"]}
    assert captured["url"] == "https://api.example.com/v1/places"
    assert captured["params"] == [
        ("q", "coffee"),
        ("key", "server-side-api-secret"),
    ]
    assert "server-side-api-secret" not in captured["url"]


def test_http_connector_rejects_query_credential_unless_operation_explicitly_allows_it(monkeypatch):
    monkeypatch.setattr(
        "core.http_connector.import_string",
        lambda path: lambda reference: {
            "type": "query",
            "name": "key",
            "value": "server-side-api-secret",
        },
    )
    manifest = {
        "http": {
            "base_url": "https://api.example.com",
            "auth_required": True,
            "operations": {
                "search": {
                    "method": "GET",
                    "path": "/v1/places",
                    "path_params": {},
                    "query_params": {"q": "query"},
                    "auth_query_parameter": "access_token",
                }
            },
        }
    }
    selected_tool = SimpleNamespace(
        name="search",
        timeout_seconds=5,
        idempotency_mode="read_only",
    )
    with override_settings(
        PLUGIN_HTTP_ALLOWED_HOSTS=["api.example.com"],
        PLUGIN_CREDENTIAL_RESOLVER="platform.secrets.resolve",
    ):
        with pytest.raises(ToolExecutionError, match="does not match"):
            execute_http_operation(
                {"query": "coffee"},
                context(
                    manifest=manifest,
                    auth_type="credential_ref",
                    credential_ref="vault://maps/api-key",
                ),
                selected_tool,
            )


def test_http_connector_rejects_query_credential_parameter_collision(monkeypatch):
    manifest = {
        "http": {
            "base_url": "https://api.example.com",
            "auth_required": True,
            "operations": {
                "search": {
                    "method": "GET",
                    "path": "/v1/places",
                    "path_params": {},
                    "query_params": {"key": "query"},
                    "auth_query_parameter": "key",
                }
            },
        }
    }
    with patch(
        "core.http_connector._validate_base_url",
        return_value="https://api.example.com",
    ):
        with pytest.raises(ToolExecutionError, match="conflicts"):
            from core.http_connector import _operation_config

            _operation_config(
                context(manifest=manifest, auth_type="credential_ref", credential_ref="vault://key"),
                SimpleNamespace(name="search"),
            )
