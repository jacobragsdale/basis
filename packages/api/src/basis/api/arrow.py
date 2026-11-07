"""A dataset route's answer as an Arrow IPC stream, `application/vnd.apache.arrow.stream`, which is how parallax,
basis-ui's grid, receives its data. `dataset.py` says what a dataset route declares and how each field is a column.

A dataset route answers Arrow when the request's `Accept` names the stream's media type, with a `q` above 0 and at
least as high as JSON's (`application/json`, `application/*` or `*/*`), and JSON otherwise; `Accept:
application/vnd.apache.arrow.stream` alone, as basis-ui's client sends it, asks for Arrow. Either answer says `Vary:
Accept`, and an error is a problem in JSON whichever was asked for. The stream holds one record batch of every row,
with the columns `dataset.Rows` declares, and none when there are no rows: a reader gets the schema either way.

The rows are validated as the route's response, as FastAPI validates a JSON one, before they are written, so a
request's transaction commits only after both, and a value no column holds, such as a time without its offset, is a
defect: a 500, logged once."""

import re
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from enum import Enum

import pyarrow as pa
from pydantic import BaseModel

from basis.api.dataset import Column, Kind, Rows

MEDIA_TYPE = "application/vnd.apache.arrow.stream"
_JSON_RANGES = frozenset({"application/json", "application/*", "*/*"})
_Q = re.compile(r"\s*q\s*=\s*(\S*)\s*", re.IGNORECASE)
_QUALITY = re.compile(r"0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?")
"""A weight, as RFC 9110 writes one: a range with any other `q` counts for nothing."""


def wanted(accept: str | None) -> bool:
    """Whether a request whose `Accept` is `accept` asks a dataset route for Arrow rather than JSON."""
    arrow = json = 0.0
    for entry in (accept or "").split(","):
        media, *parameters = entry.split(";")
        kind = media.strip().lower()
        given = next((found.group(1) for parameter in parameters if (found := _Q.fullmatch(parameter)) is not None), "1")
        quality = float(given) if _QUALITY.fullmatch(given) else 0.0
        if kind == MEDIA_TYPE:
            arrow = max(arrow, quality)
        elif kind in _JSON_RANGES:
            json = max(json, quality)
    return arrow > 0 and arrow >= json


def schema(rows: Rows) -> pa.Schema:
    """The stream's schema: a field for each column, nullable only where its model's field may be None."""
    return pa.schema([pa.field(column.name, _type(column), nullable=column.nullable) for column in rows.columns])


def encode(rows: Rows, values: Sequence[BaseModel]) -> bytes:
    """`values`, each a row of `rows.model`, as an Arrow IPC stream."""
    declared = schema(rows)
    arrays = [pa.array([_value(column, getattr(value, column.attribute)) for value in values], type=field.type) for column, field in zip(rows.columns, declared, strict=True)]
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, declared) as writer:
        if values:
            writer.write_table(pa.Table.from_arrays(arrays, schema=declared))
    return bytes(sink.getvalue())


def _type(column: Column) -> pa.DataType:
    return pa.decimal128(column.precision, column.scale) if column.kind == "decimal" else _TYPES[column.kind]


_TYPES: dict[Kind, pa.DataType] = {"utf8": pa.string(), "int64": pa.int64(), "float64": pa.float64(), "bool": pa.bool_(), "date32": pa.date32(), "timestamp": pa.timestamp("us", tz="UTC")}


def _value(column: Column, value: object) -> object:
    """`value` as its column holds it: an `Enum` member by its value, a UUID as its text, and a time with its offset
    as the instant it is."""
    if value is None:
        return None
    if column.enum and isinstance(value, Enum):
        return value.value
    if column.kind == "utf8" and not isinstance(value, str):
        return str(value)
    if isinstance(value, datetime) and value.tzinfo is None:
        msg = f"the dataset column {column.name} holds the time {value.isoformat()}, which has no offset: give each time its offset, as datetime.now(UTC) does"
        raise TypeError(msg)
    if isinstance(value, Decimal) and not value.is_finite():
        msg = f"the dataset column {column.name} holds {value}, which no decimal column holds"
        raise TypeError(msg)
    return value
