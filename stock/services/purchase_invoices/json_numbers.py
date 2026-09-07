"""Exact Decimal JSON numbers for invoice APIs and persisted replay bodies."""

import json
from decimal import Decimal


def dumps(value):
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Non-finite JSON number")
        return format(value.normalize(), "f")
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(json.dumps(k) + ":" + dumps(value[k]) for k in sorted(value))
            + "}"
        )
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(dumps(v) for v in value) + "]"
    if isinstance(value, float):
        raise TypeError("Invoice accounting requires Decimal, not float")
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


class InvalidNumbers(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("Invalid JSON numbers")


class _InvalidNumber:
    pass


def _number(token):
    if "e" in token.lower():
        return _InvalidNumber()
    return Decimal(token)


def _constant(token):
    return _InvalidNumber()


def loads(value):
    def unique_object(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field: " + key)
            result[key] = item
        return result

    result = json.loads(
        value,
        parse_float=_number,
        parse_constant=_constant,
        object_pairs_hook=unique_object,
    )
    errors = {}

    def validate(node, path):
        if isinstance(node, _InvalidNumber):
            errors[path or "body"] = [
                "Use a plain, finite number without exponent notation."
            ]
        elif isinstance(node, dict):
            for name, child in node.items():
                validate(child, f"{path}.{name}" if path else name)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                validate(child, f"{path}.{index}" if path else str(index))

    validate(result, "")
    if errors:
        raise InvalidNumbers(errors)
    return result
