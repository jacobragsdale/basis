"""A dataset route's answer as an Arrow IPC stream, `application/vnd.apache.arrow.stream`, which is how parallax,
basis-ui's grid, receives its data. `dataset.py` says what a dataset route declares and how each field is a column.

A dataset route answers Arrow when the request's `Accept` names the stream's media type, with a `q` above 0 and at
least as high as JSON's (`application/json`, `application/*` or `*/*`), and JSON otherwise; `Accept:
application/vnd.apache.arrow.stream` alone, as basis-ui's client sends it, asks for Arrow. Either answer says `Vary:
Accept`, and an error is a problem in JSON whichever was asked for. The stream holds the rows in their order, in record
batches of at most `BATCH_ROWS` rows each, with the columns `dataset.Rows` declares, and no batch when there are no
rows: a reader gets the schema either way.

The rows are validated as the route's response, by one adapter, as FastAPI validates a JSON one, objects' attributes
included, and dumped once, before they are written, so a request's transaction commits only after both, and a value no
column holds is a defect: a 500, logged once."""

import json
import math
import re
from collections.abc import Mapping, Sequence
from enum import Enum

import pyarrow as pa
import pyarrow.compute as pc
import pydantic_core

from basis.api.dataset import Column, Kind, Rows
from basis.api.format import METADATA
from basis.core import frames

MEDIA_TYPE = "application/vnd.apache.arrow.stream"
_JSON_RANGES = frozenset({"application/json", "application/*", "*/*"})
_Q = re.compile(r"\s*q\s*=\s*(\S*)\s*", re.IGNORECASE)
_QUALITY = re.compile(r"0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?")
"""A weight, as RFC 9110 writes one: a range with any other `q` counts for nothing."""
BATCH_ROWS = 65_536
"""The most rows a record batch of a stream holds, which the contract promises a UI. A reader has an IPC message only
once all of it has arrived, so one batch of every row would keep a grid empty until the last byte, and hold the table
twice while it is read; this is the size parallax's budgets were measured at."""


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
    """The stream's schema: a field for each column, nullable where its model's field may be None, and every float."""
    return pa.schema([pa.field(column.name, _type(column), nullable=column.nullable, metadata=_metadata(column)) for column in rows.columns])


def _metadata(column: Column) -> dict[bytes, bytes] | None:
    """A column's Arrow field metadata: its display format, as `basis.format`, the contract's object as JSON."""
    if column.format is None:
        return None
    return {METADATA.encode(): json.dumps(column.format.as_json(), separators=(",", ":")).encode()}


def encode(rows: Rows, values: Sequence[Mapping[str, object]]) -> bytes:
    """`values`, rows of `rows.model` as its adapter dumps them, by their columns' names, as an Arrow IPC stream."""
    declared = schema(rows)
    if converted := [column for column in rows.columns if column.enum or column.text or column.kind == "float64"]:  # what pyarrow does not convert itself
        values = [{**row, **{column.name: _value(column, row.get(column.name)) for column in converted}} for row in values]
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, declared) as writer:
        if values:
            writer.write_table(pa.Table.from_pylist(list(values), schema=declared), max_chunksize=BATCH_ROWS)
    return bytes(sink.getvalue())


def _type(column: Column) -> pa.DataType:
    return _TYPES[column.kind]


_TYPES: dict[Kind, pa.DataType] = {"utf8": pa.string(), "float64": pa.float64(), "bool": pa.bool_(), "date32": pa.date32(), "timestamp": pa.timestamp("us", tz="UTC")}


def _value(column: Column, value: object) -> object:
    """`value` as its column holds it: an `Enum` member by its value, a UUID as its text, and NaN or ±inf as null, as
    every number on the wire is (design.md, "Numbers")."""
    if value is None:
        return None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Enum):
        return value.value
    return str(value) if column.text else value


class TableError(ValueError):
    """A table a route answers, or a query's, that does not hold its row's columns: a defect, a 500."""


def conform(rows: Rows, found: object, what: str) -> pa.Table:
    """`found`, a pandas frame or an Arrow table, as the Arrow table of `rows`'s columns, checked once: each column by
    its row's field's name or its JSON name, in any case a database gives it; numbers as floats, each the nearest
    double, through text for a decimal; NaN and ±inf as null; a column that may not be null holding none; a `Literal`'s
    or an `Enum`'s column holding only its values, a UUID's only UUIDs, in lower case, and a bool's only booleans, or 1
    and 0; and every time with its zone, in UTC. A column missing, one the row does not declare, or one of another type raises
    `TableError`, naming it."""
    table = _arrow(found, what)
    declared = schema(rows)
    names = {name.lower(): name for name in table.column_names}
    if len(names) != len(table.column_names):
        msg = f"{what} names a column twice, in any case: {table.column_names}"
        raise TableError(msg)
    wanted = {column.attribute.lower(): column for column in rows.columns} | {column.name.lower(): column for column in rows.columns}
    if extra := sorted(name for lowered, name in names.items() if lowered not in wanted):
        msg = f"{what} has columns {rows.model.__qualname__} does not declare: {', '.join(extra)}: drop them, or declare them as its fields"
        raise TableError(msg)
    arrays: list[pa.ChunkedArray | pa.Array] = []
    for column, field in zip(rows.columns, declared, strict=True):
        given = names.get(column.name.lower()) or names.get(column.attribute.lower())
        if given is None:
            if table.num_columns == 0 and table.num_rows == 0:  # no rows, as a fake or a query of nothing gives them
                arrays.append(pa.array([], field.type))
                continue
            msg = f"{what} has no column {column.name}, which {rows.model.__qualname__} declares"
            raise TableError(msg)
        arrays.append(_column(table.column(given), column, field, what))
    return pa.Table.from_arrays(arrays, schema=declared)


def _arrow(found: object, what: str) -> pa.Table:
    if isinstance(found, pa.Table):
        return found
    if type(found).__module__.partition(".")[0] == "pandas" and type(found).__name__ == "DataFrame":
        return pa.Table.from_pandas(found, preserve_index=False)
    msg = f"{what} is a {type(found).__qualname__}: return a pandas DataFrame or a pyarrow Table of the row's columns"
    raise TableError(msg)


def _column(values: pa.ChunkedArray, column: Column, field: pa.Field, what: str) -> pa.ChunkedArray:
    where = f"{what}'s column {column.name}"
    if column.kind == "float64":
        if not (pa.types.is_floating(values.type) or pa.types.is_integer(values.type) or pa.types.is_decimal(values.type) or pa.types.is_null(values.type)):
            msg = f"{where} is {values.type}, and the row declares a float"
            raise TableError(msg)
        floats = frames.floats(pa.table({column.name: values}), what=what).column(0) if not pa.types.is_null(values.type) else values.cast(pa.float64())
        if pa.types.is_floating(floats.type) and floats.type != pa.float64():
            floats = pc.cast(pc.cast(floats, pa.string()), pa.float64())  # a float32 through its shortest text, so 0.1 stays 0.1
        return pc.call_function("if_else", [pc.call_function("is_finite", [floats]), floats, pa.scalar(None, pa.float64())])  # by name: pyarrow's stubs list neither
    if column.kind == "timestamp" and pa.types.is_timestamp(values.type) and values.type.tz is None:
        msg = f"{where} holds times without their zone, and every time basis gives carries one: give them a zone, as UTC"
        raise TableError(msg)
    if column.kind == "bool" and pa.types.is_integer(values.type) and (wrong := _outside(values, pa.array([0, 1], values.type))) is not None:
        msg = f"{where} holds {wrong}, and a bool column takes true and false, or 1 and 0"
        raise TableError(msg)
    try:
        cast = values.cast(field.type, safe=True)
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError) as error:
        msg = f"{where} is {values.type}, which is not {field.type}, as the row declares: {error}"
        raise TableError(msg) from error
    if not field.nullable and cast.null_count:
        msg = f"{where} holds {cast.null_count} nulls, and the row's field may not be None"
        raise TableError(msg)
    if column.values is not None and (wrong := _outside(cast, pa.array(sorted(column.values), pa.string()))) is not None:
        msg = f"{where} holds {wrong!r}, which is not one of its values: {', '.join(sorted(column.values))}"
        raise TableError(msg)
    if column.text:
        cast = pc.call_function("utf8_lower", [cast])  # a UUID's text as JSON gives it, whatever case a database gives
        if (wrong := _first(cast, pc.call_function("invert", [pc.call_function("match_substring_regex", [cast], pc.MatchSubstringOptions(_UUID))]))) is not None:
            msg = f"{where} holds {wrong!r}, which is not a UUID in groups of 8-4-4-4-12 hex digits"
            raise TableError(msg)
    return cast


_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


def _outside(values: pa.ChunkedArray, allowed: pa.Array) -> object:
    """The first value of `values` but null that `allowed` does not hold, or None."""
    return _first(values, pc.call_function("invert", [pc.call_function("is_in", [values], pc.SetLookupOptions(allowed))]))  # by name: pyarrow's stubs list neither


def _first(values: pa.ChunkedArray, wrong: pa.ChunkedArray) -> object:
    """The first value but null where `wrong` is true, or None."""
    found = values.filter(pc.fill_null(wrong, False))  # noqa: FBT003 -- the value a null takes
    return found[0].as_py() if len(found) else None


def write(table: pa.Table) -> bytes:
    """`table` as an Arrow IPC stream, in batches of at most `BATCH_ROWS` rows: a chunk of the table that holds fewer
    is a batch as it is."""
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table, max_chunksize=BATCH_ROWS)
    return bytes(sink.getvalue())


def as_json(table: pa.Table) -> bytes:
    """`table` as the JSON a dataset route answers: a list of rows, a time with its offset, a date as its ISO date, and
    null where a value is missing, written by pydantic's serializer with no model to validate."""
    return pydantic_core.to_json(table.to_pylist())
