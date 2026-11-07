"""How basis reads a request: strictly, so that no key it does not know is ignored and no value is coerced into its type.
A misspelled field, or a value of the wrong kind, fails at the boundary with a 422 `invalid-request` problem that names
it, before the handler runs.

**A JSON body** is read by basis itself, from its bytes, in pydantic's strict mode on JSON, before FastAPI sees it. Each
value has its own type in JSON: a string for a `str`, a number without a fraction for an `int`, `true` or `false` for a
`bool`, an array for a list. A type JSON has none for is a string of its standard form: a date as `"2026-11-06"`, a UUID
as `"019a5c3e-..."`. Two rules go further than pydantic's strict mode:

- A `Decimal` is a JSON string of its digits, as `"1000000.00"`, never a number: pydantic reads a JSON number through a
  float, so `12345678901234567890.123456789` would arrive as `12345678901234567000`, and `1000000.00` without its scale.
  Its digits read as a query's decimal does, below, so `"1e3"`, `"+1"` and `" 1"` are refused; `DECIMAL` is the
  grammar, which the OpenAPI document gives as the string's pattern.
- A `datetime` carries its offset, as `"2026-11-06T14:30:00Z"`: a time without one could be any zone's.

A key no model declares is refused at every depth, whatever a model's own config says. The body is JSON only when the
request says so, as `Content-Type: application/json`; a route takes at most one body, its whole JSON, declared in its
handler's own parameters, and a route that declares none refuses one. The body is validated once, by basis: FastAPI
gets the value it made, which it does not validate again, so a validator never runs twice.

To check decimals and times against the JSON, basis matches each model's field to the JSON by one key, its plain
`alias` or else its name, so a request model gives none other: no `validation_alias` of its own, `AliasChoices` or
`AliasPath`, and no `populate_by_name`, `validate_by_name` or `validate_by_alias=False`. A set's items have no order to
match to the JSON's, so a set in a body holds scalars, a set of decimals included, never models or tuples of decimals or
times. Where a body's type holds no decimal and no time, that check, and the second parse it needs, are skipped.

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

import dataclasses
import math
import re
import types
import typing
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, MutableSet, Sequence
from collections.abc import Set as AbstractSet
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
from pydantic.fields import FieldInfo

from basis.api.errors import ServiceError

type ErrorEntry = dict[str, object]
"""One error as pydantic and FastAPI give them: `type`, `loc`, `msg` and `input`, which `errors.problem` turns into a
`FieldError`."""

_DIGITS = r"-?(?:0|[1-9][0-9]*)"
DECIMAL = _DIGITS + r"(?:\.[0-9]+)?"
"""A decimal's text, in a body's string or a query's: digits, a point and digits after it, with a minus sign only
before them and no exponent."""
_IS_DECIMAL = re.compile(DECIMAL)


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
    (Decimal, Text("a decimal, as 0.045 or 100: digits, a point and digits, with a minus sign only before them and no exponent", _matching(DECIMAL))),
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
class Body:
    adapter: TypeAdapter[object]
    """The body's type, read in strict mode from JSON."""
    precise: bool
    """Whether it can hold a decimal or a time, which `body` then checks against the JSON."""


@dataclass(frozen=True)
class Shape:
    """What a route reads from a request, as its handler's parameters, and its dependencies', declare it."""

    path: tuple[Parameter, ...]
    query: tuple[Parameter, ...]
    body: Body | None
    """None for a route that takes no body."""


def shape(path: str, handler: Callable[..., object], name: str) -> Shape:
    """What the route `name` reads, after checking that basis can read each of its parameters strictly."""
    found = list(_flat(get_dependant(path=path, call=handler)))
    if any(dependant.background_tasks_param_name is not None for dependant in found):
        msg = (
            f"{name} takes BackgroundTasks, whose work runs after the response, outside the route's deadline and transaction, where a failure reaches nobody:"
            " do the work in the handler; a call to another service that changes its state waits for the outbox basis is to have"
        )
        raise ServiceError(msg)
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
    if bodies and not found[0].body_params:
        msg = f"a dependency of {name}'s takes the body {bodies[0].name}: take it in {name}'s own parameters, and pass it on"
        raise ServiceError(msg)
    body: Body | None = None
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
        body = Body(TypeAdapter(info.annotation), _precise(name, info.annotation, frozenset()))
    return Shape(readable_path, tuple(query), body)


def _precise(route: str, kind: object, within: frozenset[type]) -> bool:
    """Whether a body of `kind` can hold a decimal or a time, after refusing what basis cannot match to the JSON to
    check them: a model's field read by any key but one, and a set of anything but scalars that holds one. `within`
    holds the models being walked, for one that refers to itself."""
    kind = bare(kind)
    if (origin := get_origin(kind)) is not None:
        return _precise_of(route, kind, origin, within)
    if not isinstance(kind, type) or kind in within:
        return False
    if issubclass(kind, Decimal | datetime):
        return True
    if issubclass(kind, BaseModel):
        _one_key(route, kind, kind.model_config, kind.model_fields)
        held = [_precise(route, field.annotation, within | {kind}) for field in kind.model_fields.values()]  # every field's, so each model's keys are checked
        return any(held)
    if dataclasses.is_dataclass(kind):
        fields: object = getattr(kind, "__pydantic_fields__", None)
        if isinstance(fields, dict):
            config: object = getattr(kind, "__pydantic_config__", None)
            settled = {str(key): item for key, item in config.items()} if isinstance(config, Mapping) else {}
            _one_key(route, kind, settled, {str(name): info for name, info in fields.items() if isinstance(info, FieldInfo)})
        hints = typing.get_type_hints(kind, include_extras=True)
        held = [_precise(route, hints[found.name], within | {kind}) for found in dataclasses.fields(kind)]
        return any(held)
    return False


def _precise_of(route: str, kind: object, origin: object, within: frozenset[type]) -> bool:
    """`_precise` for a generic type: a union, a list, a tuple, a mapping or a set, by each of its types; a `Literal`
    holds values, which are neither."""
    if origin is Literal:
        return False
    arguments = [argument for argument in get_args(kind) if argument is not Ellipsis]
    if origin in {set, frozenset, AbstractSet, MutableSet}:
        items = bare(arguments[0]) if arguments else object
        holds = _precise(route, items, within)
        if holds and not (isinstance(items, type) and issubclass(items, Decimal | datetime)):
            msg = f"{route}'s body holds a {named(kind)}, whose items basis cannot match to the JSON's to check their decimals and times: hold a tuple or a list instead"
            raise ServiceError(msg)
        return holds
    held = [_precise(route, argument, within) for argument in arguments]
    return any(held)


def _one_key(route: str, model: type, config: Mapping[str, object], fields: Mapping[str, FieldInfo]) -> None:
    """That each of `model`'s fields reads from one key of the JSON: its plain alias, or else its name."""
    if config.get("populate_by_name") or config.get("validate_by_name") or config.get("validate_by_alias") is False:
        msg = f"{route}'s body {model.__qualname__} reads its fields by their names and their aliases: drop populate_by_name, validate_by_name and validate_by_alias, so each reads by one key"
        raise ServiceError(msg)
    for name, field in fields.items():
        if field.validation_alias is not None and field.validation_alias != field.alias:
            msg = f"{route}'s body {model.__qualname__} reads {name} by {field.validation_alias!r}: give it a plain alias=, the one key it reads by, or none"
            raise ServiceError(msg)


def _flat(dependant: Dependant) -> Iterator[Dependant]:
    yield dependant
    for sub in dependant.dependencies:
        yield from _flat(sub)


def _parameter(route: str, where: Literal["path", "query"], key: str, annotation: object) -> Parameter:
    """`key`'s parameter: one of its type's values, or, in a query, a list or a tuple of them."""
    kind = _given(annotation)
    is_many = where == "query" and get_origin(kind) in {list, tuple}
    if is_many:
        arguments = [argument for argument in get_args(kind) if argument is not Ellipsis]
        kind = _given(arguments[0]) if len(set(arguments)) == 1 else None
    found = text(kind) if kind is not None else None
    if found is None:
        listing = "str, int, Decimal, float, bool, date, datetime, UUID, a Literal or an Enum" + (", or a list or tuple of one" if where == "query" else "")
        msg = f"{route}'s {where} parameter {key} is {named(bare(annotation))}, which basis cannot read strictly from text: give it {listing}"
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


def bare(annotation: object) -> object:
    """The type `annotation` names: without `Annotated`'s metadata, and through `type` aliases."""
    while True:
        if isinstance(annotation, TypeAliasType):
            annotation = annotation.__value__
        elif get_origin(annotation) is Annotated:
            annotation = get_args(annotation)[0]
        else:
            return annotation


def named(annotation: object) -> str:
    """`annotation` as a message names it: a class by its name, and any other type as Python writes it."""
    if annotation is None:
        return "None"
    return annotation.__qualname__ if isinstance(annotation, type) else repr(annotation)


def _given(annotation: object) -> object:
    """The type a parameter's text reads as: `bare`, and without `| None`, since a parameter that is absent is the one way
    to give none."""
    annotation = bare(annotation)
    if get_origin(annotation) in {typing.Union, types.UnionType} and types.NoneType in get_args(annotation):
        rest = [argument for argument in get_args(annotation) if argument is not types.NoneType]
        if len(rest) == 1:
            return bare(rest[0])
    return annotation


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


NO_BODY: ErrorEntry = {"type": "extra_forbidden", "loc": ("body",), "msg": "This route takes no body: send the request without one", "input": None}
"""What a request with a body answers on a route that declares none."""


def _refused(where: str, parameter: Parameter, value: str) -> ErrorEntry:
    return {"type": "strict_text", "loc": (where, parameter.key), "msg": f"Input should be {parameter.reads.expected}", "input": value}


_JSON = re.compile(r"application/(?:[\w.+-]+\+)?json", re.IGNORECASE)
_TREE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def is_json(content_type: str | None) -> bool:
    """Whether a body of `content_type` is JSON: `application/json`, or a type of it such as `application/problem+json`."""
    return content_type is not None and _JSON.fullmatch(content_type.partition(";")[0].strip()) is not None


def body(declared: Body, raw: bytes, content_type: str | None) -> tuple[object, list[ErrorEntry]]:
    """The body `raw`, read as `declared`'s type in strict mode, or what is wrong with it."""
    if not is_json(content_type):
        message = "The body is not JSON: send it with Content-Type: application/json" if content_type is None else f"The body is {content_type}, not JSON: send it as application/json"
        return None, [{"type": "content_type", "loc": ("body",), "msg": message, "input": content_type}]
    try:
        value = declared.adapter.validate_json(raw, strict=True, extra="forbid")
    except ValidationError as error:
        return None, [_entry(entry) for entry in error.errors(include_url=False)]
    if not declared.precise:
        return value, []
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
    together: a value the strict mode read is where its JSON was, by each model's or dataclass's field alias, list index
    or key. A set's items, scalars as `_precise` sees to, are each checked against every item of its JSON."""
    match value:
        case Decimal() if not isinstance(tree, str):
            yield _as_number(at, tree)
        case Decimal() if isinstance(tree, str) and _IS_DECIMAL.fullmatch(tree) is None:
            yield _not_digits(at, tree)
        case datetime() if value.tzinfo is None:
            yield _without_offset(at, tree)
        case RootModel():
            yield from _inexact(value.root, tree, at)
        case BaseModel() if isinstance(tree, dict):
            keys = {name: field.alias or name for name, field in type(value).model_fields.items()}
            yield from _inexact_fields(value, keys, tree, at)
        case list() | tuple() if isinstance(tree, list):
            for index, (item, node) in enumerate(zip(value, tree, strict=False)):
                yield from _inexact(item, node, (*at, index))
        case set() | frozenset() if isinstance(tree, list):
            yield from _inexact_set(value, tree, at)
        case dict() if isinstance(tree, dict):
            yield from (found for key, item in value.items() if str(key) in tree for found in _inexact(item, tree[str(key)], (*at, str(key))))
        case _ if dataclasses.is_dataclass(value) and not isinstance(value, type) and isinstance(tree, dict):
            fields: object = getattr(type(value), "__pydantic_fields__", None)
            infos = {str(name): info for name, info in fields.items() if isinstance(info, FieldInfo)} if isinstance(fields, dict) else {}
            keys = {found.name: alias if (info := infos.get(found.name)) is not None and (alias := info.alias) is not None else found.name for found in dataclasses.fields(value)}
            yield from _inexact_fields(value, keys, tree, at)
        case _:
            return


def _inexact_set(value: AbstractSet[object], tree: list[JsonValue], at: tuple[str | int, ...]) -> Iterator[ErrorEntry]:
    if any(isinstance(item, Decimal) for item in value):  # a set of decimals: every item of its JSON is one
        yield from (_as_number((*at, index), node) for index, node in enumerate(tree) if not isinstance(node, str))
        yield from (_not_digits((*at, index), node) for index, node in enumerate(tree) if isinstance(node, str) and _IS_DECIMAL.fullmatch(node) is None)
    if any(isinstance(item, datetime) and item.tzinfo is None for item in value):
        yield _without_offset(at, tree)


def _inexact_fields(value: object, keys: Mapping[str, str], tree: dict[str, JsonValue], at: tuple[str | int, ...]) -> Iterator[ErrorEntry]:
    """A model's or a dataclass's fields, each the attribute named and the JSON at its key."""
    for name, key in keys.items():
        if key in tree:
            yield from _inexact(getattr(value, name), tree[key], (*at, key))


def _as_number(at: tuple[str | int, ...], given: JsonValue) -> ErrorEntry:
    message = 'Input should be a string of the decimal, as "0.045": a JSON number reaches a decimal through a float, which can change it'
    return {"type": "decimal_string", "loc": at, "msg": message, "input": given}


def _not_digits(at: tuple[str | int, ...], given: JsonValue) -> ErrorEntry:
    message = 'Input should be a decimal\'s digits, as "0.045" or "-2.50": a point and digits, with a minus sign only before them and no exponent'
    return {"type": "decimal_digits", "loc": at, "msg": message, "input": given}


def _without_offset(at: tuple[str | int, ...], given: JsonValue) -> ErrorEntry:
    return {"type": "timezone_aware", "loc": at, "msg": "Input should be a time with its offset, as 2026-11-06T14:30:00Z", "input": given}
