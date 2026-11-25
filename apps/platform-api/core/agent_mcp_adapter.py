"""MCP streamable HTTP protocol adapter for marketplace agent plugins.

This adapter maps a remote Model Context Protocol (MCP) server exposed through
``plugin.endpoint_url`` into the platform capability model. Discovery performs
the MCP ``initialize`` handshake and a paged ``tools/list``; invocation performs
``tools/call`` and normalizes the MCP result into a bounded JSON object.

The transport reuses :class:`core.mcp_connector.McpClient` and its endpoint
validation (HTTPS, allowlist, public DNS, size caps, redirect prohibition). No
vendor host or endpoint is hard-coded here; every value comes from the stored
plugin definition and manifest.

Error codes defined locally:

* ``CAPABILITY_INVOCATION_FAILED`` - generic upstream or adapter failure.
* ``CAPABILITY_TIMEOUT`` - reserved for callers that can distinguish a
  transport timeout; the shared MCP connector normalizes transport errors into
  invocation failures, so MCP timeouts surface as ``CAPABILITY_INVOCATION_FAILED``.
* ``CAPABILITY_UPSTREAM_ERROR`` - the remote MCP server answered with an
  application-level error (``isError`` or a JSON-RPC error frame).
* ``CAPABILITY_CONFIG_INVALID`` - the plugin or manifest is misconfigured.
"""

from __future__ import annotations

import re
from typing import Any

from core import plugin_credentials
from core.agent_capability_router import (
    CapabilityResolutionError,
    register_protocol_adapter,
)
from core.mcp_connector import McpClient, _validate_endpoint
from core.plugin_credentials import CredentialInvalidShapeError
from core.tool_router import (
    FAULT_AUTH_REJECTED,
    FAULT_CLIENT_ERROR,
    FAULT_INVALID_RESPONSE,
    FAULT_SERVER_ERROR,
    FAULT_TIMEOUT,
    FAULT_UNREACHABLE,
    FAULT_UPSTREAM_ERROR,
    ToolExecutionError,
)

PROTOCOL = "mcp"

# Stable local error codes. These live in this module on purpose: the router
# module owns its own code set and must not be extended by protocol adapters.
CAPABILITY_INVOCATION_FAILED = "CAPABILITY_INVOCATION_FAILED"
CAPABILITY_TIMEOUT = "CAPABILITY_TIMEOUT"
CAPABILITY_UPSTREAM_ERROR = "CAPABILITY_UPSTREAM_ERROR"
CAPABILITY_UPSTREAM_AUTH_REJECTED = "CAPABILITY_UPSTREAM_AUTH_REJECTED"
CAPABILITY_UPSTREAM_CLIENT_ERROR = "CAPABILITY_UPSTREAM_CLIENT_ERROR"
CAPABILITY_UPSTREAM_SERVER_ERROR = "CAPABILITY_UPSTREAM_SERVER_ERROR"
CAPABILITY_UPSTREAM_INVALID_RESPONSE = "CAPABILITY_UPSTREAM_INVALID_RESPONSE"
CAPABILITY_UPSTREAM_UNREACHABLE = "CAPABILITY_UPSTREAM_UNREACHABLE"
CAPABILITY_CONFIG_INVALID = "CAPABILITY_CONFIG_INVALID"

# Connector taxonomy category -> capability raised code. Categories without an
# entry keep the generic invocation failure token.
_FAULT_CODES: dict[str, str] = {
    FAULT_AUTH_REJECTED: CAPABILITY_UPSTREAM_AUTH_REJECTED,
    FAULT_CLIENT_ERROR: CAPABILITY_UPSTREAM_CLIENT_ERROR,
    FAULT_SERVER_ERROR: CAPABILITY_UPSTREAM_SERVER_ERROR,
    FAULT_TIMEOUT: CAPABILITY_TIMEOUT,
    FAULT_INVALID_RESPONSE: CAPABILITY_UPSTREAM_INVALID_RESPONSE,
    FAULT_UNREACHABLE: CAPABILITY_UPSTREAM_UNREACHABLE,
    FAULT_UPSTREAM_ERROR: CAPABILITY_UPSTREAM_ERROR,
}

_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_MAX_NAME_LENGTH = 100
_MAX_DESCRIPTION_LENGTH = 1000
_MAX_CAPABILITIES = 32

_EMPTY_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

# Discovered tools return either a rendered text payload or a structured object
# under ``data``; the closed schema mirrors that two-shape result contract.
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


def _closed_object_schema(schema: Any) -> dict[str, Any] | None:
    """Return a closed-object copy of ``schema``, or ``None`` when it is invalid."""

    if not isinstance(schema, dict) or schema.get("type") != "object":
        return None
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
    # Keep local definition blocks so ``$ref`` pointers recorded by the remote
    # tool schema can be resolved by the shared argument validator.
    for key in ("definitions", "$defs"):
        definitions = schema.get(key)
        if isinstance(definitions, dict):
            closed[key] = definitions
    return closed


def _normalize_name(raw: str) -> tuple[str, str] | None:
    """Map a remote tool name to a local capability name.

    Names that already match the platform pattern are kept verbatim. Other
    names are lower-cased, have invalid characters replaced with ``_`` and are
    trimmed to the bounded identifier shape. The original remote name is kept
    on the capability as ``remote_name`` so invocation can call the right tool.
    """

    if not isinstance(raw, str) or not raw:
        return None
    original = raw[:_MAX_NAME_LENGTH]
    if _NAME_PATTERN.fullmatch(original):
        return original, original
    cleaned = re.sub(r"[^a-z0-9._-]", "_", original.lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("._-")
    if not cleaned:
        cleaned = "tool"
    if not cleaned[0].isalnum():
        cleaned = "t" + cleaned
    cleaned = cleaned[:_MAX_NAME_LENGTH].rstrip("._-")
    if not cleaned:
        cleaned = "tool"
    return cleaned, original


def _auth_headers(installation: Any) -> tuple[dict[str, str], tuple[str, str] | None]:
    """Resolve the bound credential into request headers and an optional query pair."""

    headers: dict[str, str] = {}
    query: tuple[str, str] | None = None
    ref = getattr(installation, "credential_ref", "") or ""
    if not ref:
        return headers, query
    credential = plugin_credentials.resolve(ref, tenant_id=getattr(installation, "tenant_id", None))
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
            or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,80}", name)
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
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", name)
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


def _transport_block(plugin: Any) -> dict[str, Any]:
    manifest = plugin.manifest if isinstance(plugin.manifest, dict) else {}
    block = manifest.get("mcp")
    return block if isinstance(block, dict) else {}


def _resolve_endpoint(installation: Any) -> str:
    plugin = installation.plugin
    endpoint = getattr(plugin, "endpoint_url", "")
    return _validate_endpoint(endpoint, require_allowlist=False)


def _timeout_seconds(plugin: Any, block: dict[str, Any], definition: dict[str, Any] | None) -> int:
    if definition and definition.get("timeout_seconds") is not None:
        source = definition.get("timeout_seconds")
    elif block.get("timeout_seconds") is not None:
        source = block.get("timeout_seconds")
    else:
        source = getattr(plugin, "timeout_seconds", 60)
    return _clamp_timeout(source, 60)


def discover(installation: Any) -> list[dict[str, Any]]:
    """Discover remote MCP tools and map them to platform capability declarations."""

    plugin = installation.plugin
    block = _transport_block(plugin)
    endpoint = _resolve_endpoint(installation)
    headers, _query = _auth_headers(installation)
    timeout = _timeout_seconds(plugin, block, None)
    risk_levels = block.get("risk_levels")
    if not isinstance(risk_levels, dict):
        risk_levels = {}

    try:
        with McpClient(endpoint, headers, timeout) as client:
            client.initialize()
            tools = client.list_tools()
    except ToolExecutionError as exc:
        raise CapabilityResolutionError(
            _FAULT_CODES.get(exc.code, CAPABILITY_UPSTREAM_ERROR), str(exc)
        ) from exc

    capabilities: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for tool in tools:
        raw_schema = tool.get("inputSchema")
        if raw_schema is None:
            raw_schema = tool.get("input_schema")
        schema = _closed_object_schema(raw_schema)
        if schema is None:
            # Non-object input schemas cannot be expressed in the closed-object
            # capability contract; skip the tool and keep the rest of the list.
            continue
        normalized = _normalize_name(tool.get("name", ""))
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

        description = str(tool.get("description", ""))[:_MAX_DESCRIPTION_LENGTH]
        risk_level = risk_levels.get(remote_name, "low")
        if risk_level not in {"unclassified", "low", "medium", "high", "critical"}:
            risk_level = "unclassified"
        capabilities.append(
            {
                "name": local_name,
                "description": description,
                "input_schema": schema,
                "output_schema": _DISCOVERED_OUTPUT_SCHEMA,
                "risk_level": risk_level,
                "timeout_seconds": timeout,
                "remote_name": remote_name,
            }
        )
        if len(capabilities) >= _MAX_CAPABILITIES:
            break
    return capabilities


def _result_text(content: list[dict[str, Any]]) -> str:
    parts = []
    for item in content:
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts)


def _invoke(
    installation: Any,
    definition: dict[str, Any],
    arguments: dict[str, Any],
    approved: bool,
) -> dict[str, Any]:
    """Invoke a single discovered MCP tool and normalize its result."""

    plugin = installation.plugin
    block = _transport_block(plugin)
    endpoint = _resolve_endpoint(installation)
    headers, _query = _auth_headers(installation)
    timeout = _timeout_seconds(plugin, block, definition)

    tool_map = block.get("tool_map")
    if not isinstance(tool_map, dict):
        tool_map = {}
    remote_name = definition.get("remote_name") or tool_map.get(
        definition["name"], definition["name"]
    )
    if not isinstance(remote_name, str) or not remote_name or len(remote_name) > _MAX_NAME_LENGTH:
        raise CapabilityResolutionError(
            CAPABILITY_CONFIG_INVALID, "MCP tool mapping produced an invalid tool name."
        )

    try:
        with McpClient(endpoint, headers, timeout) as client:
            client.initialize()
            payload = client.call_tool(remote_name, arguments)
    except ToolExecutionError as exc:
        raise CapabilityResolutionError(
            _FAULT_CODES.get(exc.code, CAPABILITY_INVOCATION_FAILED), str(exc)
        ) from exc

    if payload.get("isError") is True:
        message = _result_text(payload.get("content", [])) or "The MCP tool reported an error."
        raise CapabilityResolutionError(
            CAPABILITY_UPSTREAM_ERROR, message[:_MAX_DESCRIPTION_LENGTH]
        )

    structured = payload.get("structuredContent")
    text = _result_text(payload.get("content", []))
    output_properties = (definition.get("output_schema") or {}).get("properties") or {}
    if (plugin.manifest or {}).get("studio") and set(output_properties) == {"text"}:
        import json

        value = structured if isinstance(structured, dict) else text
        if isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False)[:65536]
        return {"text": str(value)[:65536]}
    if isinstance(structured, dict):
        return {"data": structured}
    return {"text": text}


def register() -> None:
    """Register this adapter under the ``mcp`` protocol."""

    register_protocol_adapter(PROTOCOL, _invoke)
