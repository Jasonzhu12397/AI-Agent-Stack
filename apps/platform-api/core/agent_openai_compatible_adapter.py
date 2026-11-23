"""OpenAI-compatible chat completions protocol adapter for marketplace plugins.

The adapter calls a generic ``/chat/completions`` HTTP endpoint described in the
plugin manifest. No vendor base URL, model family or provider is hard-coded:
the base URL, model id and request path all come from the stored manifest.

Manifest block::

    {
      "openai_compatible": {
        "base_url": "https://example-host.example.com",
        "model": "example-model",
        "path": "/v1/chat/completions",
        "timeout_seconds": 60
      }
    }

Error codes defined locally:

* ``CAPABILITY_INVOCATION_FAILED`` - generic adapter failure.
* ``CAPABILITY_TIMEOUT`` - the transport call timed out.
* ``CAPABILITY_UPSTREAM_CLIENT_ERROR`` - the upstream answered with a 4xx status.
* ``CAPABILITY_UPSTREAM_SERVER_ERROR`` - the upstream answered with a 5xx status.
* ``CAPABILITY_UPSTREAM_UNREACHABLE`` - a network-level transport failure.
* ``CAPABILITY_UPSTREAM_INVALID_RESPONSE`` - the response was not valid bounded
  JSON or did not carry an expected completion choice.
* ``CAPABILITY_CONFIG_INVALID`` - the plugin or manifest is misconfigured.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from core import plugin_credentials
from core.agent_capability_router import (
    CapabilityResolutionError,
    register_protocol_adapter,
)
from core.mcp_connector import _validate_endpoint
from core.plugin_credentials import CredentialInvalidShapeError
from core.tool_router import ToolExecutionError

PROTOCOL = "openai_compatible"

CAPABILITY_INVOCATION_FAILED = "CAPABILITY_INVOCATION_FAILED"
CAPABILITY_TIMEOUT = "CAPABILITY_TIMEOUT"
CAPABILITY_UPSTREAM_CLIENT_ERROR = "CAPABILITY_UPSTREAM_CLIENT_ERROR"
CAPABILITY_UPSTREAM_SERVER_ERROR = "CAPABILITY_UPSTREAM_SERVER_ERROR"
CAPABILITY_UPSTREAM_UNREACHABLE = "CAPABILITY_UPSTREAM_UNREACHABLE"
CAPABILITY_UPSTREAM_INVALID_RESPONSE = "CAPABILITY_UPSTREAM_INVALID_RESPONSE"
CAPABILITY_CONFIG_INVALID = "CAPABILITY_CONFIG_INVALID"
CAPABILITY_ARGUMENTS_OUT_OF_RANGE = "CAPABILITY_ARGUMENTS_OUT_OF_RANGE"

MAX_RESPONSE_BYTES = 1048576  # 1 MiB
MAX_REQUEST_BYTES = 65536  # 64 KiB
_MAX_DESCRIPTION_LENGTH = 1000
_DEFAULT_PATH = "/v1/chat/completions"
# Fallback upper bound for max_tokens when the manifest declares no
# ``openai_compatible.max_tokens_limit``.
_DEFAULT_MAX_TOKENS_LIMIT = 4096
_TEMPERATURE_MIN = 0.0
_TEMPERATURE_MAX = 2.0

_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,80}")
_QUERY_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")


def _clamp_timeout(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = fallback
    return min(max(parsed, 1), 300)


def _transport_block(plugin: Any) -> dict[str, Any]:
    manifest = plugin.manifest if isinstance(plugin.manifest, dict) else {}
    block = manifest.get("openai_compatible")
    return block if isinstance(block, dict) else {}


def _auth(installation: Any) -> tuple[dict[str, str], tuple[str, str] | None]:
    headers: dict[str, str] = {}
    query: tuple[str, str] | None = None
    ref = getattr(installation, "credential_ref", "") or ""
    if not ref:
        return headers, query
    credential = plugin_credentials.resolve(ref)
    if not isinstance(credential, dict):
        raise CredentialInvalidShapeError("Credential resolver returned an invalid credential.")
    kind = credential.get("type")
    if kind == "bearer":
        token = credential.get("token")
        if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
            raise CredentialInvalidShapeError(
                "Credential resolver returned an invalid bearer token."
            )
        headers["Authorization"] = "Bearer " + token
    elif kind == "header":
        name = credential.get("name")
        value = credential.get("value")
        if (
            not isinstance(name, str)
            or not _HEADER_NAME.fullmatch(name)
            or not isinstance(value, str)
            or not value
            or "\n" in value
            or "\r" in value
        ):
            raise CredentialInvalidShapeError(
                "Credential resolver returned an invalid header credential."
            )
        headers[name] = value
    elif kind == "query":
        name = credential.get("name")
        value = credential.get("value")
        if (
            not isinstance(name, str)
            or not _QUERY_NAME.fullmatch(name)
            or not isinstance(value, str)
            or not value
            or len(value) > 4096
        ):
            raise CredentialInvalidShapeError(
                "Credential resolver returned an invalid query credential."
            )
        query = (name, value)
    else:
        raise CredentialInvalidShapeError(
            "Credential resolver returned an unsupported credential type."
        )
    return headers, query


def _compose_url(base_url: str, path: str, query: tuple[str, str] | None) -> str:
    parsed = urlsplit(base_url)
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise CapabilityResolutionError(
            CAPABILITY_CONFIG_INVALID,
            "OpenAI-compatible request path must start with a single '/'.",
        )
    query_string = parsed.query
    if query is not None:
        pair = urlencode([query])
        query_string = pair if not query_string else f"{query_string}&{pair}"
    return urlunsplit(
        (parsed.scheme, parsed.hostname or "", parsed.path.rstrip("/") + path, query_string, "")
    )


def _messages(arguments: dict[str, Any]) -> list[dict[str, str]]:
    messages = arguments.get("messages")
    if isinstance(messages, list) and messages:
        normalized: list[dict[str, str]] = []
        for item in messages:
            if not isinstance(item, dict):
                raise CapabilityResolutionError(
                    CAPABILITY_INVOCATION_FAILED, "Message entries must be JSON objects."
                )
            role = item.get("role", "user")
            content = item.get("content", "")
            if not isinstance(role, str) or not isinstance(content, str):
                raise CapabilityResolutionError(
                    CAPABILITY_INVOCATION_FAILED, "Message role and content must be strings."
                )
            normalized.append({"role": role, "content": content})
        return normalized
    prompt = arguments.get("prompt")
    if prompt is None:
        prompt = arguments.get("message", "")
    if not isinstance(prompt, str):
        raise CapabilityResolutionError(
            CAPABILITY_INVOCATION_FAILED, "The prompt must be a string."
        )
    return [{"role": "user", "content": prompt}]


def _max_tokens_limit(block: dict[str, Any]) -> int:
    """Read the manifest max_tokens ceiling, falling back to the documented default."""

    raw = block.get("max_tokens_limit")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    return _DEFAULT_MAX_TOKENS_LIMIT


def _validate_completion_options(
    arguments: dict[str, Any], block: dict[str, Any]
) -> dict[str, Any]:
    """Validate completion options locally and return the legal subset to send.

    ``max_tokens`` must be a positive integer within the manifest ceiling;
    ``temperature`` must be a finite number in ``[0, 2]``. Booleans are not
    accepted as numbers. Omitted options are not invented; legal explicit
    values are preserved verbatim.
    """

    options: dict[str, Any] = {}

    max_tokens = arguments.get("max_tokens")
    if max_tokens is not None:
        if (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or max_tokens <= 0
        ):
            raise CapabilityResolutionError(
                CAPABILITY_ARGUMENTS_OUT_OF_RANGE,
                "max_tokens must be a positive integer.",
            )
        limit = _max_tokens_limit(block)
        if max_tokens > limit:
            raise CapabilityResolutionError(
                CAPABILITY_ARGUMENTS_OUT_OF_RANGE,
                f"max_tokens exceeds the configured limit of {limit}.",
            )
        options["max_tokens"] = max_tokens

    temperature = arguments.get("temperature")
    if temperature is not None:
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not math.isfinite(temperature)
        ):
            raise CapabilityResolutionError(
                CAPABILITY_ARGUMENTS_OUT_OF_RANGE, "temperature must be a number."
            )
        if temperature < _TEMPERATURE_MIN or temperature > _TEMPERATURE_MAX:
            raise CapabilityResolutionError(
                CAPABILITY_ARGUMENTS_OUT_OF_RANGE, "temperature must be between 0 and 2."
            )
        options["temperature"] = temperature

    return options


def _read_bounded(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise CapabilityResolutionError(
                CAPABILITY_UPSTREAM_INVALID_RESPONSE, "Upstream response exceeds 1 MiB."
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _load_json(payload: bytes) -> Any:
    try:
        return json.loads(payload.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CapabilityResolutionError(
            CAPABILITY_UPSTREAM_INVALID_RESPONSE, "Upstream response is not valid JSON."
        ) from exc


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant: {value}")


def discover(installation: Any) -> list[dict[str, Any]]:
    """No remote discovery surface exists for OpenAI-compatible endpoints.

    Capabilities are declared in the manifest; the discover endpoint merges the
    stored declarations without performing a remote catalog pull.
    """

    return []


def _invoke(
    installation: Any,
    definition: dict[str, Any],
    arguments: dict[str, Any],
    approved: bool,
) -> dict[str, Any]:
    plugin = installation.plugin
    block = _transport_block(plugin)

    base_url = block.get("base_url") or getattr(plugin, "endpoint_url", "")
    model = block.get("model")
    if not isinstance(model, str) or not model:
        raise CapabilityResolutionError(
            CAPABILITY_CONFIG_INVALID, "openai_compatible.model must be declared in the manifest."
        )
    path = block.get("path", _DEFAULT_PATH)
    timeout = _clamp_timeout(
        block.get("timeout_seconds", definition.get("timeout_seconds", plugin.timeout_seconds)), 60
    )
    headers, query = _auth(installation)
    headers["Content-Type"] = "application/json"
    headers["Accept"] = "application/json"

    body: dict[str, Any] = {"model": model, "messages": _messages(arguments), "stream": False}
    body.update(_validate_completion_options(arguments, block))
    encoded = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(encoded) > MAX_REQUEST_BYTES:
        raise CapabilityResolutionError(
            CAPABILITY_INVOCATION_FAILED, "Request body exceeds 64 KiB."
        )

    try:
        validated_base = _validate_endpoint(base_url)
        url = _compose_url(validated_base, path, query)
        with httpx.Client(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10)),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            try:
                response = client.post(url, content=encoded, headers=headers)
            except httpx.ConnectTimeout as exc:
                raise CapabilityResolutionError(
                    CAPABILITY_UPSTREAM_UNREACHABLE,
                    "The upstream chat completion endpoint is unreachable.",
                ) from exc
            except httpx.TimeoutException as exc:
                raise CapabilityResolutionError(
                    CAPABILITY_TIMEOUT,
                    "The upstream chat completion request timed out.",
                ) from exc
            except httpx.HTTPError as exc:
                raise CapabilityResolutionError(
                    CAPABILITY_UPSTREAM_UNREACHABLE,
                    "The upstream chat completion endpoint is unreachable.",
                ) from exc

            if 300 <= response.status_code < 400:
                raise CapabilityResolutionError(
                    CAPABILITY_UPSTREAM_UNREACHABLE, "Upstream redirects are not allowed."
                )
            if 400 <= response.status_code < 500:
                raise CapabilityResolutionError(
                    CAPABILITY_UPSTREAM_CLIENT_ERROR,
                    f"Upstream chat completion failed with status {response.status_code}.",
                )
            if 500 <= response.status_code < 600:
                raise CapabilityResolutionError(
                    CAPABILITY_UPSTREAM_SERVER_ERROR,
                    f"Upstream chat completion failed with status {response.status_code}.",
                )
            if not 200 <= response.status_code < 300:
                raise CapabilityResolutionError(
                    CAPABILITY_UPSTREAM_INVALID_RESPONSE,
                    f"Upstream chat completion returned status {response.status_code}.",
                )
            payload_bytes = _read_bounded(response)
    except ToolExecutionError as exc:
        # Endpoint validation failures raised by the shared validator.
        raise CapabilityResolutionError(CAPABILITY_CONFIG_INVALID, str(exc)) from exc

    payload = _load_json(payload_bytes)
    if not isinstance(payload, dict):
        raise CapabilityResolutionError(
            CAPABILITY_UPSTREAM_INVALID_RESPONSE, "Upstream response must be a JSON object."
        )
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise CapabilityResolutionError(
            CAPABILITY_UPSTREAM_INVALID_RESPONSE, "Upstream response did not return any choices."
        )
    choice = choices[0]
    if not isinstance(choice, dict):
        raise CapabilityResolutionError(
            CAPABILITY_UPSTREAM_INVALID_RESPONSE, "Upstream choice entry is invalid."
        )
    message = choice.get("message")
    if not isinstance(message, dict):
        raise CapabilityResolutionError(
            CAPABILITY_UPSTREAM_INVALID_RESPONSE, "Upstream choice message is invalid."
        )
    content = message.get("content", "")
    if not isinstance(content, str):
        raise CapabilityResolutionError(
            CAPABILITY_UPSTREAM_INVALID_RESPONSE, "Upstream message content must be a string."
        )
    return {"text": content}


def register() -> None:
    """Register this adapter under the ``openai_compatible`` protocol."""

    register_protocol_adapter(PROTOCOL, _invoke)
