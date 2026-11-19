# ruff: noqa: I001
import re

from core.mcp_connector import ToolExecutionError, discover_mcp_tools
from core.models import PluginDefinition, PluginTool


MAX_SYNCED_TOOLS = 256
TOOL_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,99}")


def sync_mcp_tools(plugin, context):
    """Import the MCP tool descriptors into the plugin definition.

    Newly imported tools stay disabled until an operator reviews them, while
    existing tools only have their description and schema refreshed.
    """

    if plugin.connector_type != PluginDefinition.ConnectorType.MCP:
        raise ToolExecutionError("MCP tool discovery requires an MCP connector.")
    descriptors = discover_mcp_tools(context)
    if len(descriptors) > MAX_SYNCED_TOOLS:
        raise ToolExecutionError("MCP server exposed too many tools to import.")

    created = []
    updated = []
    unchanged = []
    for descriptor in descriptors:
        name = descriptor.get("name")
        if not isinstance(name, str) or not TOOL_NAME.fullmatch(name):
            raise ToolExecutionError("MCP server exposed an unsupported tool name.")
        schema = descriptor.get("input_schema")
        if schema is None:
            schema = {}
        if not isinstance(schema, dict):
            raise ToolExecutionError("MCP server exposed an invalid tool schema.")
        description = str(descriptor.get("description", ""))[:2000]

        existing = PluginTool.objects.filter(plugin=plugin, name=name).first()
        if existing is None:
            PluginTool.objects.create(
                plugin=plugin,
                name=name,
                description=description,
                input_schema=schema,
                enabled=False,
            )
            created.append(name)
            continue

        changed_fields = []
        if existing.description != description:
            existing.description = description
            changed_fields.append("description")
        if existing.input_schema != schema:
            existing.input_schema = schema
            changed_fields.append("input_schema")
        if changed_fields:
            existing.save(update_fields=changed_fields)
            updated.append(name)
        else:
            unchanged.append(name)

    return {
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
    }
