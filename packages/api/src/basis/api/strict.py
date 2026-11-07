"""How basis reads a request: strictly, so that no key it does not know is ignored and no value is coerced into its type.
A misspelled field, or a value of the wrong kind, fails at the boundary with a 422 `invalid-request` problem that names
it, before the handler runs.

**A JSON body** is read by basis itself, from its bytes, in pydantic's strict mode on JSON, before FastAPI sees it. Each
value has its own type in JSON: a string for a `str`, a number without a fraction for an `int`, `true` or `false` for a
`bool`, an array for a list. A type JSON has none for is a string of its standard form: a date as `"2026-11-06"`, a UUID
as `"019a5c3e-..."`. Two rules go further than pydantic's strict mode:

- A `Decimal` is a JSON string of its digits, as `"1000000.00"`, never a number: pydantic reads a JSON number through a
  float, so `12345678901234567890.123456789` would arrive as `12345678901234567000`, and `1000000.00` without its scale.
- A `datetime` carries its offset, as `"2026-11-06T14:30:00Z"`: a time without one could be any zone's.

A key no model declares is refused at every depth, whatever a model's own config says. The body is JSON only when the
request says so, as `Content-Type: application/json`, and a route takes at most one body, its whole JSON.

**Query and path parameters** arrive as text, so basis reads each by its type's grammar before FastAPI converts it,
and FastAPI's conversion of what the grammar accepts is exact:

| Type | The text it accepts |
|---|---|
| `str` | any |
| `int` | digits, with a minus sign only before them and no leading zero: `10`, `-3`, `0` |
| `Decimal` | an `int`'s digits, and a point and digits after it: `0.045`, `100`, `-2.50`; no exponent |
| `float` | a JSON number, `1.5` or `2e-3`, that is finite |
| `bool` | `true` or `false` |
| `date` | `2026-11-06` |
| `datetime` | `2026-11-06T14:30:00Z`, or with `+01:00`, and up to six digits of a second's fraction: never without an offset |
| `UUID` | 32 hex digits in groups of 8-4-4-4-12, `019a5c3e-0000-7000-8000-000000000001` |
| a `Literal` or an `Enum` | one of its values exactly, as a `str` or an `int` reads |

A type may be optional, `T | None`, and a query parameter may be a list or a tuple of one of these, whose values are
the key given once each: `?security=A&security=B`. A key the route does not declare is refused, and so is a key given
twice for one value. A route declares its query parameters one by one, or as one model with `Query()`; a path
parameter's type is the handler's, so a path never names a converter, as `{n:int}` would."""

import math
import re
import types
import typing
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal, TypeAliasType, get_args, get_origin
from uuid import UUID

from fastapi import params
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant
from pydantic import BaseModel, JsonValue, RootModel, TypeAdapter, ValidationError

from basis.api.errors import ServiceError

type ErrorEntry = dict[str, object]
"""One error as pydantic and FastAPI give them: `type`, `loc`, `msg` and `input`, which `errors.problem` turns into a
`FieldError`."""

_DIGITS = r"-?(?:0|[1-9][0-9]*)"


@dataclass(frozen=True)
class Text:
    """A type's grammar as text, and how a refusal says what it expects."""

    expected: str
    """What the type's text is, as `Input should be ...` finishes it."""
    accepts: Callable[[str], bool]


def _matching(pattern: str, *, finite: bool = False) -> Callable[[str], bool]:
    compiled = re.compile(pattern)
    return lambda text: compiled.fullmatch(text) is not None and (not finite or math.isfinite(float(text)))


_ANY = Text("text", lambda _: True)
_SCALARS: tuple[tuple[type, Text], ...] = (
    # in this order: a bool is an int, and a datetime a date
    (bool, Text("true or false", _matching("true|false"))),
    (int, Text("an integer, as 10 or -3: digits, with a minus sign only before them and no leading zero", _matching(_DIGITS))),
    (float, Text("a finite number, as 1.5 or 2e-3, written as JSON writes one", _matching(_DIGITS + r"(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?", finite=True))),
    (Decimal, Text("a decimal, as 0.045 or 100: digits, a point and digits, with a minus sign only before them and no exponent", _matching(_DIGITS + r"(?:\.[0-9]+)?"))),
    (
        datetime,
        Text(
            "a time with its offset, as 2026-11-06T14:30:00Z or 2026-11-06T09:30:00-05:00",
            _matching(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})"),
        ),
    ),
    (date, Text("a date, as 2026-11-06", _matching(r"[0-9]{4}-[0-9]{2}-[0-9]{2}"))),
    (UUID, Text("a UUID, as 019a5c3e-0000-7000-8000-000000000001: hex digits in groups of 8-4-4-4-12", _matching(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}"))),
    (str, _ANY),
)


@dataclass(frozen=True)
class Parameter:
    key: str
    """Its name in the query string, or in the path."""
    reads: Text
    many: bool
    """A list or a tuple, whose values are the key given once each."""


@dataclass(frozen=True)
class Shape:
    """What a route reads from a request, as its handler's parameters, and its dependencies', declare it."""

    path: tuple[Parameter, ...]
    query: tuple[Parameter, ...]
    body: TypeAdapter[object] | None
    """The body's type, read in strict mode from JSON, or None for a route that takes no body."""


def shape(path: str, handler: Callable[..., object], name: str) -> Shape:
    """What the route `name` reads, after checking that basis can read each of its parameters strictly."""
    found = list(_flat(get_dependant(path=path, call=handler)))
    readable_path = tuple(_parameter(name, "path", field.alias, field.field_info.annotation) for dependant in found for field in dependant.path_params)
    query: list[Parameter] = []
    for dependant in found:
        for field in dependant.query_params:
            annotation = field.field_info.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):  # a query model: its fields are the keys
                query.extend(_parameter(name, "query", inner.alias or key, inner.annotation) for key, inner in annotation.model_fields.items())
            else:
                query.append(_parameter(name, "query", field.alias, annotation))
    bodies = [field for dependant in found for field in dependant.body_params]
    if len(bodies) > 1:
        msg = f"{name} takes {len(bodies)} bodies, {', '.join(field.name for field in bodies)}: a route takes one JSON body, so declare one model that holds them all"
        raise ServiceError(msg)
    body: TypeAdapter[object] | None = None
    if bodies:
        info = bodies[0].field_info
        if isinstance(info, params.Body) and info.embed:
            msg = f"{name}'s body {bodies[0].name} is embedded in an object of its own: drop embed=True, so the body is the model's JSON itself"
            raise ServiceError(msg)
        if isinstance(info, params.Form | params.File):
            msg = f"{name} takes a form: a route's body is JSON, so declare a model for it"
            raise ServiceError(msg)
        if info.annotation is None:
            msg = f"{name}'s body {bodies[0].name} has no type: annotate it with the model it reads"
            raise ServiceError(msg)
        body = TypeAdapter(info.annotation)
    return Shape(readable_path, tuple(query), body)


def _flat(dependant: Dependant) -> Iterator[Dependant]:
    yield dependant
    for sub in dependant.dependencies:
        yield from _flat(sub)


def _parameter(route: str, where: Literal["path", "query"], key: str, annotation: object) -> Parameter:
    """`key`'s parameter: one of its type's values, or, in a query, a list or a tuple of them."""
    kind = _bare(annotation)
    is_many = where == "query" and get_origin(kind) in {list, tuple}
    if is_many:
        arguments = [argument for argument in get_args(kind) if argument is not Ellipsis]
        kind = _bare(arguments[0]) if len(set(arguments)) == 1 else None
    found = text(kind) if kind is not None else None
    if found is None:
        listing = "str, int, Decimal, float, bool, date, datetime, UUID, a Literal or an Enum" + (", or a list or tuple of one" if where == "query" else "")
        msg = f"{route}'s {where} parameter {key} is {_named(annotation)}, which basis cannot read strictly from text: give it {listing}"
        raise ServiceError(msg)
    return Parameter(key, found, is_many)


def text(kind: object) -> Text | None:
    """How a value of `kind` reads from text, or None where basis cannot read it strictly."""
    origin = get_origin(kind)
    if origin is Literal:
        return _choices(get_args(kind))
    if isinstance(kind, type) and issubclass(kind, Enum):
        return _choices([member.value for member in kind])
    if not isinstance(kind, type) or origin is not None:
        return None
    return next((found for scalar, found in _SCALARS if issubclass(kind, scalar)), None)


def _choices(values: Sequence[object]) -> Text | None:
    texts: list[str] = []
    for value in values:
        if isinstance(value, bool):
            texts.append("true" if value else "false")
        elif isinstance(value, int | str):
            texts.append(str(value))
        else:
            return None
    allowed = frozenset(texts)
    return Text(f"one of {', '.join(texts)}", allowed.__contains__)


def _bare(annotation: object) -> object:
    """`annotation` without what does not change how its text reads: `Annotated`'s metadata, a `type` alias, and
    `| None`, since a parameter that is absent is the one way to give none."""
    while True:
        if isinstance(annotation, TypeAliasType):
            annotation = annotation.__value__
        elif get_origin(annotation) is Annotated:
            annotation = get_args(annotation)[0]
        elif get_origin(annotation) in {typing.Union, types.UnionType} and types.NoneType in get_args(annotation):
            rest = [argument for argument in get_args(annotation) if argument is not types.NoneType]
            if len(rest) != 1:
                return annotation
            annotation = rest[0]
        else:
            return annotation


def _named(annotation: object) -> str:
    found: object = getattr(annotation, "__qualname__", None)
    return found if isinstance(found, str) else repr(annotation)


def path_errors(found: Shape, given: Mapping[str, object]) -> list[ErrorEntry]:
    return [_refused("path", parameter, value) for parameter in found.path if isinstance(value := given.get(parameter.key), str) and not parameter.reads.accepts(value)]


def query_errors(found: Shape, items: Iterable[tuple[str, str]]) -> list[ErrorEntry]:
    """What is wrong with a query string's `items`, each key and value in order: keys it does not declare, a key given
    twice for one value, and text its type does not read."""
    declared = {parameter.key: parameter for parameter in found.query}
    errors: list[ErrorEntry] = []
    counted = Counter[str]()
    for key, value in items:
        counted[key] += 1
        parameter = declared.get(key)
        if parameter is None:
            if counted[key] == 1:
                takes = f"this route takes {', '.join(declared)}" if declared else "this route takes none"
                errors.append({"type": "extra_forbidden", "loc": ("query", key), "msg": f"Unknown query parameter: {takes}", "input": value})
        elif not parameter.reads.accepts(value):
            errors.append(_refused("query", parameter, value))
    errors.extend(
        {"type": "too_many", "loc": ("query", key), "msg": f"Given {count} times: give it once", "input": key}
        for key, count in counted.items()
        if count > 1 and (parameter := declared.get(key)) is not None and not parameter.many
    )
    return errors


def _refused(where: str, parameter: Parameter, value: str) -> ErrorEntry:
    return {"type": "strict_text", "loc": (where, parameter.key), "msg": f"Input should be {parameter.reads.expected}", "input": value}


_JSON = re.compile(r"application/(?:[\w.+-]+\+)?json", re.IGNORECASE)
_TREE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def body(adapter: TypeAdapter[object], raw: bytes, content_type: str | None) -> tuple[object, list[ErrorEntry]]:
    """The body `raw`, read as `adapter`'s type in strict mode, or what is wrong with it."""
    if content_type is None or _JSON.fullmatch(content_type.partition(";")[0].strip()) is None:
        message = "The body is not JSON: send it with Content-Type: application/json" if content_type is None else f"The body is {content_type}, not JSON: send it as application/json"
        return None, [{"type": "content_type", "loc": ("body",), "msg": message, "input": content_type}]
    try:
        value = adapter.validate_json(raw, strict=True, extra="forbid")
    except ValidationError as error:
        return None, [_entry(entry) for entry in error.errors(include_url=False)]
    tree = _TREE.validate_json(raw)  # parsed again, to tell a decimal given as a string from one given as a number
    return value, list(_inexact(value, tree, ("body",)))


def _entry(entry: Mapping[str, object]) -> ErrorEntry:
    location = entry.get("loc")
    found: ErrorEntry = {"type": entry.get("type"), "loc": ("body", *(location if isinstance(location, tuple) else ())), "msg": entry.get("msg"), "input": entry.get("input")}
    if entry.get("type") == "json_invalid":
        context = entry.get("ctx")
        found["msg"] = "The body is not JSON"
        found["ctx"] = {"error": context.get("error")} if isinstance(context, Mapping) else {}
    return found


def _inexact(value: object, tree: JsonValue, at: tuple[str | int, ...]) -> Iterator[ErrorEntry]:
    """Each decimal in `value` that its JSON `tree` gave as a number, and each time without an offset, walking the two
    together: a value the strict mode read is where its JSON was, by each model's field alias, list index or key."""
    match value:
        case Decimal() if not isinstance(tree, str):
            yield {
                "type": "decimal_string",
                "loc": at,
                "msg": 'Input should be a string of the decimal, as "0.045": a JSON number reaches a decimal through a float, which can change it',
                "input": tree,
            }
        case datetime() if value.tzinfo is None:
            yield {"type": "timezone_aware", "loc": at, "msg": "Input should be a time with its offset, as 2026-11-06T14:30:00Z", "input": tree}
        case RootModel():
            yield from _inexact(value.root, tree, at)
        case BaseModel() if isinstance(tree, dict):
            for name, field in type(value).model_fields.items():
                key = field.alias or name
                if key in tree:
                    yield from _inexact(getattr(value, name), tree[key], (*at, key))
        case list() | tuple() if isinstance(tree, list):
            for index, (item, node) in enumerate(zip(value, tree, strict=False)):
                yield from _inexact(item, node, (*at, index))
        case dict() if isinstance(tree, dict):
            for key, item in value.items():
                if (node := tree.get(str(key))) is not None:
                    yield from _inexact(item, node, (*at, str(key)))
        case _:
            return
