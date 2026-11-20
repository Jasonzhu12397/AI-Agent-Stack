"""Schema-validated model/tool output handling.

Model output is treated as untrusted data. Callers must parse it as a JSON
object and validate it against an explicit schema before acting on it.
"""

from __future__ import annotations

import json
import re
from typing import Any

TYPE_MAP = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
}


class ModelOutputError(ValueError):
    """Model output is not valid JSON or does not match its schema."""


def parse_json_object(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n", "", cleaned)
        cleaned = re.sub(r"\n```$", "", cleaned)
    try:
        document = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ModelOutputError(f"model output is not valid JSON: {exc.msg}") from exc
    if not isinstance(document, dict):
        raise ModelOutputError("model output must be a JSON object")
    return document


def validate_schema(value: Any, schema: dict, path: str = "$") -> None:
    if not isinstance(schema, dict):
        raise ModelOutputError(f"{path}: schema must be an object")
    expected = schema.get("type")
    if expected:
        python_type = TYPE_MAP.get(expected)
        if python_type is None:
            raise ModelOutputError(f"{path}: unsupported schema type {expected}")
        if expected in {"integer", "number"} and isinstance(value, bool):
            raise ModelOutputError(f"{path}: expected {expected}")
        if not isinstance(value, python_type):
            raise ModelOutputError(f"{path}: expected {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise ModelOutputError(f"{path}: value is not in the allowed enum")
    if isinstance(value, dict):
        for required in schema.get("required", []):
            if required not in value:
                raise ModelOutputError(f"{path}: missing required property {required}")
        properties = schema.get("properties", {})
        for key, child in value.items():
            if key in properties:
                validate_schema(child, properties[key], f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                raise ModelOutputError(f"{path}: unexpected property {key}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            validate_schema(item, schema["items"], f"{path}[{index}]")


TOOL_CALL_SCHEMA = {
    "type": "object",
    "required": ["tool", "arguments"],
    "additionalProperties": False,
    "properties": {
        "tool": {
            "type": "string",
            "enum": ["read_file", "apply_patch", "run_tests", "git_diff", "git_commit"],
        },
        "arguments": {"type": "object"},
    },
}


def parse_tool_call(text: str) -> dict:
    document = parse_json_object(text)
    validate_schema(document, TOOL_CALL_SCHEMA)
    return document
