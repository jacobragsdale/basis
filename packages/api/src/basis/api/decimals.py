"""A decimal a request gives, as the OpenAPI document says it: a JSON string of its digits, never a number, with what
its type declares of it.

    {"type": "string", "pattern": "^-?(?:0|[1-9][0-9]{0,15})(?:\\.[0-9]{1,2}0*)?$", "x-basis-decimal": {"exclusiveMinimum": "0"}}

- Its `pattern` is the grammar basis reads it by (`strict.DECIMAL`): digits, a point and digits, with a minus sign only
  before them and no exponent. Where the type declares `max_digits` or `decimal_places`, the pattern counts them as
  pydantic does: a whole part of at most `max_digits - decimal_places` digits, a fraction of at most `decimal_places`
  digits before zeros that end it, and with `max_digits` alone, at most that many in all.
- `x-basis-decimal`, which every request's decimal has, holds its bounds, which JSON Schema cannot set for a string:
  `minimum`, `exclusiveMinimum`, `maximum`, `exclusiveMaximum` and `multipleOf`, each a string of the decimal, as the
  type declares it with `ge`, `gt`, `le`, `lt` and `multiple_of`. A UI reads them to check a value before it sends it.

A decimal's schema is made from its type, the field's annotation and what it declares, never from pydantic's schema of
it, which gives its bounds as floats and its digits not at all."""

import types
import typing
from collections.abc import Iterator, Sequence
from decimal import Decimal
from typing import Annotated, TypeAliasType, get_args, get_origin

from pydantic import BaseModel, JsonValue
from pydantic.fields import FieldInfo

from basis.api import strict

EXTENSION = "x-basis-decimal"
"""The key of a request's decimal that holds its bounds, and marks it a decimal."""
_BOUNDS = {"ge": "minimum", "gt": "exclusiveMinimum", "le": "maximum", "lt": "exclusiveMaximum", "multiple_of": "multipleOf"}
_WHOLE = r"(?:0|[1-9][0-9]*)"
_FRACTION = r"(?:\.[0-9]+)?"


def schema(metadata: Sequence[object]) -> dict[str, JsonValue]:
    """A request's decimal, as `metadata`, what its type and its field declare, has it."""
    declared = {name: value for item in metadata for name in (*_BOUNDS, "max_digits", "decimal_places") if (value := getattr(item, name, None)) is not None}
    digits, places = declared.get("max_digits"), declared.get("decimal_places")
    bounds: dict[str, JsonValue] = {_BOUNDS[name]: _text(value) for name, value in declared.items() if name in _BOUNDS}
    return {"type": "string", "pattern": f"^{pattern(digits if isinstance(digits, int) else None, places if isinstance(places, int) else None)}$", EXTENSION: bounds}


def pattern(digits: int | None, places: int | None) -> str:
    """The grammar of a decimal of at most `digits` digits in all and `places` after its point, without its anchors:
    `strict.DECIMAL` where it declares neither."""
    if places is not None:
        if digits is not None and digits <= places and places > 0:  # no whole digit, so a point and a place, as pydantic counts `0` as one whole digit
            return rf"-?0\.[0-9]{{1,{places}}}0*"
        whole = _WHOLE if digits is None else _whole_up_to(digits - places)
        return f"-?{whole}{_fraction_up_to(places)}"
    if digits is not None:
        return "-?(?:" + "|".join(f"{_whole_of(count)}{_fraction_up_to(digits - count)}" for count in range(digits + 1)) + ")"
    return strict.DECIMAL


def _whole_up_to(count: int) -> str:
    return "0" if count <= 0 else f"(?:0|[1-9][0-9]{{0,{count - 1}}})"


def _whole_of(count: int) -> str:
    return "0" if count == 0 else f"[1-9][0-9]{{{count - 1}}}"


def _fraction_up_to(count: int) -> str:
    return r"(?:\.0+)?" if count <= 0 else rf"(?:\.[0-9]{{1,{count}}}0*)?"


def _text(value: object) -> str:
    return str(value) if isinstance(value, Decimal | int) and not isinstance(value, bool) else str(Decimal(str(value)))


def of_field(info: FieldInfo | None, annotation: object) -> list[dict[str, JsonValue]]:
    """The schema of each decimal a field of `annotation`, which `info` declares, holds outside the models it holds, in
    the order the document gives them: through its unions, its lists' and tuples' items and its mappings' values."""
    return list(_walk(annotation, list(info.metadata) if info is not None else [], frozenset()))


def _walk(kind: object, metadata: list[object], within: frozenset[object]) -> Iterator[dict[str, JsonValue]]:
    if (unwrapped := _unwrapped(kind, metadata, within)) is None:
        return
    kind, metadata, within = unwrapped
    origin = get_origin(kind)
    arguments = [argument for argument in get_args(kind) if argument is not Ellipsis]
    if origin in {typing.Union, types.UnionType}:
        for argument in arguments:
            yield from _walk(argument, metadata, within)
    elif origin in {dict, typing.Mapping} or (isinstance(origin, type) and issubclass(origin, dict)):
        yield from _walk(arguments[-1], [], within) if arguments else iter(())  # a mapping's value, its key being no decimal the document shows
    elif origin is not None and origin is not typing.Literal:
        for argument in arguments:
            yield from _walk(argument, [], within)
    elif isinstance(kind, type) and issubclass(kind, Decimal):
        yield schema(metadata)


def _unwrapped(kind: object, metadata: list[object], within: frozenset[object]) -> tuple[object, list[object], frozenset[object]] | None:
    """`kind` through its type aliases and `Annotated`, with every metadata it passed, or None for an alias already
    being walked."""
    while True:
        if isinstance(kind, TypeAliasType):
            if kind in within:
                return None
            within |= {kind}
            kind = kind.__value__
        elif get_origin(kind) is Annotated:
            for item in get_args(kind)[1:]:
                metadata = [*metadata, *(item.metadata if isinstance(item, FieldInfo) else [item])]
            kind = get_args(kind)[0]
        else:
            return kind, metadata, within


def fields(model: type[BaseModel]) -> dict[str, list[dict[str, JsonValue]]]:
    """The schemas of the decimals each of `model`'s fields holds, by the key a request gives the field."""
    return {field.alias or name: of_field(field, field.annotation) for name, field in model.model_fields.items()}


def models(kinds: Sequence[object]) -> list[type[BaseModel]]:
    """Every model a request of `kinds` can hold, at any depth."""
    seen: list[type[BaseModel]] = []
    waiting = list(kinds)
    walked: set[int] = set()
    while waiting:
        kind = strict.bare(waiting.pop())
        if id(kind) in walked:
            continue
        walked.add(id(kind))
        if isinstance(kind, TypeAliasType):
            waiting.append(kind.__value__)
        elif get_origin(kind) is not None:
            waiting.extend(argument for argument in get_args(kind) if argument is not Ellipsis)
        elif isinstance(kind, type) and issubclass(kind, BaseModel):
            seen.append(kind)
            waiting.extend(field.annotation for field in kind.model_fields.values())
    return seen
