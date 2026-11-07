"""A dataset route's rows, as the columns of the Arrow stream it answers with when a request asks for one. A route that
returns a dataset declares it, `api.get(..., response=list[CashFlow], dataset=True)`, and answers JSON, or Arrow when
the request's `Accept` asks for `application/vnd.apache.arrow.stream` (`arrow.py`).

A dataset is a list or a tuple of one model, its rows, each field a column, named as its JSON names it: by its
serialization alias, its alias, or its name. A row is flat, so each column has one type:

| Field | Column |
|---|---|
| `str`, `UUID`, a `Literal` or an `Enum` of strings | `utf8` |
| `int`, a `Literal` or an `Enum` of integers | `int64` |
| `float` | `float64` |
| `bool` | `bool` |
| `Decimal`, with `max_digits` and `decimal_places` | `decimal128(max_digits, decimal_places)` |
| `date` | `date32` |
| `AwareDatetime` | `timestamp[us, tz=UTC]` |

A column is nullable where its field may be None, and only there. A decimal declares its digits, at most 38, and its
places, as `Annotated[Decimal, Field(max_digits=18, decimal_places=2)]`, since a column of Arrow's has one precision
and one scale; a row holds no field it computes and no keys beyond its fields, so its JSON and its Arrow hold the same
values. Anything else is refused when the route is declared.

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

from pydantic import BaseModel, RootModel, TypeAdapter
from pydantic.fields import FieldInfo

from basis.api import strict
from basis.api.errors import ServiceError

type Kind = Literal["utf8", "int64", "float64", "bool", "decimal", "date32", "timestamp"]

MAX_DIGITS = 38
"""The most digits an Arrow `decimal128` holds."""


@dataclass(frozen=True)
class Column:
    name: str
    """The column's name: the key the row's JSON gives the field."""
    attribute: str
    """The model's field it holds."""
    kind: Kind
    nullable: bool
    precision: int = 0
    """A decimal's digits; 0 for a column of any other kind."""
    scale: int = 0
    """A decimal's places after the point."""
    enum: bool = False
    """Whether its values are an `Enum`'s members, whose values the column holds."""
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
    (int, "int64"),
    (float, "float64"),
    (Decimal, "decimal"),
    (date, "date32"),
    (UUID, "utf8"),
    (str, "utf8"),
)


def rows(response: object, route: str) -> Rows:
    """The rows of the dataset `route` answers with `response`, after checking that each is a flat row of columns
    Arrow holds."""
    kind = strict.bare(response)
    arguments = [argument for argument in get_args(kind) if argument is not Ellipsis]
    model = strict.bare(arguments[0]) if get_origin(kind) in {list, tuple} and len(arguments) == 1 else None
    if not (isinstance(model, type) and issubclass(model, BaseModel)) or issubclass(model, RootModel):
        msg = f"{route} declares a dataset, and answers {strict.named(kind)}: a dataset is rows, so answer list[Row], Row a model whose fields are its columns"
        raise ServiceError(msg)
    if model.model_config.get("extra") == "allow":
        msg = f"{route}'s dataset rows, {model.__qualname__}, allow keys beyond their fields, which no column holds: drop extra='allow'"
        raise ServiceError(msg)
    if model.model_computed_fields:
        msg = f"{route}'s dataset rows, {model.__qualname__}, compute {', '.join(model.model_computed_fields)}, which no column holds: make each a field"
        raise ServiceError(msg)
    columns = tuple(_column(route, model, name, field) for name, field in model.model_fields.items() if field.exclude is not True)
    return Rows(model, columns, TypeAdapter(cast("type[Sequence[BaseModel]]", kind)))  # a value's type, which ty cannot read as one


def _column(route: str, model: type[BaseModel], name: str, field: FieldInfo) -> Column:
    where = f"{route}'s dataset column {model.__qualname__}.{name}"
    annotation, nullable, metadata = _unwrapped(field.annotation, list(field.metadata))
    key = field.serialization_alias or field.alias or name
    if (choice := _choice(annotation)) is not None:
        kind, is_enum = choice
        return Column(key, name, kind, nullable, enum=is_enum)
    kind: Kind | None = "timestamp" if annotation is strict.AWARE else next((found for scalar, found in _KINDS if isinstance(annotation, type) and issubclass(annotation, scalar)), None)
    if kind is None:
        msg = f"{where} is {strict.named(annotation)}, which Arrow's columns do not hold: give it str, int, float, bool, Decimal, date, AwareDatetime, UUID, a Literal or an Enum, or T | None"
        raise ServiceError(msg)
    if kind != "decimal":
        return Column(key, name, kind, nullable, text=isinstance(annotation, type) and issubclass(annotation, UUID))
    digits = next((value for found in metadata if isinstance(value := getattr(found, "max_digits", None), int)), None)
    places = next((value for found in metadata if isinstance(value := getattr(found, "decimal_places", None), int)), None)
    if digits is None or places is None or not 0 <= places <= digits <= MAX_DIGITS:
        example = "Annotated[Decimal, Field(max_digits=18, decimal_places=2)]"
        msg = f"{where} is a decimal, which a column holds with one precision and one scale: declare them, as {example}, with at most {MAX_DIGITS} digits"
        raise ServiceError(msg)
    return Column(key, name, kind, nullable, precision=digits, scale=places)


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


def _choice(annotation: object) -> tuple[Kind, bool] | None:
    """The column of a `Literal` or an `Enum` whose values are all strings or all integers, and whether it is an
    `Enum`; None for any other type."""
    if get_origin(annotation) is Literal:
        values, is_enum = list(get_args(annotation)), False
    elif isinstance(annotation, type) and issubclass(annotation, Enum):
        values, is_enum = [member.value for member in annotation], True
    else:
        return None
    if all(isinstance(value, str) for value in values):
        return "utf8", is_enum
    if all(isinstance(value, int) and not isinstance(value, bool) for value in values):
        return "int64", is_enum
    return None
