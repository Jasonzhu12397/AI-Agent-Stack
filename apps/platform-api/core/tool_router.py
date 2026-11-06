from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class ToolExecutionError(Exception):
    """A safe, user-facing tool execution failure."""


class ToolApprovalRequired(ToolExecutionError):
    """A policy gate that must not be converted into an ordinary tool result."""


@dataclass(frozen=True)
class ToolContext:
    tenant_id: str
    project_id: str
    installation_id: str
    plugin_key: str
    plugin_version: str
    tool_name: str
    connection_id: str | None
    connection_config: dict[str, Any]
    connection_auth_type: str
    credential_ref: str
    plugin_manifest: dict[str, Any]


Handler = Callable[[dict[str, Any], ToolContext], Any]


def _matches_type(value: Any, expected: str) -> bool:
    checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: (
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and (isinstance(item, int) or math.isfinite(item))
        ),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    check = checks.get(expected)
    return check(value) if check else False


def _json_values_equal(left: Any, right: Any) -> bool:
    try:
        return json.dumps(
            left,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ) == json.dumps(
            right,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        return False


def validate_schema(value: Any, schema: dict[str, Any], path: str = "$", _depth: int = 0) -> None:
    """Validate the registry's bounded JSON Schema subset recursively."""
    if _depth > 16:
        raise ToolExecutionError("Tool arguments exceed the maximum schema nesting depth.")
    if not isinstance(schema, dict):
        raise ToolExecutionError("Tool schema is malformed.")
    expected = schema.get("type")
    if not isinstance(expected, str):
        raise ToolExecutionError("Tool schema is malformed.")
    if expected in {"integer", "number"} and isinstance(value, float) and not math.isfinite(value):
        raise ToolExecutionError(f"{path} must be finite.")
    if expected == "object" and not isinstance(value, dict):
        raise ToolExecutionError(f"{path} must be an object.")
    if not _matches_type(value, expected):
        raise ToolExecutionError(f"{path} has an invalid value type.")
    if "enum" in schema:
        allowed_values = schema["enum"]
        if not isinstance(allowed_values, list) or not any(
            _json_values_equal(value, allowed) for allowed in allowed_values
        ):
            raise ToolExecutionError(f"{path} is not an allowed value.")
    if expected == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise ToolExecutionError("Tool schema is malformed.")
        if schema.get("additionalProperties", False) is not False:
            raise ToolExecutionError("Tool schemas must reject additional properties.")
        missing = [key for key in required if not isinstance(key, str) or key not in value]
        if missing:
            raise ToolExecutionError(f"Missing required tool arguments: {', '.join(missing)}.")
        unknown = sorted(set(value) - set(properties))
        if unknown:
            raise ToolExecutionError(f"Unknown tool arguments: {', '.join(unknown)}.")
        for key, item in value.items():
            validate_schema(item, properties[key], f"{path}.{key}", _depth + 1)
        return
    if expected == "array":
        minimum = schema.get("minItems", 0)
        maximum = schema.get("maxItems", 100)
        item_schema = schema.get("items")
        if (
            not isinstance(minimum, int) or isinstance(minimum, bool)
            or not isinstance(maximum, int) or isinstance(maximum, bool)
            or minimum < 0 or maximum < minimum
            or len(value) < minimum or len(value) > maximum
            or not isinstance(item_schema, dict)
        ):
            raise ToolExecutionError(f"{path} has an invalid array shape.")
        for index, item in enumerate(value):
            try:
                validate_schema(item, item_schema, f"{path}[{index}]", _depth + 1)
            except ToolExecutionError as exc:
                if "invalid value type" in str(exc):
                    raise ToolExecutionError(f"{path} contains an invalid item.") from exc
                raise
        return
    if expected == "string":
        minimum = schema.get("minLength", 0)
        maximum = schema.get("maxLength", 65536)
        if (not isinstance(minimum, int) or isinstance(minimum, bool)
            or not isinstance(maximum, int) or isinstance(maximum, bool)
            or minimum < 0 or maximum < minimum
            or len(value) < minimum or len(value) > maximum):
            raise ToolExecutionError(f"{path} has an invalid length.")
    if expected in {"integer", "number"}:
        if isinstance(value, float) and not math.isfinite(value):
            raise ToolExecutionError(f"{path} must be finite.")
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolExecutionError(f"{path} is below its minimum.")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolExecutionError(f"{path} exceeds its maximum.")

class ToolRouter:
    """Policy-enforcing dispatcher; connector transports must register trusted handlers."""

    def __init__(self) -> None:
        self._handlers: dict[tuple[str, ...], Handler] = {}

    def register(
        self,
        plugin_key: str,
        tool_name: str,
        handler: Handler,
        plugin_version: str = "*",
    ) -> None:
        if not plugin_key or not tool_name or not callable(handler) or not plugin_version:
            raise ValueError("A plugin key, version, tool name and callable handler are required.")
        key = (
            (plugin_key, tool_name)
            if plugin_version == "*"
            else (plugin_key, plugin_version, tool_name)
        )
        if key in self._handlers:
            raise ValueError("A handler is already registered for this connector version and tool.")
        self._handlers[key] = handler

    def has_handler(
        self,
        plugin_key: str,
        tool_name: str,
        plugin_version: str = "*",
    ) -> bool:
        if plugin_version == "*":
            return (
                (plugin_key, tool_name) in self._handlers
                or (plugin_key, "*", tool_name) in self._handlers
            )
        return (plugin_key, plugin_version, tool_name) in self._handlers

    def execute(
        self,
        *,
        installation: Any,
        tool: Any,
        arguments: dict[str, Any],
        granted_permissions: list[str] | None = None,
        connection: Any | None = None,
        approved: bool = False,
    ) -> dict[str, Any]:
        plugin = installation.plugin
        if installation.status != "installed":
            raise ToolExecutionError("Connector installation is not active.")
        if not plugin.enabled or plugin.review_status != "approved":
            raise ToolExecutionError("Connector is not approved and enabled.")
        if installation.plugin_version != plugin.version:
            raise ToolExecutionError("Installed connector version does not match the catalog.")
        if tool.plugin_id != plugin.id or not tool.enabled:
            raise ToolExecutionError("Tool is not enabled for this connector.")

        required = set(tool.required_permissions or [])
        granted = set(installation.granted_permissions or [])
        supplied = set(granted_permissions or [])
        if not required.issubset(granted) or not required.issubset(supplied):
            raise ToolExecutionError("Required connector permissions have not been granted.")

        if tool.risk_level in {"high", "critical"} and not approved:
            raise ToolApprovalRequired(
                "This tool requires explicit approval before execution."
            )
        if connection is not None:
            if connection.installation_id != installation.id or connection.status != "healthy":
                raise ToolExecutionError("Connector connection is not healthy or is out of scope.")

        validate_schema(arguments, tool.input_schema)
        try:
            encoded_arguments = json.dumps(
                arguments,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ToolExecutionError("Tool arguments are not valid JSON.") from exc
        if len(encoded_arguments) > 65536:
            raise ToolExecutionError("Tool arguments exceed 64 KiB.")
        handler = (
            self._handlers.get((plugin.plugin_key, plugin.version, tool.name))
            or self._handlers.get((plugin.plugin_key, tool.name))
            or self._handlers.get((plugin.plugin_key, "*", tool.name))
        )
        if handler is None:
            raise ToolExecutionError("No trusted executor is registered for this connector tool.")

        context = ToolContext(
            tenant_id=str(installation.tenant_id),
            project_id=str(installation.project_id),
            installation_id=str(installation.id),
            plugin_key=plugin.plugin_key,
            plugin_version=plugin.version,
            tool_name=tool.name,
            connection_id=str(connection.id) if connection else None,
            connection_config=dict(connection.config) if connection else {},
            connection_auth_type=connection.auth_type if connection else "none",
            credential_ref=connection.credential_ref if connection else "",
            plugin_manifest=(
                dict(getattr(plugin, "manifest", {}))
                if isinstance(getattr(plugin, "manifest", {}), dict)
                else {}
            ),
        )
        try:
            result = handler(dict(arguments), context)
            validate_schema(result, tool.output_schema)
            encoded_result = json.dumps(
                result,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded_result) > 65536:
                raise ToolExecutionError("Tool result exceeds 64 KiB.")
        except ToolExecutionError:
            raise
        except Exception as exc:
            raise ToolExecutionError("Connector tool execution failed.") from exc
        return {"ok": True, "tool": tool.name, "result": result}
