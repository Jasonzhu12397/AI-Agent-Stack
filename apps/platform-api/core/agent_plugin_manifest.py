"""Manifest contract validation and read helpers for marketplace agent plugins.

The manifest is the data-driven description of what an installed agent plugin
exposes: capability declarations, closed-object schemas, risk levels and the
HTTP operation map used by the connector transport. No vendor name or endpoint
is hard-coded here; every value comes from the stored manifest.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

CAPABILITY_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
RISK_LEVELS = {"low", "medium", "high", "critical"}
MAX_CAPABILITIES = 32
MAX_DESCRIPTION_LENGTH = 1000
MAX_MANIFEST_BYTES = 32768

# Protocols whose capabilities are sourced from a remote discovery surface
# (MCP tools/list, A2A agent card). OpenAI-compatible capabilities are
# declared in the manifest because the endpoint exposes no catalog pull.
DISCOVERY_PROTOCOLS = {"mcp", "a2a"}
SOURCE_DECLARED = "declared"
SOURCE_DISCOVERED = "discovered"


def endpoint_fingerprint(endpoint_url: str) -> str:
    """Stable SHA-256 fingerprint of an endpoint URL used by discovery records."""

    return hashlib.sha256(str(endpoint_url).encode("utf-8")).hexdigest()

_EMPTY_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}


def _is_closed_object_schema(value: Any) -> bool:
    """Check that a schema describes a closed JSON object type."""
    if not isinstance(value, dict):
        return False
    if value.get("type") != "object":
        return False
    if value.get("additionalProperties") is not False:
        return False
    if not isinstance(value.get("properties", {}), dict):
        return False
    if not isinstance(value.get("required", []), list):
        return False
    return True


def _discovery_context(
    manifest: dict[str, Any], protocol: str, endpoint_url: str | None
) -> tuple[Any, set[str], bool | None]:
    """Return the discovery record, its covered tool names and endpoint match state."""

    block = manifest.get(protocol) if protocol in DISCOVERY_PROTOCOLS else None
    discovery = block.get("discovery") if isinstance(block, dict) else None
    tools: set[str] = set()
    matches: bool | None = None
    if isinstance(discovery, dict):
        raw_tools = discovery.get("tools")
        if isinstance(raw_tools, list):
            tools = {item for item in raw_tools if isinstance(item, str)}
        if endpoint_url:
            matches = discovery.get("endpoint") == endpoint_fingerprint(endpoint_url)
    return discovery, tools, matches


def validate_manifest(
    manifest: Any, protocol: str, endpoint_url: str | None = None
) -> str | None:
    """Return a human-readable validation error, or None when the manifest is valid.

    A missing manifest is accepted for backwards compatibility with legacy
    definitions that only carry the old string-list ``capabilities`` field.

    For ``mcp`` and ``a2a`` protocols every capability must be covered by a
    fresh discovery record for the current endpoint URL. A capability marked
    ``source: "discovered"`` can only be declared by those two protocols.
    """
    if manifest is None:
        return None
    if not isinstance(manifest, dict):
        return "manifest must be a JSON object."
    if not manifest:
        return None

    capabilities = manifest.get("capabilities", [])
    if not isinstance(capabilities, list) or not capabilities:
        return "manifest.capabilities must be a non-empty array."
    if len(capabilities) > MAX_CAPABILITIES:
        return "manifest declares too many capabilities."

    discovery, discovery_tools, discovery_matches = _discovery_context(
        manifest, protocol, endpoint_url
    )

    names: list[str] = []
    for entry in capabilities:
        if not isinstance(entry, dict):
            return "Each manifest capability must be an object."
        name = entry.get("name")
        if not isinstance(name, str) or not CAPABILITY_NAME_PATTERN.fullmatch(name):
            return "Capability names must match ^[a-z0-9][a-z0-9._-]{0,99}$."
        names.append(name)

        source = entry.get("source", SOURCE_DECLARED)
        if source not in {SOURCE_DECLARED, SOURCE_DISCOVERED}:
            return "Capability source must be 'declared' or 'discovered'."
        if protocol in DISCOVERY_PROTOCOLS:
            if discovery is None:
                return (
                    f"Capability '{name}' must be discovered from the remote "
                    "endpoint before registration; call the discover endpoint first."
                )
            if discovery_matches is False:
                return (
                    f"Discovery record for '{name}' is stale: the plugin endpoint "
                    "changed; run discover again."
                )
            if name not in discovery_tools:
                return (
                    f"Capability '{name}' is not covered by the latest discovery "
                    "record; run discover again."
                )
        elif source == SOURCE_DISCOVERED:
            return "Only mcp and a2a capabilities can be marked as discovered."

        description = entry.get("description", "")
        if not isinstance(description, str) or len(description) > MAX_DESCRIPTION_LENGTH:
            return "Capability description must be a string up to 1,000 characters."

        risk_level = entry.get("risk_level", "low")
        if risk_level not in RISK_LEVELS:
            return "Capability risk_level must be low, medium, high or critical."

        timeout_seconds = entry.get("timeout_seconds", 30)
        if (
            not isinstance(timeout_seconds, int)
            or isinstance(timeout_seconds, bool)
            or not 1 <= timeout_seconds <= 300
        ):
            return "Capability timeout_seconds must be an integer between 1 and 300."

        if not _is_closed_object_schema(entry.get("input_schema")):
            return "Capability input_schema must be a closed JSON object."
        if not _is_closed_object_schema(entry.get("output_schema")):
            return "Capability output_schema must be a closed JSON object."

    if len(set(names)) != len(names):
        return "Manifest capability names must be unique."

    transport = manifest.get("http")
    if protocol == "http_json":
        if not isinstance(transport, dict):
            return "HTTP JSON plugins must declare an http transport block."
        base_url = transport.get("base_url")
        if not isinstance(base_url, str) or not base_url.startswith("https://"):
            return "HTTP transport base_url must be an HTTPS URL."
        operations = transport.get("operations")
        if not isinstance(operations, dict):
            return "HTTP transport operations must be an object map."
        missing = [name for name in names if name not in operations]
        if missing:
            return "HTTP operations must cover every declared capability."
        for operation_name, operation in operations.items():
            if not isinstance(operation, dict):
                return f"HTTP operation {operation_name} must be an object."
            method = operation.get("method")
            if method not in {"GET", "POST"}:
                return f"HTTP operation {operation_name} method must be GET or POST."
            path = operation.get("path")
            if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
                return f"HTTP operation {operation_name} path must start with a single '/'."
    elif transport is not None:
        return "Only http_json plugins declare an http transport block."

    return None


def declared_capabilities(plugin: Any) -> list[dict[str, Any]]:
    """Return capability declarations, preferring the manifest over the legacy field."""
    manifest = plugin.manifest if isinstance(getattr(plugin, "manifest", None), dict) else {}
    entries = manifest.get("capabilities")
    if isinstance(entries, list) and entries:
        return [entry for entry in entries if isinstance(entry, dict)]
    legacy = plugin.capabilities if isinstance(getattr(plugin, "capabilities", None), list) else []
    fallback_schema = getattr(plugin, "input_schema", _EMPTY_OBJECT_SCHEMA) or _EMPTY_OBJECT_SCHEMA
    fallback_output = getattr(plugin, "output_schema", _EMPTY_OBJECT_SCHEMA) or _EMPTY_OBJECT_SCHEMA
    timeout = getattr(plugin, "timeout_seconds", 30)
    return [
        {
            "name": name,
            "description": "",
            "risk_level": "low",
            "timeout_seconds": timeout,
            "input_schema": fallback_schema,
            "output_schema": fallback_output,
        }
        for name in legacy
        if isinstance(name, str)
    ]


def capability_definition(plugin: Any, capability_name: str) -> dict[str, Any] | None:
    """Look up a single capability declaration on the plugin."""
    for entry in declared_capabilities(plugin):
        if entry.get("name") == capability_name:
            return entry
    return None


def capability_names(plugin: Any) -> list[str]:
    """Return the ordered declared capability names."""
    return [entry["name"] for entry in declared_capabilities(plugin) if "name" in entry]


def capability_names_from_manifest(manifest: dict[str, Any]) -> list[str]:
    """Return declared capability names from a raw manifest dictionary."""
    entries = manifest.get("capabilities")
    if not isinstance(entries, list):
        return []
    return [
        entry["name"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    ]
