"""A2A (Agent-to-Agent) protocol adapter with agent card discovery.

Discovery fetches the remote agent card at
``/.well-known/agent-card.json`` (overridable through the manifest ``a2a``
block), validates its minimum fields and maps each declared skill to a platform
capability. Invocation posts a JSON-RPC 2.0 ``message/send`` request to
``plugin.endpoint_url`` and normalizes the returned artifact or message text.

Manifest block::

    {
      "a2a": {
        "agent_card_path": "/.well-known/agent-card.json",
        "timeout_seconds": 60
      }
    }

Error codes defined locally:

* ``CAPABILITY_INVOCATION_FAILED`` - generic adapter failure.
* ``CAPABILITY_TIMEOUT`` - the transport call timed out.
* ``CAPABILITY_UPSTREAM_ERROR`` - the remote agent answered with a JSON-RPC
  error frame or an application-level failure.
* ``CAPABILITY_UPSTREAM_CLIENT_ERROR`` - the remote returned a 4xx status.
* ``CAPABILITY_UPSTREAM_SERVER_ERROR`` - the remote returned a 5xx status.
* ``CAPABILITY_UPSTREAM_UNREACHABLE`` - a network-level transport failure.
* ``CAPABILITY_UPSTREAM_INVALID_RESPONSE`` - the response was not valid bounded
  JSON or did not match the expected A2A shape.
* ``CAPABILITY_CONFIG_INVALID`` - the plugin, card or manifest is misconfigured.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from core import plugin_credentials
from core.agent_capability_router import (
    CapabilityResolutionError,
    register_protocol_adapter,
)
from core.mcp_connector import _validate_endpoint
from core.plugin_credentials import CredentialInvalidShapeError
from core.tool_router import ToolExecutionError

PROTOCOL = "a2a"

CAPABILITY_INVOCATION_FAILED = "CAPABILITY_INVOCATION_FAILED"
CAPABILITY_TIMEOUT = "CAPABILITY_TIMEOUT"
CAPABILITY_UPSTREAM_ERROR = "CAPABILITY_UPSTREAM_ERROR"
CAPABILITY_UPSTREAM_CLIENT_ERROR = "CAPABILITY_UPSTREAM_CLIENT_ERROR"
CAPABILITY_UPSTREAM_SERVER_ERROR = "CAPABILITY_UPSTREAM_SERVER_ERROR"
CAPABILITY_UPSTREAM_UNREACHABLE = "CAPABILITY_UPSTREAM_UNREACHABLE"
CAPABILITY_UPSTREAM_INVALID_RESPONSE = "CAPABILITY_UPSTREAM_INVALID_RESPONSE"
CAPABILITY_CONFIG_INVALID = "CAPABILITY_CONFIG_INVALID"
CAPABILITY_STREAMING_UNSUPPORTED = "CAPABILITY_STREAMING_UNSUPPORTED"

MAX_RESPONSE_BYTES = 1048576  # 1 MiB
_MAX_NAME_LENGTH = 100
_MAX_DESCRIPTION_LENGTH = 1000
_MAX_CAPABILITIES = 32
_DEFAULT_CARD_PATH = "/.well-known/agent-card.json"
_JSONRPC_VERSION = "2.0"

_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,80}")
_QUERY_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")

_EMPTY_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

_DISCOVERED_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "data": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    "required": [],
    "additionalProperties": False,
}


def _clamp_timeout(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = fallback
    return min(max(parsed, 1), 300)


def _transport_block(plugin: Any) -> dict[str, Any]:
    manifest = plugin.manifest if isinstance(plugin.manifest, dict) else {}
    block = manifest.get("a2a")
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


def _closed_object_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return dict(_EMPTY_OBJECT_SCHEMA)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    required = schema.get("required")
    if not isinstance(required, list):
        required = []
    required = [item for item in required if isinstance(item, str)]
    closed: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    # Keep local definition blocks so ``$ref`` pointers can be resolved by the
    # shared argument validator.
    for key in ("definitions", "$defs"):
        definitions = schema.get(key)
        if isinstance(definitions, dict):
            closed[key] = definitions
    return closed


def _normalize_name(raw: str) -> tuple[str, str] | None:
    if not isinstance(raw, str) or not raw:
        return None
    original = raw[:_MAX_NAME_LENGTH]
    if _NAME_PATTERN.fullmatch(original):
        return original, original
    cleaned = re.sub(r"[^a-z0-9._-]", "_", original.lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("._-")
    if not cleaned:
        cleaned = "skill"
    if not cleaned[0].isalnum():
        cleaned = "s" + cleaned
    cleaned = cleaned[:_MAX_NAME_LENGTH].rstrip("._-")
    if not cleaned:
        cleaned = "skill"
    return cleaned, original


def _card_url(endpoint: str, card_path: str) -> str:
    parsed = urlsplit(endpoint)
    if (
        not isinstance(card_path, str)
        or not card_path.startswith("/")
        or card_path.startswith("//")
    ):
        raise CapabilityResolutionError(
            CAPABILITY_CONFIG_INVALID, "agent_card_path must start with a single '/'."
        )
    return urlunsplit(
        (parsed.scheme, parsed.hostname or "", parsed.path.rstrip("/") + card_path, "", "")
    )


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


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant: {value}")


def _load_json(payload: bytes) -> Any:
    try:
        return json.loads(payload.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CapabilityResolutionError(
            CAPABILITY_UPSTREAM_INVALID_RESPONSE, "Upstream response is not valid JSON."
        ) from exc


def _http_client(timeout: int) -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(timeout, connect=min(timeout, 10)),
        follow_redirects=False,
        trust_env=False,
    )


def _validate_card(card: Any) -> tuple[dict[str, Any], bool]:
    """Validate the minimum card shape and return the declared streaming flag.

    A missing or non-object ``capabilities`` block is rejected as a missing
    required field. The ``capabilities.streaming`` flag is relaxed: when it is
    absent or not a boolean, the card defaults to non-streaming (``False``);
    invocation rejects any streaming request locally instead of contacting a
    ``message/stream`` endpoint.
    """

    if not isinstance(card, dict):
        raise CapabilityResolutionError(
            CAPABILITY_UPSTREAM_INVALID_RESPONSE, "Agent card must be a JSON object."
        )
    missing = []
    if not isinstance(card.get("name"), str) or not card.get("name"):
        missing.append("name")
    if not isinstance(card.get("description"), str):
        missing.append("description")
    capabilities = card.get("capabilities")
    if not isinstance(capabilities, dict):
        missing.append("capabilities")
    if not isinstance(card.get("skills"), list):
        missing.append("skills")
    if missing:
        raise CapabilityResolutionError(
            CAPABILITY_CONFIG_INVALID,
            f"Agent card is missing required fields: {', '.join(missing)}.",
        )
    streaming = capabilities.get("streaming")
    if not isinstance(streaming, bool):
        streaming = False
    return card, streaming


def discover_card(installation: Any) -> list[dict[str, Any]]:
    """Fetch and validate the remote agent card, mapping skills to capabilities."""

    plugin = installation.plugin
    block = _transport_block(plugin)
    endpoint = _validate_endpoint(getattr(plugin, "endpoint_url", ""), require_allowlist=False)
    card_path = block.get("agent_card_path", _DEFAULT_CARD_PATH)
    timeout = _clamp_timeout(block.get("timeout_seconds", plugin.timeout_seconds), 60)
    headers, _query = _auth(installation)
    headers["Accept"] = "application/json"
    url = _card_url(endpoint, card_path)

    try:
        with _http_client(timeout) as client:
            try:
                response = client.get(url, headers=headers)
            except httpx.ConnectTimeout as exc:
                raise CapabilityResolutionError(
                    CAPABILITY_UPSTREAM_UNREACHABLE, "The agent card endpoint is unreachable."
                ) from exc
            except httpx.TimeoutException as exc:
                raise CapabilityResolutionError(
                    CAPABILITY_TIMEOUT, "The agent card request timed out."
                ) from exc
            except httpx.HTTPError as exc:
                raise CapabilityResolutionError(
                    CAPABILITY_UPSTREAM_UNREACHABLE, "The agent card endpoint is unreachable."
                ) from exc
            if 300 <= response.status_code < 400:
                raise CapabilityResolutionError(
                    CAPABILITY_UPSTREAM_UNREACHABLE, "Upstream redirects are not allowed."
                )
            if not 200 <= response.status_code < 300:
                raise CapabilityResolutionError(
                    CAPABILITY_UPSTREAM_INVALID_RESPONSE,
                    f"Agent card request returned status {response.status_code}.",
                )
            card = _load_json(_read_bounded(response))
    except ToolExecutionError as exc:
        raise CapabilityResolutionError(CAPABILITY_CONFIG_INVALID, str(exc)) from exc

    card, streaming = _validate_card(card)

    capabilities: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for skill in card["skills"]:
        if not isinstance(skill, dict):
            continue
        normalized = _normalize_name(skill.get("id") or skill.get("name") or "")
        if normalized is None:
            continue
        local_name, remote_name = normalized
        if local_name in seen_names:
            suffix = 2
            candidate = f"{local_name}_{suffix}"
            while candidate in seen_names:
                suffix += 1
                candidate = f"{local_name}_{suffix}"
            local_name = candidate
        seen_names.add(local_name)

        description = str(skill.get("description", ""))[:_MAX_DESCRIPTION_LENGTH]
        capabilities.append(
            {
                "name": local_name,
                "description": description,
                "input_schema": _closed_object_schema(
                    skill.get("inputSchema") or skill.get("input")
                ),
                "output_schema": _closed_object_schema(
                    skill.get("outputSchema") or skill.get("output")
                ),
                "risk_level": "low",
                "timeout_seconds": timeout,
                "remote_name": remote_name,
                "streaming": streaming,
            }
        )
        if len(capabilities) >= _MAX_CAPABILITIES:
            break
    return capabilities


def _result_text(result: Any) -> str | None:
    if not isinstance(result, dict):
        return None
    artifacts = result.get("artifacts")
    if isinstance(artifacts, list) and artifacts:
        artifact = artifacts[0]
        if isinstance(artifact, dict):
            text = artifact.get("text")
            if isinstance(text, str):
                return text
            parts = artifact.get("parts")
            if isinstance(parts, list) and parts:
                part = parts[0]
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    return part["text"]
    message = result.get("message")
    if isinstance(message, dict):
        parts = message.get("parts")
        if isinstance(parts, list) and parts:
            part = parts[0]
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                return part["text"]
    return None


def _invoke(
    installation: Any,
    definition: dict[str, Any],
    arguments: dict[str, Any],
    approved: bool,
) -> dict[str, Any]:
    plugin = installation.plugin
    block = _transport_block(plugin)
    endpoint = _validate_endpoint(getattr(plugin, "endpoint_url", ""), require_allowlist=False)
    timeout = _clamp_timeout(
        block.get("timeout_seconds", definition.get("timeout_seconds", plugin.timeout_seconds)), 60
    )

    wants_stream = arguments.get("stream") is True or arguments.get("streaming") is True
    streaming_allowed = definition.get("streaming") is True
    if wants_stream and not streaming_allowed:
        # Reject locally: no message/stream request is sent to the remote agent.
        raise CapabilityResolutionError(
            CAPABILITY_STREAMING_UNSUPPORTED,
            "The agent card does not declare streaming support.",
        )

    headers, _query = _auth(installation)
    headers["Content-Type"] = "application/json"
    headers["Accept"] = "application/json, text/event-stream"

    text = arguments.get("prompt")
    if text is None:
        text = arguments.get("message", arguments.get("text", ""))
    if not isinstance(text, str):
        raise CapabilityResolutionError(
            CAPABILITY_INVOCATION_FAILED, "The A2A message must be a string."
        )

    payload = {
        "jsonrpc": _JSONRPC_VERSION,
        "id": 1,
        "method": "message/send",
        "params": {
            "message": {
                "messageId": str(uuid.uuid4()),
                "role": "user",
                "parts": [{"kind": "text", "text": text}],
            }
        },
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        raise CapabilityResolutionError(CAPABILITY_INVOCATION_FAILED, "Request body exceeds 1 MiB.")

    try:
        with _http_client(timeout) as client:
            try:
                response = client.post(endpoint, content=encoded, headers=headers)
            except httpx.ConnectTimeout as exc:
                raise CapabilityResolutionError(
                    CAPABILITY_UPSTREAM_UNREACHABLE, "The A2A endpoint is unreachable."
                ) from exc
            except httpx.TimeoutException as exc:
                raise CapabilityResolutionError(
                    CAPABILITY_TIMEOUT, "The A2A message request timed out."
                ) from exc
            except httpx.HTTPError as exc:
                raise CapabilityResolutionError(
                    CAPABILITY_UPSTREAM_UNREACHABLE, "The A2A endpoint is unreachable."
                ) from exc
            if 300 <= response.status_code < 400:
                raise CapabilityResolutionError(
                    CAPABILITY_UPSTREAM_UNREACHABLE, "Upstream redirects are not allowed."
                )
            if 400 <= response.status_code < 500:
                raise CapabilityResolutionError(
                    CAPABILITY_UPSTREAM_CLIENT_ERROR,
                    f"A2A endpoint failed with status {response.status_code}.",
                )
            if 500 <= response.status_code < 600:
                raise CapabilityResolutionError(
                    CAPABILITY_UPSTREAM_SERVER_ERROR,
                    f"A2A endpoint failed with status {response.status_code}.",
                )
            if not 200 <= response.status_code < 300:
                raise CapabilityResolutionError(
                    CAPABILITY_UPSTREAM_INVALID_RESPONSE,
                    f"A2A endpoint returned status {response.status_code}.",
                )
            frame = _load_json(_read_bounded(response))
    except ToolExecutionError as exc:
        raise CapabilityResolutionError(CAPABILITY_CONFIG_INVALID, str(exc)) from exc

    if not isinstance(frame, dict):
        raise CapabilityResolutionError(
            CAPABILITY_UPSTREAM_INVALID_RESPONSE, "A2A response must be a JSON object."
        )
    if "error" in frame:
        error = frame["error"]
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise CapabilityResolutionError(
            CAPABILITY_UPSTREAM_ERROR,
            str(message)[:_MAX_DESCRIPTION_LENGTH] or "A2A request failed.",
        )
    result = frame.get("result")
    rendered = _result_text(result)
    if rendered is not None:
        return {"text": rendered}
    if isinstance(result, dict):
        return {"data": result}
    raise CapabilityResolutionError(
        CAPABILITY_UPSTREAM_INVALID_RESPONSE, "A2A response did not carry an artifact or message."
    )


def register() -> None:
    """Register this adapter under the ``a2a`` protocol."""

    register_protocol_adapter(PROTOCOL, _invoke)
