"""Row schemas. A row is a class whose annotations are a table's columns, in order:

    class Position(etl.Row):
        as_of: date
        account_id: Annotated[str, etl.MaxLength(20)]
        quantity: Annotated[Decimal, etl.Precision(18, 4)]
        updated_at: Annotated[datetime, etl.Zone("UTC")] | None

A column is an `int`, `float`, `Decimal`, `str`, `bool`, `date` or `datetime`, and `| None` when it may be null. A
`Decimal` names its precision and scale, and a `datetime` names the zone its naive values are in, in sources and
destinations alike; in a frame it is always UTC. Nothing is assumed about either.

In a frame, every column is Arrow-backed, of its kind's type, by the one table in `basis.core.frames`: so an `int`
with nulls stays an integer, a `Decimal` stays exact, and a `datetime` keeps its zone.

`conform` is the boundary: every frame that enters or leaves a job passes through it. It converts only where nothing
is lost (an Oracle `NUMBER(10)` to an `int`, a CSV's text to a date, a transform's numpy column to Arrow's) and reports
every column it cannot convert, by `basis.core.frames.convert`'s rules."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from types import NoneType, UnionType
from typing import Annotated, ClassVar, Union, get_args, get_origin, get_type_hints
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

from basis.core import frames
from basis.core.errors import BasisError
from basis.core.frames import Naive
from basis.core.tables import IDENTIFIER, Column, Kind

MAX_PRECISION = 38
"""The most digits a decimal holds in SQL Server and Oracle alike."""
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


class RowError(BasisError, TypeError):
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
    def dtypes(cls) -> dict[str, pd.ArrowDtype]:
        """Each column's dtype in a frame, by name, as `frame.astype(Row.dtypes())` takes them."""
        return {column.name: frames.dtype(column) for column in cls.columns}

    @classmethod
    def empty(cls) -> pd.DataFrame:
        return frames.empty(cls.columns)


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
        return Column(name, kind, nullable, precision=decimal.precision, scale=decimal.scale)
    if kind == "datetime" and zone is None:
        msg = f"{where}: a datetime column names the zone its naive values are in, as Annotated[datetime, etl.Zone('UTC')]"
        raise RowError(msg)
    return Column(name, kind, nullable, max_length=max_length, zone=zone)


class SchemaError(ValueError):
    """A frame that does not match its row: every problem, one per line."""

    def __init__(self, what: str, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(f"{what} does not match its row:\n" + "\n".join(f"  - {problem}" for problem in problems))


def conform(frame: pd.DataFrame, row: type[Row], *, what: str, naive: Naive) -> pd.DataFrame:
    """`frame` with exactly `row`'s columns, in order and of their types, `basis.core.frames`' Arrow-backed ones, and an
    index that only counts its rows, or `SchemaError` naming every column that is missing, extra, not convertible
    without loss, null where it may not be, or too long. Column names match without regard to case, since Oracle
    reports them in upper case."""
    return conform_columns(frame, row.columns, what=what, naive=naive)


def conform_columns(frame: pd.DataFrame, columns: Sequence[Column], *, what: str, naive: Naive) -> pd.DataFrame:
    """`conform`, for some of a row's columns: those a delete matches on."""
    by_name = _by_name(frame)
    names = [column.name for column in columns]
    problems = _named_once(by_name, by_name)
    if extra := sorted(set(by_name) - set(names)):
        problems.append(f"columns the row does not declare: {', '.join(extra)}")
    problems.extend(_missing(frame, by_name, names))
    if problems:
        raise SchemaError(what, problems)
    converted: dict[str, pd.Series] = {}
    for column in columns:
        series, problem = frames.convert(frame[by_name[column.name][0]], column, naive)
        if problem is not None:
            problems.append(f"{column.name}: {problem}")
            continue
        problems.extend(f"{column.name}: {found}" for found in _invalid(series, column))
        converted[column.name] = series
    if problems:
        raise SchemaError(what, problems)
    return pd.DataFrame(converted, copy=False) if converted else pd.DataFrame()


def select(frame: pd.DataFrame, names: Sequence[str], *, what: str) -> pd.DataFrame:
    """`frame`'s columns `names`, matched as `conform` matches them, without regard to case, or `SchemaError` naming
    each that is missing or named more than once, as `conform` would."""
    by_name = _by_name(frame)
    if problems := _named_once(by_name, names) + _missing(frame, by_name, names):
        raise SchemaError(what, problems)
    return frame[[by_name[name][0] for name in names]]


def _by_name(frame: pd.DataFrame) -> dict[str, list[object]]:
    """Each of a frame's column names, in lower case, with every column it names."""
    found: dict[str, list[object]] = {}
    for name in frame.columns:
        found.setdefault(str(name).lower(), []).append(name)
    return found


def _named_once(by_name: dict[str, list[object]], names: Iterable[str]) -> list[str]:
    """Which of `names` the frame names more than once."""
    repeated = sorted(name for name in set(names) if len(by_name.get(name, [])) > 1)
    return [f"columns named more than once: {', '.join(repeated)}"] if repeated else []


def _missing(frame: pd.DataFrame, by_name: dict[str, list[object]], names: Sequence[str]) -> list[str]:
    """Why columns are missing, naming those a frame keeps in its index, as a `groupby` without `as_index=False` does."""
    if not (missing := [name for name in names if name not in by_name]):
        return []
    indexed = [name for name in missing if name in {str(level).lower() for level in frame.index.names if level is not None}]
    hint = f" ({', '.join(indexed)} in the frame's index: reset_index() it, or group with as_index=False)" if indexed else ""
    return [f"missing columns: {', '.join(missing)}{hint}"]


def _invalid(series: pd.Series, column: Column) -> list[str]:
    problems: list[str] = []
    if not column.nullable and (nulls := int(series.isna().sum())):
        problems.append(f"{nulls} nulls in a column that may not be null")
    if column.max_length is not None and len(found := series.dropna()) and (longest := int(found.str.len().max())) > column.max_length:
        problems.append(f"values up to {longest} characters, longer than its MaxLength({column.max_length})")
    return problems
