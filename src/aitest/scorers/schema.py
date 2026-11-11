"""A strict, dependency-free subset of JSON Schema.

The scorer does not pretend to implement all of JSON Schema. Configuration is
validated up front and any unsupported keyword is rejected with an explicit
error, so a schema can never be silently under-enforced.

Supported: type, enum, const, properties, required, additionalProperties,
minProperties, maxProperties, items, minItems, maxItems, uniqueItems,
minLength, maxLength, pattern, minimum, maximum, exclusiveMinimum,
exclusiveMaximum, multipleOf, allOf, anyOf, oneOf, not. Metadata keywords
($schema, $id, title, description, default, examples) are accepted and ignored.
"""

from __future__ import annotations

import math
import re
from typing import Any

METADATA_KEYWORDS = {
    "$schema",
    "$id",
    "title",
    "description",
    "default",
    "examples",
    "deprecated",
    "readOnly",
    "writeOnly",
}

SUPPORTED_KEYWORDS = METADATA_KEYWORDS | {
    "type",
    "enum",
    "const",
    "properties",
    "required",
    "additionalProperties",
    "minProperties",
    "maxProperties",
    "items",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minLength",
    "maxLength",
    "pattern",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "allOf",
    "anyOf",
    "oneOf",
    "not",
}

TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}


class SchemaConfigError(ValueError):
    """Raised when the configured schema is invalid or unsupported."""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_schema(schema: Any, path: str = "$schema") -> None:
    if not isinstance(schema, dict):
        raise SchemaConfigError(f"{path} must be an object")
    unknown = sorted(set(schema) - SUPPORTED_KEYWORDS)
    if unknown:
        raise SchemaConfigError(
            f"{path} uses unsupported JSON Schema keyword(s): {', '.join(unknown)}"
        )

    if "type" in schema:
        declared = schema["type"]
        values = declared if isinstance(declared, list) else [declared]
        if not values or any(not isinstance(item, str) or item not in TYPES for item in values):
            raise SchemaConfigError(f"{path}.type must be one of {sorted(TYPES)} or a list of them")

    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise SchemaConfigError(f"{path}.enum must be a non-empty array")

    if "required" in schema:
        required = schema["required"]
        if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
            raise SchemaConfigError(f"{path}.required must be an array of strings")

    for key in ("minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties"):
        if key in schema and (
            not isinstance(schema[key], int) or isinstance(schema[key], bool) or schema[key] < 0
        ):
            raise SchemaConfigError(f"{path}.{key} must be a non-negative integer")

    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
        if key in schema and not _is_number(schema[key]):
            raise SchemaConfigError(f"{path}.{key} must be a number")
    if "multipleOf" in schema and schema["multipleOf"] <= 0:
        raise SchemaConfigError(f"{path}.multipleOf must be greater than zero")

    if "pattern" in schema:
        if not isinstance(schema["pattern"], str):
            raise SchemaConfigError(f"{path}.pattern must be a string")
        try:
            re.compile(schema["pattern"])
        except re.error as exc:
            raise SchemaConfigError(f"{path}.pattern is not a valid regular expression: {exc}") from exc

    if "properties" in schema:
        properties = schema["properties"]
        if not isinstance(properties, dict):
            raise SchemaConfigError(f"{path}.properties must be an object")
        for name, child in properties.items():
            validate_schema(child, f"{path}.properties.{name}")

    if "additionalProperties" in schema:
        extra = schema["additionalProperties"]
        if not isinstance(extra, (bool, dict)):
            raise SchemaConfigError(f"{path}.additionalProperties must be a boolean or a schema")
        if isinstance(extra, dict):
            validate_schema(extra, f"{path}.additionalProperties")

    if "items" in schema:
        validate_schema(schema["items"], f"{path}.items")

    for key in ("allOf", "anyOf", "oneOf"):
        if key in schema:
            branches = schema[key]
            if not isinstance(branches, list) or not branches:
                raise SchemaConfigError(f"{path}.{key} must be a non-empty array of schemas")
            for index, child in enumerate(branches):
                validate_schema(child, f"{path}.{key}[{index}]")

    if "not" in schema:
        validate_schema(schema["not"], f"{path}.not")


def validate_instance(instance: Any, schema: dict, path: str = "$") -> list[dict]:
    """Return a list of schema violations; an empty list means valid."""

    errors: list[dict] = []
    _check(instance, schema, path, errors)
    return errors


def _error(errors: list[dict], path: str, keyword: str, message: str) -> None:
    errors.append({"path": path, "keyword": keyword, "message": message})


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return _is_number(value)
    return _type_name(value) == expected


def _check(value: Any, schema: dict, path: str, errors: list[dict]) -> None:
    if "type" in schema:
        declared = schema["type"]
        values = declared if isinstance(declared, list) else [declared]
        if not any(_matches_type(value, item) for item in values):
            _error(errors, path, "type", f"expected {' or '.join(values)}, got {_type_name(value)}")
            return

    if "enum" in schema and value not in schema["enum"]:
        _error(errors, path, "enum", f"value {value!r} is not one of {schema['enum']!r}")
    if "const" in schema and value != schema["const"]:
        _error(errors, path, "const", f"value {value!r} does not equal {schema['const']!r}")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                _error(errors, f"{path}.{name}", "required", f"required property '{name}' is missing")
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            _error(errors, path, "minProperties", f"expected at least {schema['minProperties']} properties")
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            _error(errors, path, "maxProperties", f"expected at most {schema['maxProperties']} properties")
        for name, item in value.items():
            if name in properties:
                _check(item, properties[name], f"{path}.{name}", errors)
            elif "additionalProperties" in schema:
                extra = schema["additionalProperties"]
                if extra is False:
                    _error(errors, f"{path}.{name}", "additionalProperties", f"property '{name}' is not allowed")
                elif isinstance(extra, dict):
                    _check(item, extra, f"{path}.{name}", errors)

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            _error(errors, path, "minItems", f"expected at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            _error(errors, path, "maxItems", f"expected at most {schema['maxItems']} items")
        if schema.get("uniqueItems"):
            seen: list[str] = []
            for item in value:
                marker = repr(item)
                if marker in seen:
                    _error(errors, path, "uniqueItems", "array items must be unique")
                    break
                seen.append(marker)
        if "items" in schema:
            for index, item in enumerate(value):
                _check(item, schema["items"], f"{path}[{index}]", errors)

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            _error(errors, path, "minLength", f"expected length >= {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            _error(errors, path, "maxLength", f"expected length <= {schema['maxLength']}")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            _error(errors, path, "pattern", f"does not match pattern {schema['pattern']!r}")

    if _is_number(value):
        if "minimum" in schema and value < schema["minimum"]:
            _error(errors, path, "minimum", f"expected >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            _error(errors, path, "maximum", f"expected <= {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            _error(errors, path, "exclusiveMinimum", f"expected > {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            _error(errors, path, "exclusiveMaximum", f"expected < {schema['exclusiveMaximum']}")
        if "multipleOf" in schema:
            quotient = value / schema["multipleOf"]
            if not math.isclose(quotient, round(quotient), rel_tol=1e-9, abs_tol=1e-9):
                _error(errors, path, "multipleOf", f"expected a multiple of {schema['multipleOf']}")

    if "allOf" in schema:
        for child in schema["allOf"]:
            _check(value, child, path, errors)

    if "anyOf" in schema:
        if not any(not validate_instance(value, child, path) for child in schema["anyOf"]):
            _error(errors, path, "anyOf", "value does not satisfy any schema in anyOf")

    if "oneOf" in schema:
        matches = sum(1 for child in schema["oneOf"] if not validate_instance(value, child, path))
        if matches != 1:
            _error(errors, path, "oneOf", f"value satisfies {matches} schemas; exactly one is required")

    if "not" in schema and not validate_instance(value, schema["not"], path):
        _error(errors, path, "not", "value must not satisfy the 'not' schema")
