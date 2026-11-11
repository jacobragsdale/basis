"""A frame's columns as pandas holds them and Arrow stores them: the one table of the type each kind of column has,
the conversions to it that lose nothing, and a frame to Arrow and back. Pure: it converts values, and reads and writes
nothing.

| Kind | In a frame | In Arrow |
|---|---|---|
| `float` | numpy's `float64`, NaN where it is missing | `pa.float64()`, null where it is missing |
| `str` | `pd.ArrowDtype(pa.string())` | `pa.string()` |
| `bool` | `pd.ArrowDtype(pa.bool_())` | `pa.bool_()` |
| `date` | `pd.ArrowDtype(pa.date32())` | `pa.date32()` |
| `datetime` | `pd.ArrowDtype(pa.timestamp("us", "UTC"))` | `pa.timestamp("us", "UTC")` |

Every number is a float, numpy's, as the firm's quants and pandas itself take one, so arithmetic, `np.where` and every
library work on it as they expect; an amount or a count is a float, and an identifier is text. A datetime keeps its
zone, which in a frame is always UTC, text is Arrow's, and no column is ever `object`. The rest are Arrow-backed
because a frame arrives as Arrow, from the drivers and Parquet, and leaves as Arrow, to the drivers and the run store,
and an Arrow-backed column shares its buffers both ways, and because `.dt.date` stays a typed date, where on
`datetime64` it is `object`.

A number becomes a float through the nearest double to it, every time: an integer exactly, while it is a whole number
of 2^53 or less, beyond which a float holds it only roughly, so it is refused, naming its column; a decimal and a
float32 through their shortest text, since Arrow's direct cast of a decimal misses the nearest double (2.675 becomes
2.6750000000000003) and a float32's widening keeps its binary error (0.1 becomes 0.10000000149011612). A driver's read
is given every number so (`floats`), and so is a frame `convert` conforms.

This is the one module of basis's pure code that imports pyarrow, for its types and casts and never its files: the
import check (`basis.core.testing.import_chains`) discounts this import alone, so code it checks that imports pyarrow
itself is still caught.

`convert` changes a column's values to its kind's type only where nothing is lost but a number's rounding to the
nearest double. pyarrow's safe casts refuse a time finer than a microsecond; the rest they would allow, a time of day
dropped from a date or an integer other than 0 or 1 made a boolean, are checked here."""

from collections.abc import Callable, Sequence
from typing import Any, Literal, cast

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

from basis.core.errors import BasisError
from basis.core.tables import Column, Kind

_TYPES: dict[Kind, pa.DataType] = {"float": pa.float64(), "str": pa.string(), "bool": pa.bool_(), "date": pa.date32(), "datetime": pa.timestamp("us", "UTC")}
"""Each kind's Arrow type."""
FLOAT = np.dtype("float64")
"""A number's dtype in a frame."""
WHOLE = 2**53
"""The largest whole number a float holds exactly, and every one below it."""

type Naive = Literal["in_zone", "reject"]
"""What a naive datetime in a frame means: `in_zone`, a value in the column's zone, as a source gives it; `reject`, a
mistake, as in a transform's output, where every datetime is UTC."""
type Array = pa.Array | pa.ChunkedArray


class NumberError(BasisError, ValueError):
    """A column of whole numbers a float cannot hold exactly."""


def arrow_type(column: Column) -> pa.DataType:
    """The Arrow type of `column`'s values: the table above."""
    return _TYPES[column.kind]


def dtype(column: Column) -> pd.ArrowDtype | np.dtype:
    """The pandas dtype of `column` in a frame: numpy's float64 for a number, and `pd.ArrowDtype` of its Arrow type
    for the rest."""
    return FLOAT if column.kind == "float" else pd.ArrowDtype(arrow_type(column))


def empty(columns: Sequence[Column]) -> pd.DataFrame:
    """A frame of `columns` with no rows."""
    return pd.DataFrame({column.name: _series(pa.array([], arrow_type(column)), column.name) for column in columns})


def to_arrow(frame: pd.DataFrame) -> pa.Table:
    """A frame of declared columns as Arrow, with their schema: each column's type is its dtype's, which converting it
    to its column (`convert`) made the declared one; without the frame's index (`preserve_index=False`), which holds no
    data; and without pandas' metadata, which names pandas' version. So the same rows are always the same table, and
    the same object in the run store. A float's NaN is a null in Arrow, so a missing number is one value however it was
    made. A column of any other dtype is refused, since its Arrow type would be guessed."""
    if others := [f"{name} ({found})" for name, found in frame.dtypes.items() if not isinstance(found, pd.ArrowDtype) and found != FLOAT]:
        msg = f"columns that are neither floats nor Arrow-backed, so have no declared type: {', '.join(others)}: conform the frame to its row first"
        raise TypeError(msg)
    schema = pa.schema([(str(name), pa.float64() if found == FLOAT else cast("pd.ArrowDtype", found).pyarrow_dtype) for name, found in frame.dtypes.items()])
    table = pa.Table.from_pandas(frame, schema=schema, preserve_index=False)
    return cast("pa.Table", table.replace_schema_metadata(None))  # pyarrow is untyped


def from_arrow(table: pa.Table) -> pd.DataFrame:
    """`table` as a frame: each float64 column numpy's, its nulls NaN, and every other column Arrow-backed, of the
    table's own type, sharing its buffers; its index counts the rows, whatever pandas' metadata the table carries."""
    return cast("pd.DataFrame", table.to_pandas(types_mapper=_mapped, ignore_metadata=True))  # pyarrow is untyped


def _mapped(found: pa.DataType) -> pd.ArrowDtype | None:
    """A column's pandas dtype from Arrow: None, numpy's own, for a float64, and Arrow's for the rest."""
    return None if found == pa.float64() else pd.ArrowDtype(found)


def floats(table: pa.Table, *, what: str) -> pa.Table:
    """`table` with every numeric column a float64, each value the nearest double to it, as a read gives its numbers to
    code; `NumberError` names a column of whole numbers beyond 2^53, which a float holds only roughly."""
    for index, name in enumerate(table.column_names):
        values = table.column(index)
        if values.type != pa.float64() and _is_number(values.type):
            converted, problem = _to_float(values)
            if converted is None:
                msg = f"{what}: column {name} holds {problem}: select it as text if it names something, as CAST({name} AS VARCHAR(20)), or CAST it AS FLOAT if it is an amount"
                raise NumberError(msg)
            table = table.set_column(index, name, converted)
    return table


def _is_number(found: pa.DataType) -> bool:
    return bool(pa.types.is_integer(found) or pa.types.is_floating(found) or pa.types.is_decimal(found))


def _to_float(values: Array) -> _Converted:
    """`values`, a numeric type's, as the nearest doubles to them, or why some cannot be: whole numbers beyond 2^53. A
    decimal and a float32 go through their shortest text, which Arrow parses to the nearest double, and its direct
    cast does not."""
    found = values.type
    if pa.types.is_integer(found) or (pa.types.is_decimal(found) and found.scale == 0):
        low, high = (_call(name, values).as_py() for name in ("min", "max"))
        if (low is not None and low < -WHOLE) or (high is not None and high > WHOLE):
            return None, "whole numbers beyond 2^53, which a float holds only roughly"
        if pa.types.is_integer(found):
            return pc.cast(values, pa.float64()), None
    if pa.types.is_decimal(found) or pa.types.is_floating(found):
        return pc.cast(pc.cast(values, pa.string()), pa.float64()), None
    return None, f"{found} cannot become double"


def convert(series: pd.Series, column: Column, naive: Naive) -> tuple[pd.Series, str | None]:
    """`series` as `column`'s type, named as the column, or why it cannot become it without loss. Any dtype converts:
    Arrow's, numpy's, whose NaN is pandas' null, and `object`, whose Python values Arrow types first. Text converts to
    every kind, as a CSV's does; any number to a float, the nearest double to it, but whole numbers beyond 2^53; a
    float that is infinite is refused; a datetime at midnight to a date; 0 and 1 to a boolean; a datetime to UTC, a
    naive one read in its column's zone unless `naive` is `reject`."""
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
    if problem is None and converted is not None and column.kind == "float" and _any(_call("is_inf", converted)):
        problem = "infinite values"
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
    """A column as a frame holds it: a float64 as numpy's, its nulls NaN, and the rest Arrow-backed."""
    if array.type == pa.float64():
        return pd.Series(array.to_numpy(zero_copy_only=False), name=name, dtype=FLOAT)
    return pd.Series(pd.arrays.ArrowExtensionArray(array), name=name, copy=False)


def _is_text(found: pa.DataType) -> bool:
    return bool(pa.types.is_string(found) or pa.types.is_large_string(found) or pa.types.is_string_view(found))


def _all(mask: Array) -> bool:
    """Whether every non-null value of a boolean array is true."""
    return bool(_call("all", mask, options=pc.ScalarAggregateOptions(skip_nulls=True)).as_py() is not False)


def _any(mask: Array) -> bool:
    """Whether any non-null value of a boolean array is true."""
    return bool(_call("any", mask, options=pc.ScalarAggregateOptions(skip_nulls=True)).as_py())


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


def _floating(array: Array, _column: Column, _naive: Naive) -> _Converted:
    """A number as a float: the nearest double to it (`_to_float`)."""
    converted, problem = _to_float(array)
    if converted is None:
        reason = problem or f"{array.type} is no number"
        return None, f"{reason}: give it as text if it names something, or as a float if it is an amount" if reason.startswith("whole") else reason
    return converted, None


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
        local = _microseconds(array)
        for fold, says in (("ambiguous", "twice, as its clocks go back"), ("nonexistent", "never, as its clocks go forward")):
            other = "nonexistent" if fold == "ambiguous" else "ambiguous"
            try:
                _call("assume_timezone", local, options=pc.AssumeTimezoneOptions(column.zone, **{fold: "raise", other: "earliest"}))
            except pa.ArrowInvalid:
                if getattr(column, fold) == "raise":
                    return None, (
                        f"naive times {column.zone} shows {says}, such as {_unread(local, column.zone, fold)}, which name no one instant:"
                        f" declare how to read them, as etl.Zone({column.zone!r}, {fold}='earliest'), or 'latest'"
                    )
        array = _call("assume_timezone", local, options=pc.AssumeTimezoneOptions(column.zone, ambiguous=column.ambiguous, nonexistent=column.nonexistent))
    return pc.cast(_microseconds(array), arrow_type(column)), None


def _unread(local: Array, zone: str, fold: str) -> str:
    """The first of `local`'s naive times that `zone` shows twice (`ambiguous`) or never (`nonexistent`)."""
    early = _call("assume_timezone", local, options=pc.AssumeTimezoneOptions(zone, ambiguous="earliest", nonexistent="earliest"))
    late = _call("assume_timezone", local, options=pc.AssumeTimezoneOptions(zone, ambiguous="latest", nonexistent="latest"))
    round_trip = _call("local_timestamp", early)
    flags = _call("not_equal", early, late) if fold == "ambiguous" else _call("not_equal", pc.cast(round_trip, local.type), local)
    index = flags.to_pylist().index(True)
    return str(local[index].as_py())


def twice_named(values: pd.Series, zone: str) -> pd.Series:
    """Whether each instant, in a frame of UTC datetimes, is at a time its column's `zone` shows twice, as its clocks
    go back, so its naive value there names two instants. A null is not."""
    array = _microseconds(_array(values))
    if array.type.tz is None:
        msg = "twice_named reads instants: give the values a zone"
        raise ValueError(msg)
    local = _call("local_timestamp", pc.cast(array, pa.timestamp("us", zone)))
    early = _call("assume_timezone", local, options=pc.AssumeTimezoneOptions(zone, ambiguous="earliest", nonexistent="earliest"))
    late = _call("assume_timezone", local, options=pc.AssumeTimezoneOptions(zone, ambiguous="latest", nonexistent="latest"))
    flags = _call("coalesce", _call("not_equal", early, late), pa.scalar(value=False))
    return pd.Series(flags.to_pylist(), index=values.index, dtype=bool)


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


_CONVERTERS: dict[Kind, _Converter] = {"float": _floating, "bool": _boolean, "date": _date, "datetime": _to_utc, "str": _not_convertible}
_PARSERS: dict[Kind, _Parser] = {"str": _parse_text, "bool": _parse_boolean, "float": _parse_number, "date": _parse_date, "datetime": _parse_instant}


def _first_line(error: Exception) -> str:
    return str(error).strip().splitlines()[0]
