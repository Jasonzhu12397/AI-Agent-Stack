# ruff: noqa: I001
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from django.test import override_settings

from core import audit, plugin_credentials
from core.http_connector import execute_http_operation
from core.tool_router import ToolExecutionError


def _manifest(auth_query_parameter=None):
    operation = {
        "method": "GET",
        "path": "/v1/items/{item_id}",
        "path_params": {"item_id": "item_id"},
        "query_params": {"q": "query"},
    }
    if auth_query_parameter is not None:
        operation["auth_query_parameter"] = auth_query_parameter
    return {
        "http": {
            "base_url": "https://api.example.com",
            "auth_required": True,
            "operations": {"search": operation},
        }
    }


def _context(credential_ref, manifest=None):
    return SimpleNamespace(
        plugin_manifest=manifest or _manifest(),
        connection_auth_type="credential_ref",
        credential_ref=credential_ref,
    )


def _tool():
    return SimpleNamespace(
        name="search",
        timeout_seconds=5,
        idempotency_mode="read_only",
        risk_level="low",
    )


RealHttpxClient = httpx.Client


def _mock_client_factory(handler, captured):
    def factory(**options):
        options.pop("transport", None)
        captured["client_options"] = options
        return RealHttpxClient(
            transport=httpx.MockTransport(handler), **options
        )

    return factory


def _ok_handler(request):
    return httpx.Response(200, json={"items": ["ok"]})


def test_bearer_secret_is_injected_as_header_and_never_returned(
    monkeypatch, caplog
):
    monkeypatch.setenv("PLUGIN_SECRET_GH_TOKEN", "bearer-secret-42")
    monkeypatch.setattr(
        "core.http_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    captured = {}
    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["api.example.com"]):
        with patch(
            "core.http_connector.httpx.Client",
            side_effect=_mock_client_factory(_ok_handler, captured),
        ):
            result = execute_http_operation(
                {"item_id": "item-1", "query": "status"},
                _context("env://PLUGIN_SECRET_GH_TOKEN"),
                _tool(),
            )

    assert result == {"items": ["ok"]}
    assert captured["client_options"] is not None
    assert "bearer-secret-42" not in json.dumps(result)
    assert "bearer-secret-42" not in caplog.text


def test_header_credential_is_injected_from_env_json(monkeypatch):
    monkeypatch.setenv(
        "PLUGIN_SECRET_API_KEY",
        json.dumps(
            {"type": "header", "name": "X-Api-Key", "value": "header-secret-17"}
        ),
    )
    monkeypatch.setattr(
        "core.http_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    captured = {}
    seen_headers = {}

    def handler(request):
        seen_headers.update(dict(request.headers))
        return _ok_handler(request)

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["api.example.com"]):
        with patch(
            "core.http_connector.httpx.Client",
            side_effect=_mock_client_factory(handler, captured),
        ):
            execute_http_operation(
                {"item_id": "item-1", "query": "status"},
                _context("env://PLUGIN_SECRET_API_KEY"),
                _tool(),
            )

    assert seen_headers["x-api-key"] == "header-secret-17"


def test_query_credential_is_injected_only_when_operation_declares_it(
    monkeypatch,
):
    monkeypatch.setenv(
        "PLUGIN_SECRET_QUERY_KEY",
        json.dumps({"type": "query", "name": "key", "value": "query-secret-31"}),
    )
    monkeypatch.setattr(
        "core.http_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    captured = {}
    seen_params = []

    def handler(request):
        seen_params.extend(request.url.params.items())
        return _ok_handler(request)

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["api.example.com"]):
        with patch(
            "core.http_connector.httpx.Client",
            side_effect=_mock_client_factory(handler, captured),
        ):
            execute_http_operation(
                {"item_id": "item-1", "query": "status"},
                _context(
                    "env://PLUGIN_SECRET_QUERY_KEY",
                    manifest=_manifest(auth_query_parameter="key"),
                ),
                _tool(),
            )

    assert ("key", "query-secret-31") in seen_params
    assert "query-secret-31" not in str(captured.get("client_options", {}))


def test_missing_env_variable_fails_closed_without_transport_call(
    monkeypatch, caplog
):
    monkeypatch.delenv("PLUGIN_SECRET_MISSING", raising=False)
    monkeypatch.setattr(
        "core.http_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    calls = {"transport": 0}

    def handler(request):
        calls["transport"] += 1
        return _ok_handler(request)

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["api.example.com"]):
        with patch(
            "core.http_connector.httpx.Client",
            side_effect=_mock_client_factory(handler, {}),
        ):
            with pytest.raises(
                plugin_credentials.CredentialNotFoundError
            ) as exc:
                execute_http_operation(
                    {"item_id": "item-1", "query": "status"},
                    _context("env://PLUGIN_SECRET_MISSING"),
                    _tool(),
                )

    assert exc.value.code == "CREDENTIAL_NOT_FOUND"
    assert calls["transport"] == 0
    assert "PLUGIN_SECRET_MISSING" not in str(exc.value)
    assert caplog.text == ""


def test_unknown_scheme_fails_closed_without_transport_call(monkeypatch):
    monkeypatch.setattr(
        "core.http_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    calls = {"transport": 0}

    def handler(request):
        calls["transport"] += 1
        return _ok_handler(request)

    with override_settings(PLUGIN_HTTP_ALLOWED_HOSTS=["api.example.com"]):
        with patch(
            "core.http_connector.httpx.Client",
            side_effect=_mock_client_factory(handler, {}),
        ):
            with pytest.raises(
                plugin_credentials.CredentialProviderUnsupportedError
            ) as exc:
                execute_http_operation(
                    {"item_id": "item-1", "query": "status"},
                    _context("vault://platform/github"),
                    _tool(),
                )

    assert exc.value.code == "CREDENTIAL_PROVIDER_UNSUPPORTED"
    assert calls["transport"] == 0


def test_empty_resolver_setting_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "core.http_connector.socket.getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    with override_settings(
        PLUGIN_HTTP_ALLOWED_HOSTS=["api.example.com"],
        PLUGIN_CREDENTIAL_RESOLVER="",
    ):
        with pytest.raises(
            plugin_credentials.CredentialResolverNotConfiguredError
        ) as exc:
            execute_http_operation(
                {"item_id": "item-1", "query": "status"},
                _context("env://PLUGIN_SECRET_GH_TOKEN"),
                _tool(),
            )

    assert exc.value.code == "CREDENTIAL_RESOLVER_NOT_CONFIGURED"


def test_secret_never_enters_audit_metadata(monkeypatch):
    monkeypatch.setenv("PLUGIN_SECRET_AUDIT", "audit-secret-99")
    metadata = {
        "connection_id": "conn-1",
        "credential_ref": "env://PLUGIN_SECRET_AUDIT",
        "token": "audit-secret-99",
        "notes": "plain context",
    }
    sanitized = audit._safe_metadata(metadata)
    serialized = json.dumps(sanitized)
    assert "audit-secret-99" not in serialized
    assert "credential_ref" not in json.dumps(sanitized)


def test_resolver_exception_does_not_echo_reference_or_secret(monkeypatch):
    monkeypatch.delenv("PLUGIN_SECRET_ECHO", raising=False)
    with pytest.raises(plugin_credentials.CredentialNotFoundError) as exc:
        plugin_credentials.resolve("env://PLUGIN_SECRET_ECHO")
    message = str(exc.value)
    assert "PLUGIN_SECRET_ECHO" not in message
    assert plugin_credentials.redact("env://PLUGIN_SECRET_ECHO") == "env://***"


def test_resolver_error_still_surfaces_as_tool_execution_error(monkeypatch):
    monkeypatch.delenv("PLUGIN_SECRET_TYPE", raising=False)
    with pytest.raises(ToolExecutionError):
        plugin_credentials.resolve("env://PLUGIN_SECRET_TYPE")
