import json


class ToolCallValidationError(ValueError):
    pass


def to_openai_function_tools(plugin_tools):
    """Convert enabled catalog tools to the OpenAI-compatible function schema."""
    result = []
    names = set()
    for tool in plugin_tools:
        if not getattr(tool, "enabled", False):
            continue
        plugin = tool.plugin
        name = f"{plugin.plugin_key}__{tool.name}"
        if len(name) > 64 or name in names:
            raise ToolCallValidationError("Tool names must be unique and at most 64 characters.")
        schema = tool.input_schema
        if (
            not isinstance(schema, dict)
            or schema.get("type") != "object"
            or schema.get("additionalProperties", False) is not False
        ):
            raise ToolCallValidationError("Tool input schema must be a closed object schema.")
        names.add(name)
        description = tool.description or f"Execute {plugin.name}: {tool.name}"
        result.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description[:1024],
                    "parameters": schema,
                },
            }
        )
    return result


def parse_openai_tool_calls(message, offered_tools):
    """Validate model-selected functions before any application dispatch."""
    if not isinstance(message, dict):
        raise ToolCallValidationError("Model message must be an object.")
    offered = {
        item["function"]["name"]: item
        for item in offered_tools
        if isinstance(item, dict)
        and isinstance(item.get("function"), dict)
        and isinstance(item["function"].get("name"), str)
    }
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list) or len(calls) > 16:
        raise ToolCallValidationError("Model returned too many tool calls.")
    parsed = []
    call_ids = set()
    for call in calls:
        if not isinstance(call, dict):
            raise ToolCallValidationError("Model tool call must be an object.")
        function = call.get("function")
        call_id = call.get("id")
        if not isinstance(function, dict) or not isinstance(call_id, str) or not call_id:
            raise ToolCallValidationError("Model tool call is missing required fields.")
        name = function.get("name")
        if name not in offered:
            raise ToolCallValidationError("Model selected a function that was not offered.")
        if call_id in call_ids:
            raise ToolCallValidationError("Model returned a duplicate tool call ID.")
        raw_arguments = function.get("arguments")
        if not isinstance(raw_arguments, str) or len(raw_arguments) > 65536:
            raise ToolCallValidationError("Tool arguments must be a bounded JSON string.")
        try:
            arguments = json.loads(raw_arguments)
        except (TypeError, ValueError) as exc:
            raise ToolCallValidationError("Tool arguments are not valid JSON.") from exc
        if not isinstance(arguments, dict):
            raise ToolCallValidationError("Tool arguments must be a JSON object.")
        call_ids.add(call_id)
        parsed.append(
            {
                "id": call_id,
                "name": name,
                "arguments": arguments,
            }
        )
    return parsed
