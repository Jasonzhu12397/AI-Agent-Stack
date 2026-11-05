import json

import pytest

from core.llm_tools import ToolCallValidationError, parse_openai_tool_calls


def _offered(schema):
    return [
        {
            "type": "function",
            "function": {
                "name": "sample__tool",
                "parameters": schema,
            },
        }
    ]


def _message(arguments):
    return {
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "sample__tool",
                    "arguments": json.dumps(arguments),
                },
            }
        ]
    }


def test_accepts_valid_nested_arguments():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "options"],
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 20},
            "options": {
                "type": "object",
                "additionalProperties": False,
                "required": ["enabled"],
                "properties": {"enabled": {"type": "boolean"}},
            },
        },
    }

    result = parse_openai_tool_calls(
        _message({"name": "deploy", "options": {"enabled": True}}),
        _offered(schema),
    )

    assert result[0]["arguments"]["options"]["enabled"] is True


def test_rejects_unknown_top_level_argument():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"name": {"type": "string"}},
    }

    with pytest.raises(ToolCallValidationError, match="missing or unknown"):
        parse_openai_tool_calls(_message({"admin": True}), _offered(schema))


def test_rejects_missing_required_argument():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["name"],
        "properties": {"name": {"type": "string"}},
    }

    with pytest.raises(ToolCallValidationError, match="missing or unknown"):
        parse_openai_tool_calls(_message({}), _offered(schema))


def test_rejects_boolean_as_integer():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"replicas": {"type": "integer"}},
    }

    with pytest.raises(ToolCallValidationError, match="Invalid value"):
        parse_openai_tool_calls(_message({"replicas": True}), _offered(schema))


def test_rejects_nested_unknown_argument():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "config": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"region": {"type": "string"}},
            }
        },
    }

    with pytest.raises(ToolCallValidationError, match="missing or unknown"):
        parse_openai_tool_calls(
            _message({"config": {"region": "us-east", "unsafe": True}}),
            _offered(schema),
        )


def test_rejects_array_over_limit():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "nodes": {
                "type": "array",
                "minItems": 1,
                "maxItems": 2,
                "items": {"type": "string"},
            }
        },
    }

    with pytest.raises(ToolCallValidationError, match="Invalid array"):
        parse_openai_tool_calls(
            _message({"nodes": ["a", "b", "c"]}),
            _offered(schema),
        )


def test_rejects_non_finite_json_number():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"ratio": {"type": "number"}},
    }
    message = _message({})
    message["tool_calls"][0]["function"]["arguments"] = '{"ratio":NaN}'

    with pytest.raises(ToolCallValidationError, match="valid JSON"):
        parse_openai_tool_calls(message, _offered(schema))


def test_rejects_duplicate_offered_tool_names():
    offered = _offered({"type": "object", "additionalProperties": False})
    offered.extend(_offered({"type": "object", "additionalProperties": False}))

    with pytest.raises(ToolCallValidationError, match="unique"):
        parse_openai_tool_calls({"tool_calls": []}, offered)


def test_rejects_non_object_schema_values():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"settings": {"type": "object", "properties": []}},
    }

    with pytest.raises(ToolCallValidationError, match="closed and valid"):
        parse_openai_tool_calls(
            _message({"settings": {}}),
            _offered(schema),
        )
