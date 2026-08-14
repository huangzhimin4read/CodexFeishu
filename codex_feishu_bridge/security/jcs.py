"""RFC 8785 JSON Canonicalization Scheme for bridge hash inputs.

The implementation accepts the JSON data model only. Integers outside the
IEEE-754 safe range, non-finite floats, non-string object keys, and lone UTF-16
surrogates are rejected so hashes cannot depend on a lossy conversion.
"""

from __future__ import annotations

import math
from hashlib import sha256
from typing import Any


class CanonicalizationError(ValueError):
    """Raised when a value is outside the interoperable JSON/JCS domain."""


_MAX_SAFE_INTEGER = 9_007_199_254_740_991


def _validate_unicode(value: str) -> None:
    for char in value:
        codepoint = ord(char)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise CanonicalizationError("lone UTF-16 surrogate is not valid JCS")


def _quote(value: str) -> str:
    _validate_unicode(value)
    pieces = ['"']
    escapes = {
        '"': '\\"',
        "\\": "\\\\",
        "\b": "\\b",
        "\t": "\\t",
        "\n": "\\n",
        "\f": "\\f",
        "\r": "\\r",
    }
    for char in value:
        if char in escapes:
            pieces.append(escapes[char])
        elif ord(char) <= 0x1F:
            pieces.append(f"\\u{ord(char):04x}")
        else:
            pieces.append(char)
    pieces.append('"')
    return "".join(pieces)


def _number(value: int | float) -> str:
    if isinstance(value, bool):
        raise CanonicalizationError("booleans are not numbers")
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INTEGER:
            raise CanonicalizationError("integer is outside the IEEE-754 safe range")
        return str(value)
    if not math.isfinite(value):
        raise CanonicalizationError("non-finite JSON number")
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    raw = repr(abs(value)).lower()
    if "e" in raw:
        coefficient, raw_exponent = raw.split("e", 1)
        exponent = int(raw_exponent)
    else:
        coefficient, exponent = raw, 0

    if coefficient.endswith(".0"):
        coefficient = coefficient[:-2]

    if "." in coefficient:
        integer, fraction = coefficient.split(".", 1)
    else:
        integer, fraction = coefficient, ""
    digits = (integer + fraction).lstrip("0")
    if not digits:
        return "0"
    decimal_point = len(integer) + exponent - (len(integer + fraction) - len((integer + fraction).lstrip("0")))

    if 0 < decimal_point <= 21:
        if decimal_point >= len(digits):
            body = digits + ("0" * (decimal_point - len(digits)))
        else:
            body = digits[:decimal_point] + "." + digits[decimal_point:]
    elif -6 < decimal_point <= 0:
        body = "0." + ("0" * (-decimal_point)) + digits
    else:
        mantissa = digits[0]
        if len(digits) > 1:
            mantissa += "." + digits[1:]
        scientific_exponent = decimal_point - 1
        exponent_text = f"+{scientific_exponent}" if scientific_exponent >= 0 else str(scientific_exponent)
        body = mantissa + "e" + exponent_text
    return sign + body


def _utf16_sort_key(value: str) -> bytes:
    _validate_unicode(value)
    return value.encode("utf-16-be")


def canonicalize_text(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, (int, float)):
        return _number(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonicalize_text(item) for item in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise CanonicalizationError("JCS object keys must be strings")
        ordered = sorted(value, key=_utf16_sort_key)
        return "{" + ",".join(
            _quote(key) + ":" + canonicalize_text(value[key]) for key in ordered
        ) + "}"
    raise CanonicalizationError(f"unsupported JSON value: {type(value).__name__}")


def canonicalize(value: Any) -> bytes:
    return canonicalize_text(value).encode("utf-8")


def digest(value: Any) -> str:
    return sha256(canonicalize(value)).hexdigest()
