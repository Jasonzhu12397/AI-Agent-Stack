import json
import math


class ToolCallValidationError(ValueError):
    pass


def _validate_value(value, schema, path="$", depth=0, budget=None):
    if budget is None:
        budget = [4096]
    budget[0] -= 1
    if budget[0] < 0 or depth > 32:
        raise ToolCallValidationError("Tool arguments exceed validation limits.")
    if not isinstance(schema, dict):
        raise ToolCallValidationError("Tool input schema is malformed.")

    expected = schema.get("type")
    checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: (
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and (not isinstance(item, float) or math.isfinite(item))
        ),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    check = checks.get(expected)
    if check is None or not check(value):
        raise ToolCallValidationError("Invalid value for tool argument at " + path + ".")

    if "enum" in schema:
        choices = schema["enum"]
        if not isinstance(choices, list) or value not in choices:
            raise ToolCallValidationError("Value is not allowed for tool argument at " + path + ".")

    if expected == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if (
            not isinstance(properties, dict)
            or not isinstance(required, list)
            or schema.get("additionalProperties", False) is not False
            or any(not isinstance(key, str) for key in required)
        ):
            raise ToolCallValidationError("Tool object schema must be closed and valid.")
        missing = [key for key in required if key not in value]
        unknown = sorted(set(value) - set(properties))
        if missing or unknown:
            raise ToolCallValidationError("Tool arguments contain missing or unknown fields at " + path + ".")
        for key, item in value.items():
            _validate_value(item, properties[key], path + "." + key, depth + 1, budget)

    elif expected == "array":
        minimum = schema.get("minItems", 0)
        maximum = schema.get("maxItems", 100)
        item_schema = schema.get("items")
        if (
            not isinstance(minimum, int)
            or isinstance(minimum, bool)
            or not isinstance(maximum, int)
            or isinstance(maximum, bool)
            or minimum < 0
            or maximum < minimum
            or len(value) < minimum
            or len(value) > maximum
            or not isinstance(item_schema, dict)
        ):
            raise ToolCallValidationError("Invalid array arguments at " + path + ".")
        for index, item in enumerate(value):
            _validate_value(item, item_schema, path + "[" + str(index) + "]", depth + 1, budget)

    elif expected == "string":
        minimum = schema.get("minLength", 0)
        maximum = schema.get("maxLength", 65536)
        if (
            not isinstance(minimum, int)
            or isinstance(minimum, bool)
            or not isinstance(maximum, int)
            or isinstance(maximum, bool)
            or minimum < 0
            or maximum < minimum
            or len(value) < minimum
            or len(value) > maximum
        ):
            raise ToolCallValidationError("Invalid string length at " + path + ".")

    elif expected in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolCallValidationError("Number is below its minimum at " + path + ".")
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolCallValidationError("Number exceeds its maximum at " + path + ".")


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
    """Validate model-selected functions and arguments before application dispatch."""
    if not isinstance(message, dict):
        raise ToolCallValidationError("Model message must be an object.")
    if not isinstance(offered_tools, list):
        raise ToolCallValidationError("Offered tools must be a list.")

    offered = {}
    for item in offered_tools:
        if not isinstance(item, dict) or not isinstance(item.get("function"), dict):
            raise ToolCallValidationError("Offered tool definition is malformed.")
        function = item["function"]
        name = function.get("name")
        schema = function.get("parameters")
        if not isinstance(name, str) or not name or name in offered:
            raise ToolCallValidationError("Offered tool names must be unique non-empty strings.")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ToolCallValidationError("Offered tool schema must be an object schema.")
        offered[name] = schema

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
            arguments = json.loads(raw_arguments, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (TypeError, ValueError) as exc:
            raise ToolCallValidationError("Tool arguments are not valid JSON.") from exc
        if not isinstance(arguments, dict):
            raise ToolCallValidationError("Tool arguments must be a JSON object.")
        _validate_value(arguments, offered[name])
        call_ids.add(call_id)
        parsed.append(
            {
                "id": call_id,
                "name": name,
                "arguments": arguments,
            }
        )
    return parsed
