# ruff: noqa: I001
import pytest

from core.capability_argument_validation import (
    CAPABILITY_ARGUMENTS_INVALID,
    CAPABILITY_SCHEMA_REF_UNSUPPORTED,
    ArgumentValidationError,
    validate_arguments,
)


def _expect_invalid(schema, value, code=CAPABILITY_ARGUMENTS_INVALID):
    with pytest.raises(ArgumentValidationError) as exc:
        validate_arguments(value, schema)
    assert exc.value.code == code


OBJECT_SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "units": {"type": "string", "enum": ["celsius", "fahrenheit"]},
    },
    "required": ["city"],
    "additionalProperties": False,
}


def test_valid_object_passes():
    validate_arguments({"city": "Austin", "units": "celsius"}, OBJECT_SCHEMA)


def test_missing_required_argument_rejected():
    _expect_invalid(OBJECT_SCHEMA, {"units": "celsius"})


def test_unknown_property_rejected_when_closed():
    _expect_invalid(OBJECT_SCHEMA, {"city": "Austin", "extra": 1})


def test_unknown_property_allowed_when_not_closed():
    schema = {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    }
    validate_arguments({"city": "Austin", "extra": 1}, schema)


def test_type_mismatch_string_field():
    _expect_invalid(OBJECT_SCHEMA, {"city": 42})


def test_type_mismatch_integer_field():
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
        "additionalProperties": False,
    }
    _expect_invalid(schema, {"count": "three"})


def test_type_mismatch_boolean_field():
    schema = {
        "type": "object",
        "properties": {"enabled": {"type": "boolean"}},
        "required": ["enabled"],
        "additionalProperties": False,
    }
    _expect_invalid(schema, {"enabled": "yes"})


def test_type_mismatch_object_field():
    schema = {
        "type": "object",
        "properties": {"meta": {"type": "object"}},
        "required": ["meta"],
        "additionalProperties": False,
    }
    _expect_invalid(schema, {"meta": ["not", "an", "object"]})


def test_type_mismatch_array_field():
    schema = {
        "type": "object",
        "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
        "required": ["tags"],
        "additionalProperties": False,
    }
    _expect_invalid(schema, {"tags": "not-an-array"})


def test_type_mismatch_null_field():
    schema = {
        "type": "object",
        "properties": {"note": {"type": "null"}},
        "required": ["note"],
        "additionalProperties": False,
    }
    _expect_invalid(schema, {"note": "present"})


def test_enum_out_of_range_rejected():
    _expect_invalid(OBJECT_SCHEMA, {"city": "Austin", "units": "kelvin"})


def test_enum_allowed_value_passes():
    validate_arguments({"city": "Austin", "units": "fahrenheit"}, OBJECT_SCHEMA)


def test_nested_object_validation():
    schema = {
        "type": "object",
        "properties": {
            "location": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
                "additionalProperties": False,
            }
        },
        "required": ["location"],
        "additionalProperties": False,
    }
    validate_arguments({"location": {"city": "Austin"}}, schema)
    _expect_invalid(schema, {"location": {"city": 42}})
    _expect_invalid(schema, {"location": {}})


def test_array_items_validation():
    schema = {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            }
        },
        "required": ["tags"],
        "additionalProperties": False,
    }
    validate_arguments({"tags": ["a", "b"]}, schema)
    _expect_invalid(schema, {"tags": []})
    _expect_invalid(schema, {"tags": ["a", 2]})


def test_bool_is_not_integer_or_number():
    integer_schema = {
        "type": "object",
        "properties": {"n": {"type": "integer"}},
        "required": ["n"],
        "additionalProperties": False,
    }
    number_schema = {
        "type": "object",
        "properties": {"n": {"type": "number"}},
        "required": ["n"],
        "additionalProperties": False,
    }
    _expect_invalid(integer_schema, {"n": True})
    _expect_invalid(number_schema, {"n": False})


def test_number_minimum_maximum():
    schema = {
        "type": "object",
        "properties": {"ratio": {"type": "number", "minimum": 0, "maximum": 2}},
        "required": ["ratio"],
        "additionalProperties": False,
    }
    validate_arguments({"ratio": 1.5}, schema)
    _expect_invalid(schema, {"ratio": -0.1})
    _expect_invalid(schema, {"ratio": 2.1})


def test_local_definition_ref_resolves():
    schema = {
        "type": "object",
        "properties": {"location": {"$ref": "#/definitions/Location"}},
        "required": ["location"],
        "additionalProperties": False,
        "definitions": {
            "Location": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
                "additionalProperties": False,
            }
        },
    }
    validate_arguments({"location": {"city": "Austin"}}, schema)
    _expect_invalid(schema, {"location": {"city": 42}})


def test_external_ref_rejected_with_stable_code():
    schema = {
        "type": "object",
        "properties": {"location": {"$ref": "https://example.com/schema#Location"}},
        "required": ["location"],
        "additionalProperties": False,
    }
    _expect_invalid(
        schema,
        {"location": {"city": "Austin"}},
        code=CAPABILITY_SCHEMA_REF_UNSUPPORTED,
    )


def test_unresolvable_local_ref_rejected_with_stable_code():
    schema = {
        "type": "object",
        "properties": {"location": {"$ref": "#/definitions/Missing"}},
        "required": ["location"],
        "additionalProperties": False,
        "definitions": {},
    }
    _expect_invalid(
        schema,
        {"location": {"city": "Austin"}},
        code=CAPABILITY_SCHEMA_REF_UNSUPPORTED,
    )


def test_non_object_schema_root_rejected():
    _expect_invalid({"type": "array"}, {"not": "an array"})
