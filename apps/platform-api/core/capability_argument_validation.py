"""Shared, data-driven JSON Schema subset validator for capability arguments.

This module backs the argument validation gate used by every protocol adapter
(``http_json``, ``mcp``, ``a2a``, ``openai_compatible``) through the capability
router. The validator is a pure function: it takes a JSON value and a schema
dictionary and raises :class:`ArgumentValidationError` on the first violation.
No database, transport or plugin state is touched here.

Supported schema keywords:

- ``type``: ``string``, ``number``, ``integer``, ``boolean``, ``object``,
  ``array``, ``null``. Booleans are never accepted as ``integer`` or
  ``number``.
- ``properties``, ``required`` and ``additionalProperties``. Unknown object
  properties are rejected only when ``additionalProperties`` is explicitly
  ``false``.
- ``enum``: the value must equal one of the listed entries.
- ``items``, ``minItems``, ``maxItems`` for arrays.
- ``minLength``, ``maxLength`` for strings.
- ``minimum``, ``maximum`` for numbers and integers.
- ``$ref``: local pointers only, resolved against ``#/definitions/<name>`` or
  ``#/$defs/<name>`` on the root schema. External references, fragment-less
  pointers and unresolvable targets are rejected with
  :data:`CAPABILITY_SCHEMA_REF_UNSUPPORTED` instead of being sent upstream.

Error codes:

- :data:`CAPABILITY_ARGUMENTS_INVALID` reuses the router-level
  ``CAPABILITY_INVOCATION_FAILED`` token: a value violates the schema. The
  router records the invocation failure with that code; no synonym code is
  introduced for ordinary argument violations.
- :data:`CAPABILITY_SCHEMA_REF_UNSUPPORTED`: the schema itself uses a
  reference the local validator cannot resolve.
"""

from __future__ import annotations

import math
from typing import Any

# Reused verbatim from the router code set; do not introduce a synonym token
# for ordinary argument violations.
CAPABILITY_ARGUMENTS_INVALID = "CAPABILITY_INVOCATION_FAILED"
CAPABILITY_SCHEMA_REF_UNSUPPORTED = "CAPABILITY_SCHEMA_REF_UNSUPPORTED"

_MAX_DEPTH = 16
_MAX_ARRAY_ITEMS = 100
_MAX_STRING_LENGTH = 65536


class ArgumentValidationError(Exception):
    """A capability argument failed schema validation; ``code`` is stable."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


_TYPE_CHECKS: dict[str, Any] = {
    "object": lambda item: isinstance(item, dict),
    "array": lambda item: isinstance(item, list),
    "string": lambda item: isinstance(item, str),
    "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
    "number": _is_number,
    "boolean": lambda item: isinstance(item, bool),
    "null": lambda item: item is None,
}


def _json_equal(left: Any, right: Any) -> bool:
    """Compare two JSON values by their canonical serialized form."""

    try:
        import json

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


def _resolve_reference(ref: Any, root: dict[str, Any]) -> dict[str, Any]:
    """Resolve a local ``$ref`` pointer; reject anything else explicitly."""

    if not isinstance(ref, str):
        raise ArgumentValidationError(
            CAPABILITY_SCHEMA_REF_UNSUPPORTED, "Schema $ref must be a string."
        )
    if not ref.startswith("#/"):
        raise ArgumentValidationError(
            CAPABILITY_SCHEMA_REF_UNSUPPORTED,
            "Only local $ref pointers are supported; remote references are rejected.",
        )
    parts = ref[2:].split("/")
    if len(parts) != 2 or parts[0] not in {"definitions", "$defs"}:
        raise ArgumentValidationError(
            CAPABILITY_SCHEMA_REF_UNSUPPORTED,
            "Unsupported $ref pointer shape; expected #/definitions/<name>.",
        )
    definitions = root.get(parts[0])
    if not isinstance(definitions, dict):
        raise ArgumentValidationError(
            CAPABILITY_SCHEMA_REF_UNSUPPORTED,
            "$ref definitions block is missing from the schema.",
        )
    target = definitions.get(parts[1])
    if not isinstance(target, dict):
        raise ArgumentValidationError(
            CAPABILITY_SCHEMA_REF_UNSUPPORTED,
            "$ref target could not be resolved in the definitions block.",
        )
    return target


def _validate_object(
    value: dict[str, Any],
    schema: dict[str, Any],
    path: str,
    root: dict[str, Any],
    depth: int,
) -> None:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ArgumentValidationError(
            CAPABILITY_ARGUMENTS_INVALID, f"{path} declares an invalid properties map."
        )
    required = schema.get("required", [])
    if not isinstance(required, list):
        raise ArgumentValidationError(
            CAPABILITY_ARGUMENTS_INVALID, f"{path} declares an invalid required list."
        )
    missing = [key for key in required if isinstance(key, str) and key not in value]
    if missing:
        raise ArgumentValidationError(
            CAPABILITY_ARGUMENTS_INVALID,
            f"Missing required arguments: {', '.join(missing)}.",
        )
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(value) - set(properties))
        if unknown:
            raise ArgumentValidationError(
                CAPABILITY_ARGUMENTS_INVALID,
                f"Unknown arguments: {', '.join(unknown)}.",
            )
    for key, item in value.items():
        child_schema = properties.get(key)
        if child_schema is None:
            # Property not declared and additionalProperties allowed; skip it
            # rather than failing the whole call on an undeclared passthrough.
            continue
        validate_arguments(item, child_schema, f"{path}.{key}", root, depth + 1)


def _validate_array(
    value: list[Any],
    schema: dict[str, Any],
    path: str,
    root: dict[str, Any],
    depth: int,
) -> None:
    minimum = schema.get("minItems", 0)
    maximum = schema.get("maxItems", _MAX_ARRAY_ITEMS)
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
        raise ArgumentValidationError(
            CAPABILITY_ARGUMENTS_INVALID, f"{path} has an invalid array shape."
        )
    for index, item in enumerate(value):
        validate_arguments(item, item_schema, f"{path}[{index}]", root, depth + 1)


def validate_arguments(
    value: Any,
    schema: dict[str, Any],
    path: str = "$",
    root: dict[str, Any] | None = None,
    depth: int = 0,
) -> None:
    """Validate ``value`` against the bounded schema subset.

    Raises :class:`ArgumentValidationError` when the value violates the schema
    or the schema uses an unsupported reference.
    """

    if depth > _MAX_DEPTH:
        raise ArgumentValidationError(
            CAPABILITY_ARGUMENTS_INVALID, "Arguments exceed the maximum nesting depth."
        )
    if not isinstance(schema, dict):
        raise ArgumentValidationError(CAPABILITY_ARGUMENTS_INVALID, f"{path} schema is malformed.")

    schema = dict(schema)
    root_schema = root if root is not None else schema
    if "$ref" in schema:
        schema = _resolve_reference(schema["$ref"], root_schema)

    expected = schema.get("type")
    if not isinstance(expected, str):
        raise ArgumentValidationError(CAPABILITY_ARGUMENTS_INVALID, f"{path} schema is malformed.")

    check = _TYPE_CHECKS.get(expected)
    if check is None or not check(value):
        raise ArgumentValidationError(
            CAPABILITY_ARGUMENTS_INVALID, f"{path} has an invalid value type."
        )

    if "enum" in schema:
        allowed = schema["enum"]
        if not isinstance(allowed, list) or not any(_json_equal(value, entry) for entry in allowed):
            raise ArgumentValidationError(
                CAPABILITY_ARGUMENTS_INVALID, f"{path} is not an allowed value."
            )

    if expected == "object":
        _validate_object(value, schema, path, root_schema, depth)
        return
    if expected == "array":
        _validate_array(value, schema, path, root_schema, depth)
        return
    if expected == "string":
        minimum = schema.get("minLength", 0)
        maximum = schema.get("maxLength", _MAX_STRING_LENGTH)
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
            raise ArgumentValidationError(
                CAPABILITY_ARGUMENTS_INVALID, f"{path} has an invalid length."
            )
        return
    if expected in {"integer", "number"}:
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and (
            not isinstance(minimum, (int, float)) or isinstance(minimum, bool) or value < minimum
        ):
            raise ArgumentValidationError(
                CAPABILITY_ARGUMENTS_INVALID, f"{path} is below the minimum value."
            )
        if maximum is not None and (
            not isinstance(maximum, (int, float)) or isinstance(maximum, bool) or value > maximum
        ):
            raise ArgumentValidationError(
                CAPABILITY_ARGUMENTS_INVALID, f"{path} is above the maximum value."
            )
