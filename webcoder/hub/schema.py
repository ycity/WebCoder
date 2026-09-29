"""Validator for the small JSON Schema subset used by the tool input schemas."""
from __future__ import annotations

import copy
import math
import re


class SchemaError(ValueError):
    pass


def validate(schema: dict, value, path: str = "arguments"):
    """Validate ``value`` and return it with defaults applied. ``null`` counts as absent."""
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise SchemaError(f"{path} must be an object")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unknown = [key for key in value if key not in props]
            if unknown:
                raise SchemaError(f"{path}: unknown field(s) {', '.join(map(repr, unknown))}")
        result = {}
        if schema.get("additionalProperties") is True:  # a free-form object (e.g. CDP params): keep what was sent
            result = {key: copy.deepcopy(item) for key, item in value.items() if key not in props}
        for key, sub in props.items():
            if value.get(key) is not None:
                result[key] = validate(sub, value[key], f"{path}.{key}")
            elif "default" in sub:
                result[key] = copy.deepcopy(sub["default"])
        missing = [key for key in schema.get("required", ()) if key not in result]
        if missing:
            raise SchemaError(f"{path}: missing required field(s) {', '.join(map(repr, missing))}")
        return result
    if kind == "string":
        if not isinstance(value, str):
            raise SchemaError(f"{path} must be a string")
        if len(value) < schema.get("minLength", 0):
            raise SchemaError(f"{path} must not be empty" if schema.get("minLength") == 1
                              else f"{path} is too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise SchemaError(f"{path} is longer than {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise SchemaError(f"{path} does not have the expected format ({schema['pattern']})")
    elif kind == "integer":
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or isinstance(value, float) and not math.isfinite(value) or int(value) != value):  # NaN, Infinity
            raise SchemaError(f"{path} must be an integer")
        value = int(value)
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaError(f"{path} must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaError(f"{path} must be <= {schema['maximum']}")
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise SchemaError(f"{path} must be true or false")
    elif kind == "array":
        if not isinstance(value, list):
            raise SchemaError(f"{path} must be an array")
        if len(value) < schema.get("minItems", 0):
            raise SchemaError(f"{path} needs at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise SchemaError(f"{path} allows at most {schema['maxItems']} item(s)")
        if "items" in schema:
            value = [validate(schema["items"], item, f"{path}[{index}]") for index, item in enumerate(value)]
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaError(f"{path} must be one of {', '.join(map(repr, schema['enum']))}")
    return value
