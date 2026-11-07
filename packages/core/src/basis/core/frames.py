"""A frame's columns as pandas holds them and Arrow stores them: the one table of the type each kind of column has,
the conversions to it that lose nothing, and a frame to Arrow and back. Pure: it converts values, and reads and writes
nothing.

Every column of a frame that basis hands code, or takes from it, is backed by Arrow, `pd.ArrowDtype(TYPE)`:

| Kind | `TYPE` |
|---|---|
| `int` | `pa.int64()` |
| `float` | `pa.float64()` |
| `decimal` | `pa.decimal128(precision, scale)` |
| `str` | `pa.string()` |
| `bool` | `pa.bool_()` |
| `date` | `pa.date32()` |
| `datetime` | `pa.timestamp("us", "UTC")` |

So an integer with nulls stays an integer, a decimal stays exact, a datetime keeps its zone, which in a frame is
always UTC, text is Arrow's, and no column is ever `object`. Arrow's types throughout, rather than pandas' nullable
numpy-backed ones (`Int64`, `boolean`, `string[pyarrow]`, `datetime64[us, UTC]`), because those have no exact decimal
and no date, so a frame of them would mix three kinds of column, each with its own null; because a frame arrives as
Arrow, from the drivers and Parquet, and leaves as Arrow, to the drivers and the run store, and an Arrow-backed frame
shares its buffers both ways; and because `.dt.date` stays a typed date, where on `datetime64` it is `object`.
Measured on a million trades through basis-job's example transform, the two took the same time, 0.58 s; the frame
held 89 MB against 106 MB, and its load from Arrow copied nothing, against 65 MB.

This is the one module of basis's pure code that imports pyarrow, for its types and casts and never its files: the
import check (`basis.core.testing.import_chains`) discounts this import alone, so code it checks that imports pyarrow
itself is still caught.

`convert` changes a column's values to its kind's type only where nothing is lost. pyarrow's safe casts refuse a
decimal rounded to fewer places, a decimal or float truncated to an integer, an integer too large for its type, and a
time finer than a microsecond; the rest it would allow, a time of day dropped from a date or an integer other than 0
or 1 made a boolean, are checked here."""

from collections.abc import Callable, Sequence
from decimal import Decimal, localcontext
from typing import Any, Literal, cast

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

from basis.core.tables import Column, Kind

_TYPES: dict[Kind, pa.DataType] = {
    "int": pa.int64(), "float": pa.float64(), "str": pa.string(), "bool": pa.bool_(), "date": pa.date32(), "datetime": pa.timestamp("us", "UTC"),
}  # fmt: skip
"""Each kind's Arrow type, but a decimal's, which its precision and scale make."""

type Naive = Literal["in_zone", "reject"]
"""What a naive datetime in a frame means: `in_zone`, a value in the column's zone, as a source gives it; `reject`, a
mistake, as in a transform's output, where every datetime is UTC."""
type Array = pa.Array | pa.ChunkedArray


def arrow_type(column: Column) -> pa.DataType:
    """The Arrow type of `column`'s values: the table above."""
    if column.kind == "decimal":
        if column.precision is None or column.scale is None:
            msg = f"decimal column {column.name} has no precision and scale"
            raise ValueError(msg)
        return pa.decimal128(column.precision, column.scale)
    return _TYPES[column.kind]


def dtype(column: Column) -> pd.ArrowDtype:
    """The pandas dtype of `column` in a frame: `pd.ArrowDtype` of its Arrow type."""
    return pd.ArrowDtype(arrow_type(column))


def empty(columns: Sequence[Column]) -> pd.DataFrame:
    """A frame of `columns` with no rows."""
    return pd.DataFrame({column.name: _series(pa.array([], arrow_type(column)), column.name) for column in columns})


def to_arrow(frame: pd.DataFrame) -> pa.Table:
    """A frame of declared columns as Arrow, with their schema: each column's type is its dtype's, which converting it
    to its column (`convert`) made the declared one; without the frame's index (`preserve_index=False`), which holds no
    data; and without pandas' metadata, which names pandas' version. So the same rows are always the same table, and
    the same object in the run store. A column of any other dtype is refused, since its Arrow type would be guessed."""
    if others := [f"{name} ({found})" for name, found in frame.dtypes.items() if not isinstance(found, pd.ArrowDtype)]:
        msg = f"columns that are not Arrow-backed, so have no declared type: {', '.join(others)}: conform the frame to its row first"
        raise TypeError(msg)
    schema = pa.schema([(str(name), cast("pd.ArrowDtype", found).pyarrow_dtype) for name, found in frame.dtypes.items()])
    table = pa.Table.from_pandas(frame, schema=schema, preserve_index=False)
    return cast("pa.Table", table.replace_schema_metadata(None))  # pyarrow is untyped


def from_arrow(table: pa.Table) -> pd.DataFrame:
    """`table` as a frame of Arrow-backed columns, each of the table's own type, sharing its buffers; its index counts
    the rows, whatever pandas' metadata the table carries."""
    return cast("pd.DataFrame", table.to_pandas(types_mapper=pd.ArrowDtype, ignore_metadata=True))  # pyarrow is untyped


def convert(series: pd.Series, column: Column, naive: Naive) -> tuple[pd.Series, str | None]:
    """`series` as `column`'s type, named as the column, or why it cannot become it without loss. Any dtype converts:
    Arrow's, numpy's, whose NaN is pandas' null, and `object`, whose Python values Arrow types first. Text converts to
    every kind, as a CSV's does; an integer type to an integer, a decimal, or a float; a decimal to a decimal that keeps
    every digit, to an integer where it has no fraction, or to a float; a float only to a float; a datetime at
    midnight to a date; 0 and 1 to a boolean; a datetime to UTC, a naive one read in its column's zone unless `naive`
    is `reject`."""
    try:
        array = _array(series)
    except (pa.ArrowInvalid, pa.ArrowTypeError) as error:
        return series, f"values Arrow cannot type as one column: {_first_line(error)}"
    target = arrow_type(column)
    if pa.types.is_dictionary(array.type):
        array = pc.cast(array, array.type.value_type)
    try:
        if array.type == target:
            converted, problem = array, None
        elif pa.types.is_null(array.type):
            converted, problem = pa.nulls(len(array), target), None
        elif _is_text(array.type):
            converted, problem = _PARSERS[column.kind](_call("if_else", _call("equal", array, pa.scalar("", array.type)), pa.scalar(None, array.type), array), column)
        else:
            converted, problem = _CONVERTERS[column.kind](array, column, naive)
    except (pa.ArrowInvalid, pa.ArrowNotImplementedError) as error:
        return series, _first_line(error)
    if problem is None and converted is not None and column.kind == "float" and not _all(_call("is_finite", converted)):
        problem = "NaN or infinite values"
    if problem is not None or converted is None:
        return series, problem
    return _series(converted, column.name), None


_ASTRAL = r"[\x{10000}-\x{10FFFF}]"
"""A character outside Unicode's Basic Multilingual Plane, which UTF-16 writes as two units, as an emoji: in RE2's
syntax, which Arrow's regular expressions are."""


def utf16_units(values: pd.Series) -> pd.Series:
    """The length of each string in UTF-16 units, as SQL Server measures an `nvarchar`: its characters, and one more for
    each outside the Basic Multilingual Plane. Indexed as `values` is."""
    text = _array(values)
    units = _call("add", _call("utf8_length", text), _call("count_substring_regex", text, options=pc.MatchSubstringOptions(_ASTRAL)))
    return _indexed(pc.cast(units, pa.int64()), values)


def utf8_bytes(values: pd.Series) -> pd.Series:
    """The length of each string in UTF-8's bytes. Indexed as `values` is."""
    return _indexed(pc.cast(_call("binary_length", _array(values)), pa.int64()), values)


def ascii_only(values: pd.Series) -> pd.Series:
    """Whether each string is ASCII alone; a null is null. Indexed as `values` is."""
    return _indexed(_call("string_is_ascii", _array(values)), values)


def _indexed(array: Array, like: pd.Series) -> pd.Series:
    return pd.Series(pd.arrays.ArrowExtensionArray(array), index=like.index, name=like.name, copy=False)


def local(values: Array, zone: str) -> Array:
    """Each instant as a naive time in `zone`, as a datetime column stores it."""
    return _call("local_timestamp", pc.cast(values, pa.timestamp("us", zone)))


def _call(function: str, *args: object, options: object = None) -> Any:  # noqa: ANN401 -- pyarrow is untyped
    """A pyarrow compute function, by name: `pyarrow.compute` makes most of its functions as it loads, so a type
    checker sees only `call_function`."""
    return pc.call_function(function, list(args), options)


def _array(series: pd.Series) -> Array:
    return pa.array(series, from_pandas=True)


def _series(array: Array, name: str) -> pd.Series:
    return pd.Series(pd.arrays.ArrowExtensionArray(array), name=name, copy=False)


def _is_text(found: pa.DataType) -> bool:
    return bool(pa.types.is_string(found) or pa.types.is_large_string(found) or pa.types.is_string_view(found))


def _all(mask: Array) -> bool:
    """Whether every non-null value of a boolean array is true."""
    return bool(_call("all", mask, options=pc.ScalarAggregateOptions(skip_nulls=True)).as_py() is not False)


def _all_in(array: Array, values: pa.Array) -> bool:
    """Whether every non-null value of `array` is one of `values`."""
    return _all(_call("or_kleene", _call("is_null", array), _is_in(array, values)))


def _is_in(array: Array, values: pa.Array) -> Array:
    return _call("is_in", array, options=pc.SetLookupOptions(values))


def _count(mask: Array) -> int:
    return int(_call("sum", pc.cast(mask, pa.int64()), options=pc.ScalarAggregateOptions(skip_nulls=True)).as_py() or 0)


type _Converted = tuple[Array | None, str | None]
type _Converter = Callable[[Array, Column, Naive], _Converted]
type _Parser = Callable[[Array, Column], _Converted]


def _numeric(array: Array, column: Column, _naive: Naive) -> _Converted:
    """A numeric cast that loses nothing: pyarrow's safe cast refuses a value that would round or truncate, and the
    values it would change are then counted."""
    found, target = array.type, arrow_type(column)
    if not (pa.types.is_integer(found) or pa.types.is_floating(found) or pa.types.is_decimal(found)):
        return None, f"{found} cannot become {target}"
    if column.kind != "float" and pa.types.is_floating(found):
        return None, f"{found} is inexact: convert it to {target} in the transform, rounding as the data needs"
    try:
        if column.kind == "decimal" and pa.types.is_integer(found):
            # Arrow casts an integer only to a decimal with room for every value of its type, then narrows it safely.
            return pc.cast(pc.cast(array, pa.decimal128(20, 0)), target), None
        return pc.cast(array, target), None
    except pa.ArrowInvalid:
        return None, f"{_changed(array, column)} of {len(array)} values change when stored as {target}"


def _changed(array: Array, column: Column) -> int:
    """How many of the values a cast to `column` refused would change, counted one by one, only once it has failed."""
    return sum(1 for value in array.to_pylist() if value is not None and not _keeps(value, column))


def _keeps(value: float | Decimal, column: Column) -> bool:
    match column.kind:
        case "int":
            return value == int(value) and -(2**63) <= value < 2**63
        case "decimal" if column.precision is not None and column.scale is not None:
            exact = Decimal(value)
            # Room for every digit the value keeps at the column's scale, and one a rounding carries: Python's default
            # context holds 28, fewer than a decimal of 38 digits needs.
            with localcontext(prec=max(exact.adjusted() + 1, 1) + column.scale + 1):
                return exact == exact.quantize(Decimal(1).scaleb(-column.scale)) and abs(exact) < 10 ** (column.precision - column.scale)
        case _:
            return float(value) == value


def _boolean(array: Array, column: Column, _naive: Naive) -> _Converted:
    if not pa.types.is_integer(array.type):
        return None, f"{array.type} cannot become {arrow_type(column)}"
    if not _all_in(array, pa.array([0, 1], array.type)):
        return None, f"{array.type} values other than 0 and 1 are not booleans"
    return pc.cast(array, pa.bool_()), None


def _date(array: Array, column: Column, _naive: Naive) -> _Converted:
    """Oracle's DATE holds a time of day too: a value at midnight is a date, and one with a time is not. A datetime
    with a zone is a date where it is midnight in its own zone, and the date is the one there."""
    if pa.types.is_date(array.type):
        as_date = pc.cast(array, pa.date32())
    elif pa.types.is_timestamp(array.type):
        local = _call("local_timestamp", array) if array.type.tz is not None else array
        as_date = pc.cast(local, pa.date32())
        if not _all(_call("equal", pc.cast(as_date, local.type), local)):
            return None, "datetimes with a time of day are not dates"
    else:
        return None, f"{array.type} cannot become {arrow_type(column)}"
    return as_date, None


def _to_utc(array: Array, column: Column, naive: Naive) -> _Converted:
    found = array.type
    if not pa.types.is_timestamp(found):
        return None, f"{found} is not a datetime"
    if found.tz is None:
        if naive == "reject" or column.zone is None:
            return None, f"naive datetimes: give them a zone, as .dt.tz_localize({column.zone!r}), or convert them to UTC"
        array = _call("assume_timezone", _microseconds(array), options=pc.AssumeTimezoneOptions(column.zone, ambiguous="raise", nonexistent="raise"))
    return pc.cast(_microseconds(array), arrow_type(column)), None


def _microseconds(array: Array) -> Array:
    """A timestamp in microseconds, the finest a datetime column keeps; a finer value is refused, never truncated."""
    found = array.type
    try:
        return pc.cast(array, pa.timestamp("us", found.tz))
    except pa.ArrowInvalid as error:
        msg = "times finer than a microsecond, which a datetime column does not keep"
        raise pa.ArrowInvalid(msg) from error


def _not_convertible(array: Array, column: Column, _naive: Naive) -> _Converted:
    return None, f"{array.type} cannot become {arrow_type(column)}"


def _parse_text(text: Array, _column: Column) -> _Converted:
    return pc.cast(text, pa.string()), None


def _parse_boolean(text: Array, _column: Column) -> _Converted:
    lowered = _call("utf8_lower", text)
    if not _all_in(lowered, pa.array(["true", "false", "1", "0"])):
        return None, "text other than true, false, 1 or 0 is not a boolean"
    return _call("if_else", _call("is_null", lowered), pa.scalar(None, pa.bool_()), _is_in(lowered, pa.array(["true", "1"]))), None


def _parse_decimal(text: Array, column: Column) -> _Converted:
    """Text with more decimal places than the scale is refused as such, before Arrow's cast refuses it too."""
    if not _all(_matches(text, rf"^[+-]?\d+(\.\d{{0,{column.scale}}})?$")):
        return None, f"text that is not a number with at most {column.scale} decimal places"
    return pc.cast(text, arrow_type(column)), None


def _matches(text: Array, pattern: str) -> Array:
    return _call("match_substring_regex", text, options=pc.MatchSubstringOptions(pattern))


def _parse_number(text: Array, column: Column) -> _Converted:
    return pc.cast(text, arrow_type(column)), None


def _parse_date(text: Array, column: Column) -> _Converted:
    if not _all(_matches(text, r"^\d{4}-\d{2}-\d{2}$")):
        return None, "text that is not a date, as YYYY-MM-DD"
    return pc.cast(text, arrow_type(column)), None


def _parse_instant(text: Array, column: Column) -> _Converted:
    """ISO 8601 text: with an offset, an instant; without one, a time in the column's zone."""
    with_offset = _matches(text, r"([+-]\d{2}:?\d{2}|Z)$")
    aware = _count(with_offset)
    if aware == len(text) - text.null_count:
        return pc.cast(text, arrow_type(column)), None
    if aware:
        return None, "some values carry an offset and some do not"
    return _to_utc(pc.cast(text, pa.timestamp("us")), column, "in_zone")


_CONVERTERS: dict[Kind, _Converter] = {"int": _numeric, "float": _numeric, "decimal": _numeric, "bool": _boolean, "date": _date, "datetime": _to_utc, "str": _not_convertible}
_PARSERS: dict[Kind, _Parser] = {"str": _parse_text, "bool": _parse_boolean, "decimal": _parse_decimal, "int": _parse_number, "float": _parse_number, "date": _parse_date, "datetime": _parse_instant}


def _first_line(error: Exception) -> str:
    return str(error).strip().splitlines()[0]
