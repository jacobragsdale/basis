"""A dataset route's rows, as the columns of the Arrow stream it answers with when a request asks for one. A route that
returns a dataset declares it, `api.get(..., response=list[CashFlow], dataset=True)`, and answers JSON, or Arrow when
the request's `Accept` asks for `application/vnd.apache.arrow.stream` (`arrow.py`).

A dataset is a list or a tuple of one model, its rows, each field a column, named as its JSON names it: by its
serialization alias, its alias, or its name. A row is flat, so each column has one type:

| Field | Column |
|---|---|
| `str`, `UUID`, a `Literal` or an `Enum` of strings | `utf8` |
| `float` | `float64` |
| `bool` | `bool` |
| `date` | `date32` |
| `AwareDatetime` | `timestamp[us, tz=UTC]` |

Every number is a float (design.md, "Numbers"): an `int`, a `Decimal`, or a `Literal` or an `Enum` of integers is
refused, each with its fix. A float column is always nullable, since NaN and ±inf travel as null; any other column is
nullable where its field may be None, and only there. A row holds no field it computes and no keys beyond its fields,
and no serializer of its own, so its JSON and its Arrow hold the same values. Anything else is refused when the route
is declared.

This module only declares: `arrow.py` writes the stream, which a service's declarations never load."""

import types
import typing
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal, cast, get_args, get_origin
from uuid import UUID

import annotated_types
from pydantic import BaseModel, PlainSerializer, RootModel, TypeAdapter, WrapSerializer
from pydantic.fields import FieldInfo

from basis.api import strict
from basis.api.errors import DeclarationError
from basis.api.format import Formatted
from basis.core.display import Display

type Kind = Literal["utf8", "float64", "bool", "date32", "timestamp"]


@dataclass(frozen=True)
class Column:
    name: str
    """The column's name: the key the row's JSON gives the field."""
    attribute: str
    """The model's field it holds."""
    kind: Kind
    nullable: bool
    format: Display | None = None
    """Its display format, which a float field declares as `Annotated[float, api.format.money("USD")]`: see
    `basis.api.format`."""
    enum: bool = False
    """Whether its values are an `Enum`'s members, whose values the column holds."""
    values: frozenset[str] | None = None
    """A `Literal`'s or an `Enum`'s values, the only text the column holds; None for a column of any text."""
    text: bool = False
    """Whether its values are UUIDs, whose text the column holds."""


@dataclass(frozen=True)
class Rows:
    """What a dataset route answers: rows of `model`, as `columns`."""

    model: type[BaseModel]
    columns: tuple[Column, ...]
    adapter: TypeAdapter[Sequence[BaseModel]]
    """The route's response, the list or the tuple of rows, which the rows validate and dump by, as FastAPI's JSON
    answer does."""


_KINDS: tuple[tuple[type, Kind], ...] = (
    # in this order: a bool is an int, and a datetime a date
    (bool, "bool"),
    (float, "float64"),
    (date, "date32"),
    (UUID, "utf8"),
    (str, "utf8"),
)


def rows(response: object, route: str, *, table: bool = False) -> Rows:
    """The rows of the dataset `route` answers with `response`, after checking that each is a flat row of columns
    Arrow holds; with `table`, rows of an `api.Table`, which declare no value constraints, since no row is checked by
    itself."""
    kind = strict.bare(response)
    arguments = [argument for argument in get_args(kind) if argument is not Ellipsis]
    model = strict.bare(arguments[0]) if get_origin(kind) in {list, tuple} and len(arguments) == 1 else None
    if not (isinstance(model, type) and issubclass(model, BaseModel)) or issubclass(model, RootModel):
        msg = f"{route} declares a dataset, and answers {strict.named(kind)}: a dataset is rows, so answer list[Row], Row a model whose fields are its columns"
        raise DeclarationError(msg)
    if model.model_config.get("extra") == "allow":
        msg = f"{route}'s dataset rows, {model.__qualname__}, allow keys beyond their fields, which no column holds: drop extra='allow'"
        raise DeclarationError(msg)
    if model.model_computed_fields:
        msg = f"{route}'s dataset rows, {model.__qualname__}, compute {', '.join(model.model_computed_fields)}, which no column holds: make each a field"
        raise DeclarationError(msg)
    decorators = model.__pydantic_decorators__
    serialized = [*(name for found in decorators.field_serializers.values() for name in found.info.fields), *(["the whole row"] if decorators.model_serializers else [])]
    serialized += [name for name, field in model.model_fields.items() if any(isinstance(found, PlainSerializer | WrapSerializer) for found in _unwrapped(field.annotation, list(field.metadata))[2])]
    if serialized:
        fix = "drop the serializer, and give the field the value it answers, as the handler makes it"
        msg = f"{route}'s dataset rows, {model.__qualname__} serializes {', '.join(dict.fromkeys(serialized))} itself, and a row's JSON and its Arrow hold the same values: {fix}"
        raise DeclarationError(msg)
    columns = tuple(_column(route, model, name, field, table=table) for name, field in model.model_fields.items() if field.exclude is not True)
    return Rows(model, columns, TypeAdapter(cast("type[Sequence[BaseModel]]", kind)))  # a value's type, which ty cannot read as one


def _column(route: str, model: type[BaseModel], name: str, field: FieldInfo, *, table: bool) -> Column:
    where = f"{route}'s dataset column {model.__qualname__}.{name}"
    annotation, nullable, metadata = _unwrapped(field.annotation, list(field.metadata))
    if table and (constraints := [found for found in metadata if _constrains(found)]):
        fix = "drop the constraint, and keep the rule in the domain or the query"
        msg = f"{where} declares {', '.join(repr(found) for found in constraints)}, and an api.Table is checked by its columns' types, never row by row: {fix}"
        raise DeclarationError(msg)
    key = field.serialization_alias or field.alias or name
    if (choice := _choice(where, annotation)) is not None:
        choices = get_args(annotation) if get_origin(annotation) is Literal else [member.value for member in cast("type[Enum]", annotation)]
        return Column(key, name, "utf8", nullable, enum=choice, values=frozenset(str(value) for value in choices))
    if isinstance(annotation, type) and issubclass(annotation, Decimal | int) and not issubclass(annotation, bool):
        what = "a Decimal" if issubclass(annotation, Decimal) else "an int"
        msg = f"{where} is {what}, and every number in a dataset is a float: declare it float for an amount or a count, or str for an identifier"
        raise DeclarationError(msg)
    kind: Kind | None = "timestamp" if annotation is strict.AWARE else next((found for scalar, found in _KINDS if isinstance(annotation, type) and issubclass(annotation, scalar)), None)
    if kind is None:
        msg = f"{where} is {strict.named(annotation)}, which Arrow's columns do not hold: give it str, float, bool, date, AwareDatetime, UUID, a Literal or an Enum of strings, or T | None"
        raise DeclarationError(msg)
    formats = [found.display for found in metadata if isinstance(found, Formatted)]
    if formats and kind != "float64":
        msg = f"{where} declares a display format, and a dataset's display formats are its numbers', its float columns': drop it, or make the column a float"
        raise DeclarationError(msg)
    if len(formats) > 1:
        msg = f"{where} declares {len(formats)} display formats: give it one"
        raise DeclarationError(msg)
    return Column(key, name, kind, nullable or kind == "float64", format=formats[0] if formats else None, text=isinstance(annotation, type) and issubclass(annotation, UUID))


def _constrains(metadata: object) -> bool:
    """Whether a field's metadata is a value constraint, such as `Field(gt=0)`'s or a pattern's; a display format is none."""
    return not isinstance(metadata, Formatted) and (isinstance(metadata, annotated_types.BaseMetadata) or type(metadata).__name__ == "_PydanticGeneralMetadata")


def _unwrapped(annotation: object, metadata: list[object]) -> tuple[object, bool, list[object]]:
    """`annotation` through `Annotated` and type aliases, without `| None`, whether it had it, and the metadata of every
    `Annotated` it passed, with each `Field(...)`'s own."""
    nullable = False
    while True:
        if isinstance(annotation, typing.TypeAliasType):
            annotation = annotation.__value__
        elif get_origin(annotation) is Annotated:
            for found in get_args(annotation)[1:]:
                metadata.extend(found.metadata if isinstance(found, FieldInfo) else [found])
            annotation = get_args(annotation)[0]
        elif get_origin(annotation) in {typing.Union, types.UnionType} and types.NoneType in get_args(annotation):
            rest = [argument for argument in get_args(annotation) if argument is not types.NoneType]
            nullable = True
            if len(rest) != 1:
                return annotation, nullable, metadata
            annotation = rest[0]
        else:
            return annotation, nullable, metadata


def _choice(where: str, annotation: object) -> bool | None:
    """Whether a `Literal` or an `Enum` of strings, a text column, is an `Enum`; None for any other type. One of
    integers is refused, since a dataset's numbers are floats."""
    if get_origin(annotation) is Literal:
        values, is_enum = list(get_args(annotation)), False
    elif isinstance(annotation, type) and issubclass(annotation, Enum):
        values, is_enum = [member.value for member in annotation], True
    else:
        return None
    if all(isinstance(value, str) for value in values):
        return is_enum
    msg = f"{where} is {strict.named(annotation)}, whose values are not all strings, and a dataset's choices are text: declare a str Enum, or a Literal of strings"
    raise DeclarationError(msg)
