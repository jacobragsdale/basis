"""JSON text as a database's JSON type takes it, such as SQL Server's `json`: an object or an array at the top, numbers
in plain notation, and nothing a JSON column refuses. `dumps` writes it from Python values, and `problem` says why a
text is no such value (`docs/spikes/sqlserver-json.md`):

- the top is an object or an array, never a bare scalar, `null` or empty text (SQL Server's error 13609);
- a number is written in plain notation, never `1e-07`, and none has more than 38 digits before its point (error
  1007); NaN and the infinities are refused, as JSON has none;
- nesting is at most 128 levels deep (error 13645), and a key at most 7,998 bytes in UTF-8 (error 13646);
- a date or datetime is written as its ISO text, and a datetime must carry its zone.

Pure: it reads and writes nothing, so job code may call it, as `etl.json_text`."""

import json
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime

from basis.core.errors import BasisError

MAX_DEPTH = 128
MAX_KEY_BYTES = 7998
MAX_WHOLE_DIGITS = 38


class JsonTextError(BasisError, ValueError):
    """A value that a JSON column cannot hold; the message gives the fix."""


def dumps(value: object) -> str:
    """`value`, a mapping or a sequence of JSON's values, as JSON text a JSON column takes, without spaces."""
    if not isinstance(value, Mapping | list | tuple):
        msg = f"a JSON column holds an object or an array, not a {type(value).__name__}: give a dict or a list"
        raise JsonTextError(msg)
    return _text(value, 1)


def _text(value: object, depth: int) -> str:  # noqa: PLR0911 -- one case per JSON value
    if depth > MAX_DEPTH:
        msg = f"the value nests deeper than the {MAX_DEPTH} levels a JSON column holds: flatten it"
        raise JsonTextError(msg)
    match value:
        case None:
            return "null"
        case bool():
            return "true" if value else "false"
        case int():
            return str(value)
        case float():
            return plain(value)
        case str():
            return json.dumps(value, ensure_ascii=False)
        case datetime():
            if value.tzinfo is None:
                msg = f"{value} has no zone, so its instant is unknown: give it one, as datetime(..., tzinfo=UTC)"
                raise JsonTextError(msg)
            return json.dumps(value.isoformat())
        case date():
            return json.dumps(value.isoformat())
        case Mapping():
            return "{" + ",".join(f"{_key(key)}:{_text(item, depth + 1)}" for key, item in value.items()) + "}"
        case Sequence():
            return "[" + ",".join(_text(item, depth + 1) for item in value) + "]"
        case _:
            msg = f"a JSON value is an object, an array, text, a number, a bool, a date or null, not a {type(value).__name__}: convert it first"
            raise JsonTextError(msg)


def _key(key: object) -> str:
    if not isinstance(key, str):
        msg = f"a JSON object's keys are text, not {key!r}: make it a str"
        raise JsonTextError(msg)
    if len(key.encode()) > MAX_KEY_BYTES:
        msg = f"a key of {len(key.encode())} bytes is longer than the {MAX_KEY_BYTES} a JSON column holds: shorten it"
        raise JsonTextError(msg)
    return json.dumps(key, ensure_ascii=False)


def plain(value: float) -> str:
    """A finite float in plain notation, from its shortest round-trip digits: 1e-07 as `0.0000001`, 1.5e+20 as
    `150000000000000000000`."""
    if not math.isfinite(value):
        msg = f"{value} is no JSON number: give a finite number, or null for none"
        raise JsonTextError(msg)
    mantissa, _, exponent = repr(value).partition("e")
    sign = "-" if mantissa.startswith("-") else ""
    whole, _, fraction = mantissa.lstrip("-").partition(".")
    digits, point = whole + fraction, len(whole) + int(exponent or "0")
    if point <= 0:
        text = "0." + "0" * -point + digits.rstrip("0") if digits.strip("0") else "0"
    elif point >= len(digits):
        text = digits + "0" * (point - len(digits))
    else:
        text = f"{digits[:point]}.{digits[point:]}"
    text = text.lstrip("0") or "0"
    text = "0" + text if text.startswith(".") else text
    text = text.removesuffix(".0")
    if len(text.partition(".")[0]) > MAX_WHOLE_DIGITS:
        msg = f"{value} has more than the {MAX_WHOLE_DIGITS} digits before its point a JSON column's number holds: store it as text"
        raise JsonTextError(msg)
    return sign + text if text != "0" else text


def problem(text: str) -> str | None:
    """Why `text` is no value a JSON column holds, or None where it is one."""
    try:
        found: object = json.loads(text, parse_constant=_constant)
    except (json.JSONDecodeError, JsonTextError) as error:
        return f"not JSON ({error})"
    if not isinstance(found, dict | list):
        return f"JSON {type(found).__name__}, where a JSON column holds an object or an array"
    try:
        _check(found, 1)
    except JsonTextError as error:
        return str(error).partition(":")[0]
    return None


def _constant(name: str) -> object:
    msg = f"{name} is no JSON number"
    raise JsonTextError(msg)


def _check(value: object, depth: int) -> None:
    if depth > MAX_DEPTH:
        msg = f"nested deeper than the {MAX_DEPTH} levels a JSON column holds: flatten it"
        raise JsonTextError(msg)
    if isinstance(value, dict):
        for key, item in value.items():
            _key(key)
            _check(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check(item, depth + 1)
