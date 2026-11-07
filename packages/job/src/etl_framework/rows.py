"""Row schemas. A row is a class whose annotations are a table's columns, in order:

    class Position(etl.Row):
        as_of: date
        account_id: Annotated[str, etl.MaxLength(20)]
        quantity: Annotated[Decimal, etl.Precision(18, 4)]
        updated_at: Annotated[datetime, etl.Zone("UTC")] | None

A column is an `int`, `float`, `Decimal`, `str`, `bool`, `date` or `datetime`, and `| None` when it may be null. A
`Decimal` names its precision and scale, and a `datetime` names the zone its naive values are in, in sources and
destinations alike; in a frame it is always UTC. Nothing is assumed about either.

`conform` is the boundary: every frame that enters or leaves a job passes through it. It converts only where nothing
is lost (an Oracle `NUMBER(10)` to an `int`, a CSV's text to a date) and reports every column it cannot convert,
since polars itself rounds a decimal cast to a smaller scale and truncates a decimal cast to an integer, even when
strict."""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from types import NoneType, UnionType
from typing import Annotated, ClassVar, Literal, Union, get_args, get_origin, get_type_hints
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import polars as pl

MAX_PRECISION = 38
"""The most digits a decimal holds in SQL Server and Oracle alike."""
IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,127}$")
"""A column name: lower-case, so it reads the same unquoted in SQL Server and Oracle, and never needs quoting."""

type Kind = Literal["int", "float", "decimal", "str", "bool", "date", "datetime"]
_KINDS: dict[type, Kind] = {int: "int", float: "float", Decimal: "decimal", str: "str", bool: "bool", date: "date", datetime: "datetime"}


@dataclass(frozen=True)
class MaxLength:
    """The most characters a `str` column holds."""

    chars: int


@dataclass(frozen=True)
class Precision:
    """A `Decimal` column's digits in all, and after the point, as SQL's `DECIMAL(precision, scale)`."""

    precision: int
    scale: int


@dataclass(frozen=True)
class Zone:
    """The IANA time zone a `datetime` column's naive values are in, in its sources and destinations."""

    name: str


@dataclass(frozen=True)
class Column:
    name: str
    kind: Kind
    nullable: bool
    dtype: pl.DataType
    """The column's type in a frame."""
    max_length: int | None = None
    precision: int | None = None
    scale: int | None = None
    zone: str | None = None


_DTYPES: dict[Kind, pl.DataType] = {"int": pl.Int64(), "float": pl.Float64(), "str": pl.String(), "bool": pl.Boolean(), "date": pl.Date(), "datetime": pl.Datetime("us", "UTC")}


class RowError(TypeError):
    """A row class that does not declare its columns as `conform` needs them."""


class Row:
    """Base for a table's columns. Subclass it and annotate one attribute per column; it is never instantiated."""

    columns: ClassVar[tuple[Column, ...]] = ()

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        hints = get_type_hints(cls, include_extras=True)
        cls.columns = tuple(_column(cls, name, hint) for name, hint in hints.items() if get_origin(hint) is not ClassVar)
        if not cls.columns:
            msg = f"{cls.__qualname__} declares no columns: annotate one attribute per column"
            raise RowError(msg)

    @classmethod
    def names(cls) -> list[str]:
        return [column.name for column in cls.columns]

    @classmethod
    def column(cls, name: str) -> Column:
        for column in cls.columns:
            if column.name == name:
                return column
        msg = f"{cls.__qualname__} has no column {name!r}; it has {cls.names()}"
        raise KeyError(msg)

    @classmethod
    def schema(cls) -> pl.Schema:
        return pl.Schema({column.name: column.dtype for column in cls.columns})

    @classmethod
    def empty(cls) -> pl.DataFrame:
        return pl.DataFrame(schema=cls.schema())


def _column(row: type, name: str, hint: object) -> Column:
    where = f"{row.__qualname__}.{name}"
    if not IDENTIFIER.match(name):
        msg = f"{where}: a column name is lower-case letters, digits and underscores, starting with a letter or underscore"
        raise RowError(msg)
    nullable = False
    metadata: list[object] = []
    while True:
        if get_origin(hint) is Annotated:
            base, *extra = get_args(hint)
            metadata.extend(extra)
            hint = base
        elif get_origin(hint) in {Union, UnionType}:
            members = [member for member in get_args(hint) if member is not NoneType]
            if len(members) != 1 or len(get_args(hint)) != 2:  # noqa: PLR2004 -- X | None, nothing wider
                msg = f"{where}: a column is one type, or one type `| None`, not {hint}"
                raise RowError(msg)
            nullable, hint = True, members[0]
        else:
            break
    kind = _KINDS.get(hint) if isinstance(hint, type) else None
    if kind is None:
        msg = f"{where}: {hint} is not a column type; use int, float, Decimal, str, bool, date or datetime"
        raise RowError(msg)
    return _with_metadata(where, name, kind, nullable=nullable, metadata=metadata)


def _with_metadata(where: str, name: str, kind: Kind, *, nullable: bool, metadata: list[object]) -> Column:
    max_length = zone = None
    decimal: Precision | None = None
    for item in metadata:
        match item:
            case MaxLength(chars=chars) if kind == "str" and chars >= 1:
                max_length = chars
            case Precision(precision=p, scale=s) if kind == "decimal" and 1 <= p <= MAX_PRECISION and 0 <= s <= p:
                decimal = item
            case Zone(name=zone_name) if kind == "datetime":
                try:
                    ZoneInfo(zone_name)
                except (ZoneInfoNotFoundError, ValueError) as error:
                    msg = f"{where}: {zone_name!r} is not an IANA time zone, such as 'UTC' or 'America/New_York'"
                    raise RowError(msg) from error
                zone = zone_name
            case _:
                msg = f"{where}: {item!r} does not apply to a {kind} column"
                raise RowError(msg)
    if kind == "decimal":
        if decimal is None:
            msg = f"{where}: a Decimal column names its precision and scale, as Annotated[Decimal, etl.Precision(18, 4)]"
            raise RowError(msg)
        return Column(name, kind, nullable, pl.Decimal(decimal.precision, decimal.scale), precision=decimal.precision, scale=decimal.scale)
    if kind == "datetime" and zone is None:
        msg = f"{where}: a datetime column names the zone its naive values are in, as Annotated[datetime, etl.Zone('UTC')]"
        raise RowError(msg)
    return Column(name, kind, nullable, _DTYPES[kind], max_length=max_length, zone=zone)


class SchemaError(ValueError):
    """A frame that does not match its row: every problem, one per line."""

    def __init__(self, what: str, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(f"{what} does not match its row:\n" + "\n".join(f"  - {problem}" for problem in problems))


type Naive = Literal["in_zone", "reject"]
"""What a naive datetime in a frame means: `in_zone`, a value in the column's zone, as a source gives it; `reject`, a
mistake, as in a transform's output, where every datetime is UTC."""


def conform(frame: pl.DataFrame, row: type[Row], *, what: str, naive: Naive) -> pl.DataFrame:
    """`frame` with exactly `row`'s columns, in order and of its types, or `SchemaError` naming every column that is
    missing, extra, not convertible without loss, null where it may not be, or too long. Column names match without
    regard to case, since Oracle reports them in upper case."""
    return conform_columns(frame, row.columns, what=what, naive=naive)


def conform_columns(frame: pl.DataFrame, columns: Sequence[Column], *, what: str, naive: Naive) -> pl.DataFrame:
    """`conform`, for some of a row's columns: those a delete matches on."""
    by_name = {name.lower(): name for name in frame.columns}
    names = [column.name for column in columns]
    problems: list[str] = []
    if extra := sorted(set(by_name) - set(names)):
        problems.append(f"columns the row does not declare: {', '.join(extra)}")
    if missing := [name for name in names if name not in by_name]:
        problems.append(f"missing columns: {', '.join(missing)}")
    if problems:
        raise SchemaError(what, problems)
    converted: list[pl.Series] = []
    for column in columns:
        series, problem = _convert(frame.get_column(by_name[column.name]).alias(column.name), column, naive)
        if problem is not None:
            problems.append(f"{column.name}: {problem}")
            continue
        problems.extend(f"{column.name}: {found}" for found in _invalid(series, column))
        converted.append(series)
    if problems:
        raise SchemaError(what, problems)
    return pl.DataFrame(converted) if converted else pl.DataFrame()


def _convert(series: pl.Series, column: Column, naive: Naive) -> tuple[pl.Series, str | None]:
    """The series as the column's type, or why it cannot be converted without loss."""
    if series.dtype == column.dtype:
        return series, None
    if series.dtype == pl.Null():
        return series.cast(column.dtype), None
    try:
        if isinstance(series.dtype, pl.String):
            return _PARSERS[column.kind](series.replace("", None), column)
        return _CONVERTERS[column.kind](series, column, naive)
    except pl.exceptions.PolarsError as error:
        return series, _first_line(error)


type _Converter = Callable[[pl.Series, Column, Naive], tuple[pl.Series, str | None]]
type _Parser = Callable[[pl.Series, Column], tuple[pl.Series, str | None]]


def _numeric(series: pl.Series, column: Column, _naive: Naive) -> tuple[pl.Series, str | None]:
    """A numeric cast that loses nothing: polars rounds a decimal to a smaller scale and truncates one to an integer,
    so a value that does not survive the cast back unchanged fails it."""
    if not series.dtype.is_numeric():
        return series, f"{series.dtype} cannot become {column.dtype}"
    if series.dtype.is_float() and not series.drop_nulls().is_finite().all():
        return series, "NaN or infinite values"
    if column.kind != "float" and series.dtype.is_float():
        return series, f"{series.dtype} is inexact: convert it to {column.dtype} in the transform, rounding as the data needs"
    converted = series.cast(column.dtype, strict=True)
    if column.kind != "float" and (lost := int((converted.cast(series.dtype) != series).sum())):
        return series, f"{lost} of {series.len()} values change when stored as {column.dtype}"
    return converted, None


def _boolean(series: pl.Series, column: Column, _naive: Naive) -> tuple[pl.Series, str | None]:
    if not series.dtype.is_integer():
        return series, f"{series.dtype} cannot become {column.dtype}"
    if not series.drop_nulls().is_in([0, 1]).all():
        return series, f"{series.dtype} values other than 0 and 1 are not booleans"
    return series.cast(pl.Boolean), None


def _date(series: pl.Series, column: Column, _naive: Naive) -> tuple[pl.Series, str | None]:
    """Oracle's DATE holds a time of day too: a value at midnight is a date, and one with a time is not."""
    if not isinstance(series.dtype, pl.Datetime):
        return series, f"{series.dtype} cannot become {column.dtype}"
    as_date = series.cast(pl.Date)
    if (series != as_date.cast(series.dtype)).any():
        return series, "datetimes with a time of day are not dates"
    return as_date, None


def _not_convertible(series: pl.Series, column: Column, _naive: Naive) -> tuple[pl.Series, str | None]:
    return series, f"{series.dtype} cannot become {column.dtype}"


def _parse_text(series: pl.Series, _column: Column) -> tuple[pl.Series, str | None]:
    return series, None


def _parse_boolean(text: pl.Series, _column: Column) -> tuple[pl.Series, str | None]:
    lowered = text.str.to_lowercase()
    if not lowered.drop_nulls().is_in(["true", "false", "1", "0"]).all():
        return text, "text other than true, false, 1 or 0 is not a boolean"
    return lowered.is_in(["true", "1"]).zip_with(lowered.is_not_null(), pl.Series([None], dtype=pl.Boolean)), None


def _parse_decimal(text: pl.Series, column: Column) -> tuple[pl.Series, str | None]:
    """polars rounds text with more decimal places than the scale, so they are counted first."""
    if not text.drop_nulls().str.contains(rf"^[+-]?\d+(\.\d{{0,{column.scale}}})?$").all():
        return text, f"text that is not a number with at most {column.scale} decimal places"
    return text.cast(column.dtype, strict=True), None


def _parse_number(text: pl.Series, column: Column) -> tuple[pl.Series, str | None]:
    return text.cast(column.dtype, strict=True), None


def _parse_date(text: pl.Series, _column: Column) -> tuple[pl.Series, str | None]:
    return text.str.to_date("%Y-%m-%d", strict=True), None


def _parse_instant(text: pl.Series, column: Column) -> tuple[pl.Series, str | None]:
    return _parse_datetime(text, column), None


def _parse_datetime(text: pl.Series, column: Column) -> pl.Series:
    """ISO 8601 text: with an offset, an instant; without one, a time in the column's zone."""
    with_offset = text.str.contains(r"([+-]\d{2}:?\d{2}|Z)$")
    aware = text.filter(with_offset.fill_null(value=False))
    if aware.len() == text.drop_nulls().len():
        return text.str.to_datetime(strict=True, time_zone="UTC", time_unit="us")
    if aware.len():
        msg = "some values carry an offset and some do not"
        raise pl.exceptions.InvalidOperationError(msg)
    return text.str.to_datetime(strict=True, time_unit="us").dt.replace_time_zone(column.zone, ambiguous="raise", non_existent="raise").dt.convert_time_zone("UTC")


def _to_utc(series: pl.Series, column: Column, naive: Naive) -> tuple[pl.Series, str | None]:
    dtype = series.dtype
    if not isinstance(dtype, pl.Datetime):
        return series, f"{dtype} is not a datetime"
    if dtype.time_zone is not None:
        return series.dt.convert_time_zone("UTC").dt.cast_time_unit("us"), None
    if naive == "reject":
        return series, f"naive datetimes: give them a zone, as .dt.replace_time_zone({column.zone!r}), or convert them to UTC"
    return series.dt.cast_time_unit("us").dt.replace_time_zone(column.zone, ambiguous="raise", non_existent="raise").dt.convert_time_zone("UTC"), None


_CONVERTERS: dict[Kind, _Converter] = {"int": _numeric, "float": _numeric, "decimal": _numeric, "bool": _boolean, "date": _date, "datetime": _to_utc, "str": _not_convertible}
_PARSERS: dict[Kind, _Parser] = {"str": _parse_text, "bool": _parse_boolean, "decimal": _parse_decimal, "int": _parse_number, "float": _parse_number, "date": _parse_date, "datetime": _parse_instant}


def _invalid(series: pl.Series, column: Column) -> list[str]:
    problems: list[str] = []
    if not column.nullable and (nulls := series.null_count()):
        problems.append(f"{nulls} nulls in a column that may not be null")
    if column.kind == "float" and not series.drop_nulls().is_finite().all():
        problems.append("NaN or infinite values")
    if column.max_length is not None and (longest := series.str.len_chars().max()) is not None and int(str(longest)) > column.max_length:
        problems.append(f"values up to {longest} characters, longer than its MaxLength({column.max_length})")
    return problems


def _first_line(error: Exception) -> str:
    return str(error).strip().splitlines()[0]
