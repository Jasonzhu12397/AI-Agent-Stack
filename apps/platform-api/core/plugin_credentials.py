# ruff: noqa: I001
"""Pluggable, fail-closed credential resolution for plugin connectors.

A credential reference is a URI such as ``env://PLUGIN_SECRET_GITHUB_TOKEN``.
The resolver dispatches to a registered provider based on the URI scheme.
Providers return a credential contract that the HTTP connector understands:

* ``{"type": "bearer", "token": "..."}``
* ``{"type": "header", "name": "X-Api-Key", "value": "..."}``
* ``{"type": "query", "name": "key", "value": "..."}``

Secrets are never logged, echoed in exception messages, or written to audit
metadata. Unresolved credentials fail closed: the connector never falls back
to an anonymous request.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx
from django.conf import settings
from django.utils.module_loading import import_string

from core.tool_router import ToolExecutionError


MAX_REFERENCE_LENGTH = 2048
DEFAULT_ENV_PREFIX = "PLUGIN_SECRET_"

VAULT_DEFAULT_FIELD = "value"
VAULT_MAX_RESPONSE_BYTES = 1024 * 1024
VAULT_DEFAULT_TIMEOUT_SECONDS = 5.0
_VAULT_MOUNT_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
_VAULT_PATH_PART_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
_VAULT_FIELD_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
_VAULT_LOCAL_HOSTS = {"127.0.0.1", "localhost"}

Provider = Callable[[str], dict[str, str]]

_PROVIDERS: dict[str, Provider] = {}

_WHITESPACE = re.compile(r"\s")
_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*")


class CredentialError(ToolExecutionError):
    """Base class for credential resolution failures."""

    code: str = "CREDENTIAL_ERROR"


class CredentialResolverNotConfiguredError(CredentialError):
    code = "CREDENTIAL_RESOLVER_NOT_CONFIGURED"


class CredentialReferenceInvalidError(CredentialError):
    code = "CREDENTIAL_REFERENCE_INVALID"


class CredentialProviderUnsupportedError(CredentialError):
    code = "CREDENTIAL_PROVIDER_UNSUPPORTED"


class CredentialNotFoundError(CredentialError):
    code = "CREDENTIAL_NOT_FOUND"


class CredentialInvalidShapeError(CredentialError):
    code = "CREDENTIAL_INVALID_SHAPE"


class CredentialAccessDeniedError(CredentialError):
    code = "CREDENTIAL_ACCESS_DENIED"


class CredentialNotAcceptedError(CredentialError):
    """The target does not take a credential, so there is nothing to bind."""

    code = "CREDENTIAL_NOT_ACCEPTED"


def register_provider(scheme: str, provider: Provider) -> None:
    """Register a provider for a URI scheme; registering twice overwrites."""

    normalized = scheme.lower()
    if not isinstance(normalized, str) or not _SCHEME.fullmatch(normalized):
        raise CredentialReferenceInvalidError("Credential provider scheme is invalid.")
    if not callable(provider):
        raise CredentialInvalidShapeError("Credential provider is not callable.")
    _PROVIDERS[normalized] = provider


def unregister_provider(scheme: str) -> None:
    """Remove a previously registered provider for a scheme."""

    _PROVIDERS.pop(scheme.lower(), None)


def providers() -> tuple[str, ...]:
    """Return the sorted list of registered provider schemes."""

    return tuple(sorted(_PROVIDERS))


def redact(reference: str) -> str:
    """Mask a credential reference, keeping only its scheme prefix."""

    if not isinstance(reference, str) or not reference:
        return "***"
    try:
        parsed = urlsplit(reference)
    except ValueError:
        return "***"
    scheme = parsed.scheme.lower()
    if scheme:
        return f"{scheme}://***"
    return "***"


def _parse_reference(reference: str):
    if (
        not isinstance(reference, str)
        or not reference
        or len(reference) > MAX_REFERENCE_LENGTH
        or _WHITESPACE.search(reference)
    ):
        raise CredentialReferenceInvalidError("Connector credential reference is invalid.")
    try:
        parsed = urlsplit(reference)
    except ValueError:
        raise CredentialReferenceInvalidError(
            "Connector credential reference is malformed."
        ) from None
    scheme = parsed.scheme.lower()
    if not _SCHEME.fullmatch(scheme) or (not parsed.netloc and not parsed.path):
        raise CredentialReferenceInvalidError("Connector credential reference is invalid.")
    return parsed


def _default_env_prefixes() -> tuple[str, ...]:
    prefix = getattr(settings, "PLUGIN_CREDENTIAL_ENV_PREFIX", DEFAULT_ENV_PREFIX)
    return (prefix,)


def _env_variable(parsed) -> str:
    return parsed.netloc or parsed.path.lstrip("/")


def _allowlisted_env_value(variable: str, prefixes: tuple[str, ...]) -> str:
    """Return the raw environment value of an allowlisted variable."""

    if not any(variable.startswith(prefix) for prefix in prefixes):
        raise CredentialNotFoundError(
            "Credential variable is outside the configured provider allowlist."
        )
    if variable not in os.environ:
        raise CredentialNotFoundError("Credential variable is not available to the process.")
    return os.environ[variable]


def _contract_plaintext(contract: dict[str, str]) -> str:
    """Flatten a credential contract to the single secret string it carries."""

    if contract.get("type") == "bearer":
        return str(contract.get("token") or "")
    return str(contract.get("value") or "")


def _env_provider(reference: str) -> dict[str, str]:
    parsed = urlsplit(reference)
    variable = _env_variable(parsed)
    raw_value = _allowlisted_env_value(variable, _default_env_prefixes())
    if raw_value[:1] in {"{", "["}:
        try:
            decoded = json.loads(raw_value)
        except (json.JSONDecodeError, ValueError) as exc:
            raise CredentialInvalidShapeError(
                "Credential value is not a valid JSON credential object."
            ) from exc
        if not isinstance(decoded, dict):
            raise CredentialInvalidShapeError("Credential JSON value must be an object.")
        return decoded
    return {"type": "bearer", "token": raw_value}


def _vault_setting(name: str, default: str = "") -> str:
    value = getattr(settings, name, os.getenv(name, default))
    return str(value or "").strip()


def _vault_timeout() -> float:
    raw = getattr(
        settings,
        "PLUGIN_VAULT_TIMEOUT_SECONDS",
        os.getenv("PLUGIN_VAULT_TIMEOUT_SECONDS", ""),
    )
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        timeout = VAULT_DEFAULT_TIMEOUT_SECONDS
    return timeout if timeout > 0 else VAULT_DEFAULT_TIMEOUT_SECONDS


def _vault_allows_insecure() -> bool:
    raw = getattr(
        settings,
        "PLUGIN_VAULT_ALLOW_INSECURE",
        os.getenv("PLUGIN_VAULT_ALLOW_INSECURE", "false"),
    )
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"true", "1", "yes"}


def _parse_vault_reference(reference: str) -> tuple[str, str, str]:
    parsed = urlsplit(reference)
    if parsed.username or parsed.password or parsed.port or parsed.query:
        raise CredentialReferenceInvalidError("Vault credential reference is invalid.")
    mount = parsed.netloc
    if not mount or not _VAULT_MOUNT_PATTERN.fullmatch(mount):
        raise CredentialReferenceInvalidError("Vault credential reference is invalid.")
    path = parsed.path.lstrip("/")
    segments = path.split("/") if path else []
    if not segments or any(
        not _VAULT_PATH_PART_PATTERN.fullmatch(segment) or segment in {".", ".."}
        for segment in segments
    ):
        raise CredentialReferenceInvalidError("Vault credential reference is invalid.")
    field = parsed.fragment or VAULT_DEFAULT_FIELD
    if not _VAULT_FIELD_PATTERN.fullmatch(field):
        raise CredentialReferenceInvalidError("Vault credential reference is invalid.")
    return mount, path, field


def _vault_origin() -> str:
    address = _vault_setting("PLUGIN_VAULT_ADDR").rstrip("/")
    if not address:
        raise CredentialResolverNotConfiguredError("Vault provider is not configured.")
    parsed = urlsplit(address)
    host = (parsed.hostname or "").lower()
    local_insecure = (
        parsed.scheme == "http" and host in _VAULT_LOCAL_HOSTS and _vault_allows_insecure()
    )
    if (
        not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or (parsed.port and not local_insecure)
    ):
        raise CredentialResolverNotConfiguredError("Vault address is not a permitted origin.")
    if parsed.scheme == "https" or local_insecure:
        return address
    raise CredentialResolverNotConfiguredError("Vault address must use a permitted HTTPS origin.")


def _vault_token() -> str:
    resolver_path = _vault_setting("PLUGIN_VAULT_TOKEN_RESOLVER")
    if resolver_path:
        try:
            resolver = import_string(resolver_path)
            token = resolver()
        except Exception:
            raise CredentialResolverNotConfiguredError(
                "Vault token resolver is unavailable."
            ) from None
    else:
        token = _vault_setting("PLUGIN_VAULT_TOKEN")
    if (
        not isinstance(token, str)
        or not token.strip()
        or token.strip() != token
        or any(character.isspace() for character in token)
    ):
        raise CredentialResolverNotConfiguredError("Vault authentication is not configured.")
    return token


def _read_vault_secret(
    client: httpx.Client, address: str, mount: str, path: str, token: str
) -> dict[str, object]:
    url = f"{address}/v1/{mount}/data/{path}"
    headers = {"X-Vault-Token": token}
    try:
        response = client.get(url, headers=headers)
    except httpx.HTTPError:
        raise CredentialAccessDeniedError("Vault secret lookup failed.") from None
    if response.status_code == 404:
        raise CredentialNotFoundError("Vault secret was not found.")
    if response.status_code in {401, 403}:
        raise CredentialAccessDeniedError("Vault access was denied.")
    if response.status_code != 200:
        raise CredentialAccessDeniedError("Vault secret lookup failed.")
    body = response.content
    if len(body) > VAULT_MAX_RESPONSE_BYTES:
        raise CredentialInvalidShapeError("Vault secret response exceeded the size limit.")
    try:
        payload = json.loads(body)
    except (TypeError, ValueError, UnicodeDecodeError):
        raise CredentialInvalidShapeError("Vault returned an invalid secret document.") from None
    if not isinstance(payload, dict):
        raise CredentialInvalidShapeError("Vault returned an invalid secret document.")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
        raise CredentialInvalidShapeError("Vault secret document is missing its data payload.")
    return data["data"]


def _valid_contract(raw_value: object) -> bool:
    if not isinstance(raw_value, dict):
        return False
    credential_type = raw_value.get("type")
    if credential_type == "bearer":
        return isinstance(raw_value.get("token"), str)
    if credential_type in {"header", "query"}:
        return isinstance(raw_value.get("name"), str) and isinstance(raw_value.get("value"), str)
    return False


def _vault_provider(reference: str) -> dict[str, str]:
    mount, path, field = _parse_vault_reference(reference)
    address = _vault_origin()
    token = _vault_token()
    timeout_value = _vault_timeout()
    timeout = httpx.Timeout(timeout_value, connect=min(3.0, timeout_value))
    with httpx.Client(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
    ) as client:
        secret = _read_vault_secret(client, address, mount, path, token)
    if field not in secret:
        raise CredentialInvalidShapeError("Vault secret field is missing.")
    raw_value = secret[field]
    if isinstance(raw_value, str):
        return {"type": "bearer", "token": raw_value}
    if _valid_contract(raw_value):
        return dict(raw_value)  # type: ignore[return-value]
    raise CredentialInvalidShapeError(
        "Vault secret field does not match a supported credential contract."
    )


def resolve(credential_ref: str) -> dict[str, str]:
    """Resolve a credential reference URI to a credential contract dict."""

    parsed = _parse_reference(credential_ref)
    provider = _PROVIDERS.get(parsed.scheme)
    if provider is None:
        raise CredentialProviderUnsupportedError(
            f"No credential provider is registered for scheme '{parsed.scheme}'."
        )
    return provider(credential_ref)


def validate_reference(reference: str) -> str:
    """Validate a credential reference and return its lowercased scheme.

    One syntax decision for the whole platform: plugin installations and payment
    channels accept exactly what the resolver accepts, so a reference cannot be
    refused by one caller and resolved by another.
    """

    return _parse_reference(reference).scheme


def resolve_plaintext(credential_ref: str, *, env_prefixes: tuple[str, ...] | None = None) -> str:
    """Resolve a credential reference to the single secret string it carries.

    Callers that need one secret (payment merchant keys, signing certificates)
    use this instead of building their own environment lookup, so there is one
    reference syntax and one allowlist decision per deployment. ``env_prefixes``
    narrows the environment allowlist for that caller; registered schemes other
    than ``env`` (``vault``) dispatch through the same provider registry.
    """

    parsed = _parse_reference(credential_ref)
    if parsed.scheme == "env":
        raw_value = _allowlisted_env_value(
            _env_variable(parsed),
            env_prefixes if env_prefixes is not None else _default_env_prefixes(),
        )
        if raw_value[:1] in {"{", "["}:
            try:
                decoded = json.loads(raw_value)
            except (json.JSONDecodeError, ValueError) as exc:
                raise CredentialInvalidShapeError(
                    "Credential value is not a valid JSON credential object."
                ) from exc
            if not isinstance(decoded, dict) or not _valid_contract(decoded):
                raise CredentialInvalidShapeError(
                    "Credential JSON value does not match a supported credential contract."
                )
            return _contract_plaintext(decoded)
        return raw_value
    provider = _PROVIDERS.get(parsed.scheme)
    if provider is None:
        raise CredentialProviderUnsupportedError(
            f"No credential provider is registered for scheme '{parsed.scheme}'."
        )
    return _contract_plaintext(provider(credential_ref))


register_provider("env", _env_provider)
register_provider("vault", _vault_provider)
