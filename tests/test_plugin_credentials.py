# ruff: noqa: I001
import json

import pytest
from django.test import override_settings

from core import plugin_credentials
from core.tool_router import ToolExecutionError


@pytest.fixture
def isolated_providers():
    """Clear the provider registry for a test and restore it afterwards."""

    saved = dict(plugin_credentials._PROVIDERS)
    plugin_credentials._PROVIDERS.clear()
    try:
        yield
    finally:
        plugin_credentials._PROVIDERS.clear()
        plugin_credentials._PROVIDERS.update(saved)


def test_all_error_codes_are_tool_execution_errors_with_codes():
    expected = {
        "CREDENTIAL_RESOLVER_NOT_CONFIGURED": (
            plugin_credentials.CredentialResolverNotConfiguredError
        ),
        "CREDENTIAL_REFERENCE_INVALID": plugin_credentials.CredentialReferenceInvalidError,
        "CREDENTIAL_PROVIDER_UNSUPPORTED": (
            plugin_credentials.CredentialProviderUnsupportedError
        ),
        "CREDENTIAL_NOT_FOUND": plugin_credentials.CredentialNotFoundError,
        "CREDENTIAL_INVALID_SHAPE": plugin_credentials.CredentialInvalidShapeError,
        "CREDENTIAL_ACCESS_DENIED": plugin_credentials.CredentialAccessDeniedError,
    }
    for code, exc_type in expected.items():
        error = exc_type("detail")
        assert isinstance(error, ToolExecutionError)
        assert error.code == code


def test_env_provider_resolves_plain_token_as_bearer(monkeypatch):
    monkeypatch.setenv("PLUGIN_SECRET_GH_TOKEN", "tok_abcdef123456")
    credential = plugin_credentials.resolve("env://PLUGIN_SECRET_GH_TOKEN")
    assert credential == {"type": "bearer", "token": "tok_abcdef123456"}


def test_env_provider_resolves_json_header_credential(monkeypatch):
    monkeypatch.setenv(
        "PLUGIN_SECRET_API_KEY",
        json.dumps({"type": "header", "name": "X-Api-Key", "value": "header-secret-9"}),
    )
    credential = plugin_credentials.resolve("env://PLUGIN_SECRET_API_KEY")
    assert credential == {
        "type": "header",
        "name": "X-Api-Key",
        "value": "header-secret-9",
    }


def test_env_provider_resolves_json_query_credential(monkeypatch):
    monkeypatch.setenv(
        "PLUGIN_SECRET_QUERY_KEY",
        json.dumps({"type": "query", "name": "key", "value": "query-secret-7"}),
    )
    credential = plugin_credentials.resolve("env://PLUGIN_SECRET_QUERY_KEY")
    assert credential == {
        "type": "query",
        "name": "key",
        "value": "query-secret-7",
    }


def test_env_provider_rejects_missing_variable(monkeypatch):
    monkeypatch.delenv("PLUGIN_SECRET_MISSING", raising=False)
    with pytest.raises(plugin_credentials.CredentialNotFoundError) as exc:
        plugin_credentials.resolve("env://PLUGIN_SECRET_MISSING")
    assert exc.value.code == "CREDENTIAL_NOT_FOUND"
    assert "PLUGIN_SECRET_MISSING" not in str(exc.value)


def test_env_provider_rejects_variable_outside_prefix(monkeypatch):
    monkeypatch.setenv("UNRELATED_TOKEN", "leak-me")
    with pytest.raises(plugin_credentials.CredentialNotFoundError) as exc:
        plugin_credentials.resolve("env://UNRELATED_TOKEN")
    assert exc.value.code == "CREDENTIAL_NOT_FOUND"
    assert "leak-me" not in str(exc.value)


def test_env_provider_respects_configured_prefix(monkeypatch):
    monkeypatch.setenv("CUSTOM_SECRET_TOKEN", "custom-prefix-token")
    with override_settings(PLUGIN_CREDENTIAL_ENV_PREFIX="CUSTOM_SECRET_"):
        credential = plugin_credentials.resolve("env://CUSTOM_SECRET_TOKEN")
    assert credential == {"type": "bearer", "token": "custom-prefix-token"}


def test_unknown_scheme_is_rejected(isolated_providers):
    with pytest.raises(
        plugin_credentials.CredentialProviderUnsupportedError
    ) as exc:
        plugin_credentials.resolve("vault://platform/github")
    assert exc.value.code == "CREDENTIAL_PROVIDER_UNSUPPORTED"
    assert "platform/github" not in str(exc.value)


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "   ",
        "just-a-plain-string",
        "env:// PLUGIN_SECRET_X",
        "env://PLUGIN_SECRET_X\n",
        "env://PLUGIN_SECRET X",
    ],
)
def test_malformed_reference_is_rejected(reference):
    with pytest.raises(plugin_credentials.CredentialReferenceInvalidError) as exc:
        plugin_credentials.resolve(reference)
    assert exc.value.code == "CREDENTIAL_REFERENCE_INVALID"


def test_overlong_reference_is_rejected():
    reference = "env://" + "X" * 4096
    with pytest.raises(plugin_credentials.CredentialReferenceInvalidError):
        plugin_credentials.resolve(reference)


def test_env_provider_rejects_invalid_json(monkeypatch):
    monkeypatch.setenv("PLUGIN_SECRET_BROKEN", "{not-json")
    with pytest.raises(plugin_credentials.CredentialInvalidShapeError) as exc:
        plugin_credentials.resolve("env://PLUGIN_SECRET_BROKEN")
    assert exc.value.code == "CREDENTIAL_INVALID_SHAPE"


def test_env_provider_rejects_non_object_json(monkeypatch):
    monkeypatch.setenv("PLUGIN_SECRET_LIST", "[1, 2, 3]")
    with pytest.raises(plugin_credentials.CredentialInvalidShapeError):
        plugin_credentials.resolve("env://PLUGIN_SECRET_LIST")


def test_custom_provider_can_be_registered_and_discovered(isolated_providers):
    def kv_provider(reference):
        return {"type": "bearer", "token": "kv-secret"}

    plugin_credentials.register_provider("kv", kv_provider)
    assert "kv" in plugin_credentials.providers()
    assert plugin_credentials.resolve("kv://tenant/key") == {
        "type": "bearer",
        "token": "kv-secret",
    }


def test_registering_same_scheme_overwrites_previous_provider(isolated_providers):
    old = {"type": "query", "name": "a", "value": "old"}
    new = {"type": "query", "name": "a", "value": "new"}
    plugin_credentials.register_provider("kv", lambda ref: old)
    plugin_credentials.register_provider("kv", lambda ref: new)
    assert plugin_credentials.resolve("kv://k") == new


def test_provider_registration_is_idempotent(monkeypatch):
    monkeypatch.setenv("PLUGIN_SECRET_IDEMPOTENT", "repeatable-token")
    first = plugin_credentials.resolve("env://PLUGIN_SECRET_IDEMPOTENT")
    second = plugin_credentials.resolve("env://PLUGIN_SECRET_IDEMPOTENT")
    assert first == second


@pytest.mark.parametrize(
    "reference, expected",
    [
        ("env://PLUGIN_SECRET_FOO", "env://***"),
        ("vault://platform/github", "vault://***"),
        ("", "***"),
        (None, "***"),
    ],
)
def test_redact_masks_everything_after_scheme(reference, expected):
    assert plugin_credentials.redact(reference) == expected


def test_redact_never_leaks_variable_name_or_secret(monkeypatch):
    monkeypatch.setenv("PLUGIN_SECRET_REDACT", "super-secret-value")
    masked = plugin_credentials.redact("env://PLUGIN_SECRET_REDACT")
    assert "PLUGIN_SECRET_REDACT" not in masked
    assert "super-secret-value" not in masked
