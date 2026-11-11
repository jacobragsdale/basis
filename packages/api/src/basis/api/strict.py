"""How basis reads a request: strictly, so that no key it does not know is ignored and no value is coerced into its type.
A misspelled field, or a value of the wrong kind, fails at the boundary with a 422 `invalid-request` problem that names
it, before the handler runs.

**A JSON body** is read by basis itself, from its bytes, in pydantic's strict mode on JSON, before FastAPI sees it. Each
value has its own type in JSON: a string for a `str`, a number without a fraction for an `int`, `true` or `false` for a
`bool`, an array for a list. A type JSON has none for is a string of its standard form: a date as `"2026-11-06"`, a UUID
as `"019a5c3e-..."`. Two rules go further than pydantic's strict mode:

- Every number is a JSON number, a `float` or an `int`, and a `Decimal` anywhere in a request or a response is refused
  when the route is declared (`aware`; design.md, "Numbers"). A number is one a float holds: `NaN`, `Infinity` and
  `-Infinity`, which JSON's grammar has not, and a number too large for a float, as `1e999`, are refused, since
  pydantic would read each as NaN or infinity, which a response, and another service's call, would then carry as null.
- An object gives each key once: pydantic would keep the last of a key given twice, which another reader of the same
  bytes may not.
- A time carries its offset, as `"2026-11-06T14:30:00Z"`: a time without one could be any zone's. A time is pydantic's
  `AwareDatetime`, which refuses one without, in a request and a response alike; a plain `datetime`, which takes one
  without, or a time of day, `datetime.time`, whose offset pydantic cannot require, is refused when the route is
  declared (`aware`).

A key no model declares is refused at every depth, whatever a model's own config says. The body is JSON only when the
request says so, as `Content-Type: application/json`; a route takes at most one body, its whole JSON, declared in its
handler's own parameters, and a route that declares none refuses one. The body is validated once, by basis: FastAPI
gets the value it made, which it does not validate again, so a validator never runs twice.

Each of a request model's fields reads from one key of the JSON, its plain `alias` or else its name, which a 422 names
it by: no `validation_alias` of its own, `AliasChoices` or `AliasPath`, and no `populate_by_name`, `validate_by_name` or
`validate_by_alias=False`. The body is parsed twice, by Python's own parser for what pydantic's lets through, and
then by pydantic's, and a large one off the event loop (`routes.py`); a body over the settings' `serve.max_body_bytes`
is refused, 413 `body-too-large`, before it is read whole.

**Query and path parameters** arrive as text, so basis reads each by its type's grammar before FastAPI converts it,
and FastAPI's conversion of what the grammar accepts is exact:

| Type | The text it accepts |
|---|---|
| `str` | any |
| `int` | digits, with a minus sign only before them and no leading zero: `10`, `-3`, `0` |
| `float` | a JSON number, `1.5` or `2e-3`, that is finite |
| `bool` | `true` or `false` |
| `date` | `2026-11-06` |
| `AwareDatetime` | `2026-11-06T14:30:00Z`, or with `+01:00`, and up to six digits of a second's fraction: never without an offset |
| `UUID` | 32 hex digits in groups of 8-4-4-4-12, `019a5c3e-0000-7000-8000-000000000001` |
| a `Literal` or an `Enum` | one of its values exactly, as a `str` or an `int` reads |

A type may be optional, `T | None`, and a query parameter may be a list or a tuple of one of these, whose values are
the key given once each: `?security=A&security=B`. A key the route does not declare is refused, and so is a key given
twice for one value. A route declares its query parameters one by one, or as one model with `Query()`; a path
parameter's type is the handler's, so a path never names a converter, as `{n:int}` would."""

import dataclasses
import json
import math
import re
import types
import typing
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal, TypeAliasType, get_args, get_origin
from uuid import UUID

from fastapi import params
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_dependant
from fastapi.exceptions import FastAPIError
from pydantic import AwareDatetime, BaseModel, FutureDatetime, NaiveDatetime, PastDatetime, PydanticSchemaGenerationError, TypeAdapter, ValidationError
from pydantic.fields import FieldInfo

from basis.api.errors import DeclarationError

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
    info: FieldInfo
    """Its type and what it declares of its values, as FastAPI reads them."""


@dataclass(frozen=True)
class Body:
    adapter: TypeAdapter[object]
    """The body's type, read in strict mode from JSON."""
    info: FieldInfo | None = None
    """Its type and what it declares of its value, as FastAPI reads them; None for a body made without a route."""


@dataclass(frozen=True)
class Shape:
    """What a route reads from a request, as its handler's parameters, and its dependencies', declare it."""

    path: tuple[Parameter, ...]
    query: tuple[Parameter, ...]
    body: Body | None
    """None for a route that takes no body."""


def shape(path: str, handler: Callable[..., object], name: str) -> Shape:
    """What the route `name` reads, after checking that basis can read each of its parameters strictly."""
    try:
        dependant = get_dependant(path=path, call=handler)
    except (FastAPIError, AssertionError) as error:  # FastAPI's own refusals of a parameter, whose fixes are FastAPI's
        raise DeclarationError(_unreadable(name, handler, error)) from None
    found = list(_flat(dependant))
    if any(dependant.background_tasks_param_name is not None for dependant in found):
        msg = (
            f"{name} takes BackgroundTasks, whose work runs after the response, outside the route's deadline and transaction, where a failure reaches nobody:"
            " do the work in the handler, and make a call to another service that changes its state with Client.send, which the outbox makes once the request commits"
        )
        raise DeclarationError(msg)
    readable_path = tuple(_parameter(name, "path", field.alias, field.field_info) for dependant in found for field in dependant.path_params)
    query: list[Parameter] = []
    for dependant in found:
        for field in dependant.query_params:
            annotation = field.field_info.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):  # a query model: its fields are the keys
                query.extend(_parameter(name, "query", inner.alias or key, inner) for key, inner in annotation.model_fields.items())
            else:
                query.append(_parameter(name, "query", field.alias, field.field_info))
    bodies = [field for dependant in found for field in dependant.body_params]
    if len(bodies) > 1:
        msg = f"{name} takes {len(bodies)} bodies, {', '.join(field.name for field in bodies)}: a route takes one JSON body, so declare one model that holds them all"
        raise DeclarationError(msg)
    if bodies and not found[0].body_params:
        msg = f"a dependency of {name}'s takes the body {bodies[0].name}: take it in {name}'s own parameters, and pass it on"
        raise DeclarationError(msg)
    body: Body | None = None
    if bodies:
        info = bodies[0].field_info
        if isinstance(info, params.Body) and info.embed:
            msg = f"{name}'s body {bodies[0].name} is embedded in an object of its own: drop embed=True, so the body is the model's JSON itself"
            raise DeclarationError(msg)
        if isinstance(info, params.Form | params.File):
            msg = f"{name} takes a form: a route's body is JSON, so declare a model for it"
            raise DeclarationError(msg)
        if info.annotation is None:
            msg = f"{name}'s body {bodies[0].name} has no type: annotate it with the model it reads"
            raise DeclarationError(msg)
        aware(name, "body", info.annotation)
        _keys(name, info.annotation, frozenset())
        body = Body(TypeAdapter(info.annotation), info)
    return Shape(readable_path, tuple(query), body)


def _keys(route: str, kind: object, within: frozenset[type]) -> None:
    """That every model and pydantic dataclass a body of `kind` holds reads each field from one key of the JSON.
    `within` holds the models being walked, for one that refers to itself."""
    kind = bare(kind)
    if (origin := get_origin(kind)) is not None:
        if origin is not Literal:
            for argument in get_args(kind):
                if argument is not Ellipsis:
                    _keys(route, argument, within)
        return
    if not isinstance(kind, type) or kind in within:
        return
    if issubclass(kind, BaseModel):
        _one_key(route, kind, kind.model_config, kind.model_fields)
        for field in kind.model_fields.values():
            _keys(route, field.annotation, within | {kind})
    elif dataclasses.is_dataclass(kind):
        fields: object = getattr(kind, "__pydantic_fields__", None)
        if isinstance(fields, dict):
            config: object = getattr(kind, "__pydantic_config__", None)
            settled = {str(key): item for key, item in config.items()} if isinstance(config, Mapping) else {}
            _one_key(route, kind, settled, {str(name): info for name, info in fields.items() if isinstance(info, FieldInfo)})
        hints = typing.get_type_hints(kind, include_extras=True)
        for found in dataclasses.fields(kind):
            _keys(route, hints[found.name], within | {kind})


def _one_key(route: str, model: type, config: Mapping[str, object], fields: Mapping[str, FieldInfo]) -> None:
    """That each of `model`'s fields reads from one key of the JSON: its plain alias, or else its name."""
    if config.get("populate_by_name") or config.get("validate_by_name") or config.get("validate_by_alias") is False:
        msg = f"{route}'s body {model.__qualname__} reads its fields by their names and their aliases: drop populate_by_name, validate_by_name and validate_by_alias, so each reads by one key"
        raise DeclarationError(msg)
    for name, field in fields.items():
        if field.validation_alias is not None and field.validation_alias != field.alias:
            msg = f"{route}'s body {model.__qualname__} reads {name} by {field.validation_alias!r}: give it a plain alias=, the one key it reads by, or none"
            raise DeclarationError(msg)


def _flat(dependant: Dependant) -> Iterator[Dependant]:
    yield dependant
    for sub in dependant.dependencies:
        yield from _flat(sub)


LISTING = "str, int, float, bool, date, AwareDatetime, UUID, a Literal or an Enum"
"""The types a path or query parameter may be, which basis reads strictly from text."""


def _unreadable(name: str, handler: Callable[..., object], error: Exception) -> str:
    """Why FastAPI refused a parameter of `handler`'s, with basis's fix, which names the parameter."""
    hints = typing.get_type_hints(handler, include_extras=True)
    if (query := re.search(r"Query parameter '([^']+)'", str(error))) is not None:
        given = named(bare(hints.get(query[1], object)))
        return f"{name}'s query parameter {query[1]} is {given}, which basis cannot read strictly from text: give it {LISTING}, or a list or tuple of one"
    for parameter, hint in hints.items():
        if parameter == "return":
            continue
        try:
            TypeAdapter(hint)
        except PydanticSchemaGenerationError:
            return f"{name}'s parameter {parameter} is {named(bare(hint))}, which pydantic cannot read: declare it a pydantic model, or a type of a path or query parameter, as str or int"
    return f"{name}'s parameters are refused by FastAPI ({' '.join(str(error).split())}): give each a pydantic model, or a type of a path or query parameter, as str or int"


def _parameter(route: str, where: Literal["path", "query"], key: str, info: FieldInfo) -> Parameter:
    """`key`'s parameter: one of its type's values, or, in a query, a list or a tuple of them."""
    annotation = info.annotation
    aware(route, f"{where} parameter {key}", annotation)
    kind = _given(annotation)
    is_many = where == "query" and get_origin(kind) in {list, tuple}
    if is_many:
        arguments = [argument for argument in get_args(kind) if argument is not Ellipsis]
        kind = _given(arguments[0]) if len(set(arguments)) == 1 else None
    found = text(kind) if kind is not None else None
    if found is None:
        listing = LISTING + (", or a list or tuple of one" if where == "query" else "")
        msg = f"{route}'s {where} parameter {key} is {named(bare(annotation))}, which basis cannot read strictly from text: give it {listing}"
        raise DeclarationError(msg)
    return Parameter(key, found, is_many, info)


def text(kind: object) -> Text | None:
    """How a value of `kind` reads from text, or None where basis cannot read it strictly."""
    origin = get_origin(kind)
    if origin is Literal:
        return _choices(get_args(kind))
    if isinstance(kind, type) and issubclass(kind, Enum):
        return _choices([member.value for member in kind])
    if not isinstance(kind, type) or origin is not None:
        return None
    if kind is AWARE:  # a marker of pydantic's, for a datetime with its offset
        kind = datetime
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


def is_json(content_type: str | None) -> bool:
    """Whether a body of `content_type` is JSON: `application/json`, or a type of it such as `application/problem+json`."""
    return content_type is not None and _JSON.fullmatch(content_type.partition(";")[0].strip()) is not None


def body(declared: Body, raw: bytes, content_type: str | None) -> tuple[object, list[ErrorEntry]]:
    """The body `raw`, read as `declared`'s type in strict mode, or what is wrong with it."""
    if not is_json(content_type):
        message = "The body is not JSON: send it with Content-Type: application/json" if content_type is None else f"The body is {content_type}, not JSON: send it as application/json"
        return None, [{"type": "content_type", "loc": ("body",), "msg": message, "input": content_type}]
    sent = _tree(raw)
    if sent is not None and (unsound := list(_unsound(sent, ()))):
        return None, unsound
    try:
        value = declared.adapter.validate_json(raw, strict=True, extra="forbid")
    except ValidationError as error:
        return None, [_entry(entry, sent) for entry in error.errors(include_url=False)]
    return value, []


class _Object(dict[str, object]):
    """An object of a body, with the keys it gives more than once, which pydantic's parser takes the last value of."""

    repeated: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Infinite:
    """A number of a body that no float holds: `NaN`, `Infinity` or `-Infinity`, which pydantic's parser takes, and
    JSON's grammar does not, or one too large, as `1e999`, which every parser reads as infinity."""

    text: str


def _pairs(pairs: list[tuple[str, object]]) -> _Object:
    found = _Object(pairs)
    if len(found) < len(pairs):
        counted = Counter(key for key, _ in pairs)
        found.repeated = tuple(key for key, count in counted.items() if count > 1)
    return found


def _number(text: str) -> float | _Infinite:
    number = float(text)
    return number if math.isfinite(number) else _Infinite(text)


def _tree(raw: bytes) -> object:
    """The body `raw` as the JSON it holds, each object an `_Object` and each number no float holds an `_Infinite`, or
    None where it is not JSON, which pydantic's own error then says."""
    try:
        found: object = json.loads(raw, object_pairs_hook=_pairs, parse_float=_number, parse_constant=_Infinite)
    except ValueError:  # not JSON, or an integer of more digits than Python reads, which pydantic refuses itself
        return None
    return found


def _unsound(node: object, at: tuple[str | int, ...]) -> Iterator[ErrorEntry]:
    """What `node`, at `at` in a body, holds that pydantic would read as something else: a number no float holds,
    which would reach the handler as infinity or NaN, and a key given twice, of which it would keep the last."""
    if isinstance(node, _Infinite):
        yield {"type": "finite_number", "loc": ("body", *at), "msg": "Input should be a finite number: send a number a float holds, and null for none", "input": node.text}
    elif isinstance(node, _Object):
        for key in node.repeated:
            yield {"type": "repeated_key", "loc": ("body", *at, key), "msg": "The key is given twice: give each key of an object once", "input": key}
        for key, value in node.items():
            yield from _unsound(value, (*at, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _unsound(value, (*at, index))


def _entry(entry: Mapping[str, object], sent: object) -> ErrorEntry:
    location = entry.get("loc")
    given = tuple(part for part in location if isinstance(part, str | int)) if isinstance(location, tuple) else ()
    at = _in_body(given, sent, missing=entry.get("type") == "missing")
    found: ErrorEntry = {"type": entry.get("type"), "loc": ("body", *at), "msg": entry.get("msg"), "input": entry.get("input")}
    if entry.get("type") == "json_invalid":
        context = entry.get("ctx")
        found["msg"] = "The body is not JSON"
        found["ctx"] = {"error": context.get("error")} if isinstance(context, Mapping) else {}
    return found


def _in_body(location: tuple[str | int, ...], sent: object, *, missing: bool) -> tuple[str | int, ...]:
    """pydantic's `location` of an error in the body `sent`, as the keys and indices of the body alone: pydantic puts
    a union member's tag, such as `Leg` or `list[int]`, and a validator's, such as `function-after[...]`, between them,
    which no body holds. A key the body lacks is kept where it ends the location of a missing field."""
    if sent is None:
        return location
    found: list[str | int] = []
    node: object = sent
    for index, part in enumerate(location):
        if (child := _step(node, part)) is not None:
            found.append(part)
            node = child[0]
        elif missing and index == len(location) - 1 and isinstance(node, dict):
            found.append(part)
    return tuple(found)


def _step(node: object, part: str | int) -> tuple[object] | None:
    """The body's value at `part` of `node`, or None where `node` holds nothing there."""
    if isinstance(node, _Object) and isinstance(part, str):
        return (node[part],) if part in node else None
    if isinstance(node, list) and isinstance(part, int):
        return (node[part],) if 0 <= part < len(node) else None
    return None


AWARE: object = AwareDatetime
"""pydantic's marker for a datetime with its offset, a class at run time, as an annotation names it."""
_NAIVE: tuple[object, ...] = (NaiveDatetime, PastDatetime, FutureDatetime)
"""pydantic's markers for a datetime that may come without its offset."""


def aware(route: str, where: str, kind: object) -> None:
    """That every time `kind` can hold, at any depth, carries its offset: a `datetime` there, or one of pydantic's
    datetimes but `AwareDatetime`, is refused, and so is a time of day, whose offset pydantic cannot require. A
    `Decimal` is refused too, since every number basis gives or takes is a JSON number, a float or an int (design.md,
    "Numbers"). `where` names what `kind` is of the route's, as `response` or `body`."""
    if (found := _naive_time(kind, named(bare(kind)), frozenset())) is None:
        return
    at, what = found
    if what == "decimal":
        fix = "declare it a float, which every number basis gives or takes is, or an int for a whole count"
        msg = f"{route}'s {where} holds {at}, a Decimal, and every number basis gives or takes is a JSON number: {fix}"
        raise DeclarationError(msg)
    if what == "time":
        fix = "give the time of day with its date, as an AwareDatetime, or as text that names its zone"
        msg = f"{route}'s {where} holds {at}, a time of day, whose offset pydantic cannot require, and every time basis gives or takes carries one: {fix}"
    else:
        fix = "declare it pydantic's AwareDatetime, which refuses a time without its offset, as `from pydantic import AwareDatetime`"
        msg = f"{route}'s {where} holds {at}, {what}, which takes a time without its offset, and every time basis gives or takes carries one: {fix}"
    raise DeclarationError(msg)


def _naive_time(kind: object, at: str, within: frozenset[object]) -> tuple[str, str] | None:
    """Where in `kind` a time that may be naive is, and what it is, or None: `within` holds the models and the type
    aliases being walked, so a recursive alias or model is walked once."""
    while True:
        if isinstance(kind, TypeAliasType):
            if kind in within:
                return None
            within |= {kind}
            kind = kind.__value__
        elif get_origin(kind) is Annotated:
            kind = get_args(kind)[0]
        else:
            break
    inner: list[tuple[str, object]] = []
    if (origin := get_origin(kind)) is not None:
        inner = [] if origin is Literal else [(at, argument) for argument in get_args(kind) if argument is not Ellipsis]
    elif isinstance(kind, type) and issubclass(kind, Decimal):
        return at, "decimal"
    elif kind in _NAIVE or (isinstance(kind, type) and issubclass(kind, datetime)):
        return at, f"a {named(kind)}"
    elif isinstance(kind, type) and issubclass(kind, time):
        return at, "time"
    elif isinstance(kind, type) and kind not in within and kind is not AWARE:
        within |= {kind}
        if issubclass(kind, BaseModel):
            inner = [(f"{kind.__qualname__}.{name}", field.annotation) for name, field in kind.model_fields.items()]
            inner += [(f"{kind.__qualname__}.{name}", field.return_type) for name, field in kind.model_computed_fields.items()]
        elif dataclasses.is_dataclass(kind) or typing.is_typeddict(kind):
            inner = [(f"{kind.__qualname__}.{name}", found) for name, found in typing.get_type_hints(kind, include_extras=True).items()]
    return next((found for place, argument in inner for found in [_naive_time(argument, place, within)] if found is not None), None)
