from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class ToolExecutionError(Exception):
    """A safe, user-facing tool execution failure."""


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


def validate_schema(value: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Validate the deliberately small JSON Schema subset accepted by the registry."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ToolExecutionError("Tool schema must be an object schema.")
    if not isinstance(value, dict):
        raise ToolExecutionError(f"{path} must be an object.")

    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise ToolExecutionError("Tool schema is malformed.")
    if schema.get("additionalProperties", False) is not False:
        raise ToolExecutionError("Tool schemas must reject additional properties.")

    missing = [key for key in required if key not in value]
    if missing:
        raise ToolExecutionError(f"Missing required tool arguments: {', '.join(missing)}.")

    unknown = sorted(set(value) - set(properties))
    if unknown:
        raise ToolExecutionError(f"Unknown tool arguments: {', '.join(unknown)}.")

    for key, item in value.items():
        definition = properties[key]
        if not isinstance(definition, dict) or not _matches_type(item, definition.get("type", "")):
            raise ToolExecutionError(f"Invalid value for tool argument '{key}'.")
        if isinstance(item, str):
            if (
                len(item) < int(definition.get("minLength", 0))
                or len(item) > int(definition.get("maxLength", 65536))
            ):
                raise ToolExecutionError(f"Tool argument '{key}' has an invalid length.")
            if "enum" in definition and item not in definition["enum"]:
                raise ToolExecutionError(f"Invalid value for tool argument '{key}'.")
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            if isinstance(item, float) and not math.isfinite(item):
                raise ToolExecutionError(f"Tool argument '{key}' must be finite.")
            if "minimum" in definition and item < definition["minimum"]:
                raise ToolExecutionError(f"Tool argument '{key}' is below its minimum.")
            if "maximum" in definition and item > definition["maximum"]:
                raise ToolExecutionError(f"Tool argument '{key}' exceeds its maximum.")
        if isinstance(item, dict) and definition.get("type") == "object":
            validate_schema(item, definition, f"{path}.{key}")
        if isinstance(item, list):
            minimum = definition.get("minItems", 0)
            maximum = definition.get("maxItems", 100)
            item_schema = definition.get("items")
            if (
                not isinstance(minimum, int)
                or isinstance(minimum, bool)
                or not isinstance(maximum, int)
                or isinstance(maximum, bool)
                or minimum < 0
                or maximum < minimum
                or len(item) < minimum
                or len(item) > maximum
                or not isinstance(item_schema, dict)
            ):
                raise ToolExecutionError(f"Tool argument '{key}' has an invalid array shape.")
            for element in item:
                if not _matches_type(element, item_schema.get("type", "")):
                    raise ToolExecutionError(f"Tool argument '{key}' contains an invalid item.")
                if isinstance(element, dict):
                    validate_schema(element, item_schema, f"{path}.{key}")


class ToolRouter:
    """Policy-enforcing dispatcher; connector transports must register trusted handlers."""

    def __init__(self) -> None:
        self._handlers: dict[tuple[str, str], Handler] = {}

    def register(self, plugin_key: str, tool_name: str, handler: Handler) -> None:
        if not plugin_key or not tool_name or not callable(handler):
            raise ValueError("A plugin key, tool name and callable handler are required.")
        key = (plugin_key, tool_name)
        if key in self._handlers:
            raise ValueError("A handler is already registered for this tool.")
        self._handlers[key] = handler

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
            raise ToolExecutionError("This tool requires explicit approval before execution.")
        if connection is not None:
            if connection.installation_id != installation.id or connection.status != "healthy":
                raise ToolExecutionError("Connector connection is not healthy or is out of scope.")

        validate_schema(arguments, tool.input_schema)
        handler = self._handlers.get((plugin.plugin_key, tool.name))
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
        )
        try:
            result = handler(dict(arguments), context)
            validate_schema(result, tool.output_schema)
        except ToolExecutionError:
            raise
        except Exception as exc:
            raise ToolExecutionError("Connector tool execution failed.") from exc
        return {"ok": True, "tool": tool.name, "result": result}
