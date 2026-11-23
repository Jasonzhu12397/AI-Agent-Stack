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

from django.conf import settings

from core.tool_router import ToolExecutionError


MAX_REFERENCE_LENGTH = 2048
DEFAULT_ENV_PREFIX = "PLUGIN_SECRET_"

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


def _env_provider(reference: str) -> dict[str, str]:
    parsed = urlsplit(reference)
    variable = parsed.netloc or parsed.path.lstrip("/")
    prefix = getattr(settings, "PLUGIN_CREDENTIAL_ENV_PREFIX", DEFAULT_ENV_PREFIX)
    if not variable.startswith(prefix):
        raise CredentialNotFoundError(
            "Credential variable is outside the configured provider allowlist."
        )
    if variable not in os.environ:
        raise CredentialNotFoundError("Credential variable is not available to the process.")
    raw_value = os.environ[variable]
    if raw_value[:1] in {"{", "["}:
        try:
            decoded = json.loads(raw_value)
        except (json.JSONDecodeError, ValueError) as exc:
            raise CredentialInvalidShapeError(
                "Credential value is not a valid JSON credential object."
            ) from exc
        if not isinstance(decoded, dict):
            raise CredentialInvalidShapeError(
                "Credential JSON value must be an object."
            )
        return decoded
    return {"type": "bearer", "token": raw_value}


def resolve(credential_ref: str) -> dict[str, str]:
    """Resolve a credential reference URI to a credential contract dict."""

    parsed = _parse_reference(credential_ref)
    provider = _PROVIDERS.get(parsed.scheme)
    if provider is None:
        raise CredentialProviderUnsupportedError(
            f"No credential provider is registered for scheme '{parsed.scheme}'."
        )
    return provider(credential_ref)


register_provider("env", _env_provider)
