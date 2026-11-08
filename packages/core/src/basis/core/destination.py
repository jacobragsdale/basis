"""A destination table as its database describes it, and the checks that what a run writes fits it. Pure: a database
module reads the description, and these functions only compare.

`check_table` runs before anything loads, so a table that has drifted from its row fails a run in seconds: a column
the row has and the table does not, a type that cannot hold the row's values exactly, or a column the table requires
that the row leaves out. `check_values` runs after the transform, on the values themselves: a string longer than its
column, or holding a character its column's code page cannot store, an infinite float, a float an integer column would
lose a fraction of, or one a decimal column's precision cannot hold, a null where the table forbids one, a float's NaN
among them, or a time of day finer than the column keeps."""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Literal

import numpy as np
import pandas as pd

from basis.core import frames
from basis.core.frames import WHOLE
from basis.core.tables import Changes, Column, Kind, Target

type Dialect = Literal["mssql", "oracle"]
type Unit = Literal["characters", "UTF-16 units", "bytes"]
UNICODE = frozenset({"utf-8", "utf-16-le", "utf-16-be"})
"""The encodings that store any text."""


@dataclass(frozen=True)
class DestColumn:
    name: str
    """Lower-case, as a row names it."""
    type: str
    """The database's own type name, upper-case, without its size: `NVARCHAR`, `NUMBER`, `TIMESTAMP`."""
    length: int | None
    """What a string column holds, in `unit`s; None when unbounded, or not a string."""
    precision: int | None
    """Digits a number holds; None where the type sets them, or for Oracle's unconstrained NUMBER."""
    scale: int | None
    nullable: bool
    generated: bool
    """An identity, computed or virtual column, which the table fills."""
    defaulted: bool
    unit: Unit = "characters"
    """What `length` counts: SQL Server's `nvarchar(n)` holds n UTF-16 units, and its `varchar(n)` n bytes."""
    max_bytes: int | None = None
    """The most bytes a string column holds in its encoding, beside its `length`, as Oracle's `VARCHAR2(n CHAR)` holds n
    characters, and 4,000 bytes at most."""
    encoding: str | None = None
    """Python's codec for how a string column stores its text, as `utf-8`, `utf-16-le` or `cp1252`: it measures the
    column's bytes, and a code page that is not Unicode replaces a character it cannot store, without an error, so
    `check_values` refuses one first. None for a column that is not a string, or whose encoding basis does not know,
    which `charset` then names."""
    charset: str | None = None
    """The database's own name for the column's encoding, as `code page 1252` or `WE8MSWIN1252`, which a refusal
    names."""


@dataclass(frozen=True)
class TableSpec:
    dialect: Dialect
    table: str
    columns: dict[str, DestColumn]


_WHOLE = frozenset({"TINYINT", "SMALLINT", "INT", "BIGINT"})
"""SQL Server's integer types, which a float binds into as an integer's text, and an identifier's text too."""
_DECIMAL = frozenset({"DECIMAL", "NUMERIC", "NUMBER"})
"""The decimal types, which a float binds into as its shortest decimal text, for the column's scale to round."""
_TYPES: dict[Dialect, dict[Kind, frozenset[str]]] = {
    "mssql": {
        "float": frozenset({"FLOAT", "REAL", "DECIMAL", "NUMERIC"}) | _WHOLE,
        "str": frozenset({"CHAR", "VARCHAR", "NCHAR", "NVARCHAR", "TEXT", "NTEXT"}) | _WHOLE,
        "bool": frozenset({"BIT"}),
        "date": frozenset({"DATE", "DATETIME2", "DATETIME", "SMALLDATETIME"}),
        "datetime": frozenset({"DATETIME2", "DATETIME", "SMALLDATETIME"}),
    },
    "oracle": {
        "float": frozenset({"BINARY_DOUBLE", "BINARY_FLOAT", "FLOAT", "NUMBER"}),
        "str": frozenset({"CHAR", "NCHAR", "VARCHAR2", "NVARCHAR2", "CLOB", "NCLOB", "NUMBER"}),
        "bool": frozenset({"BOOLEAN", "NUMBER"}),
        "date": frozenset({"DATE", "TIMESTAMP"}),
        "datetime": frozenset({"TIMESTAMP", "DATE"}),
    },
}
"""The types that take each kind of column's values, given the checks below: a float into a float's type, a decimal's,
which its scale rounds it to, or an integer's, where it is whole; text into a text type, or, where it is an
identifier's whole number, an integer's, which the server converts it to."""

type Binding = Literal["float", "decimal", "whole"]
"""How a float binds into a column: as a float, as its shortest decimal text, or as a whole number's text."""


def binding(found: DestColumn) -> Binding:
    """How a float binds into `found`: as its shortest decimal text into `DECIMAL`, `NUMERIC` or Oracle's `NUMBER`,
    whose scale then rounds it, as 2.675 into `DECIMAL(10,2)` is 2.68; as a whole number's text into an integer type;
    and as a float into the rest."""
    if found.type in _WHOLE:
        return "whole"
    return "decimal" if found.type in _DECIMAL else "float"


_INTEGER_RANGES: dict[str, tuple[int, int]] = {"TINYINT": (0, 255), "SMALLINT": (-(2**15), 2**15 - 1), "INT": (-(2**31), 2**31 - 1), "BIGINT": (-(2**63), 2**63 - 1)}
_SECONDS_ONLY = frozenset({"DATETIME", "SMALLDATETIME", "DATE"})
"""Datetime types that keep no fraction of a second: SQL Server's `datetime` rounds to 1/300 s, `smalldatetime` to the
minute, and Oracle's `DATE` to the second."""


def check_table(target: Target, spec: TableSpec) -> list[str]:
    """Why the table cannot take the row's columns, whatever their values."""
    reasons: list[str] = []
    names = {column.name for column in target.columns}
    for column in target.columns:
        found = spec.columns.get(column.name)
        if found is None:
            reasons.append(f"{column.name}: {spec.table} has no such column")
        elif found.generated:
            reasons.append(f"{column.name}: {spec.table} fills it itself (identity, computed or virtual): leave it out of the row")
        elif (problem := _type_problem(spec.dialect, column, found)) is not None:
            reasons.append(f"{column.name}: {problem}")
    reasons.extend(
        f"{found.name}: {spec.table} requires it, with no default, and the row leaves it out"
        for found in spec.columns.values()
        if found.name not in names and not found.nullable and not found.generated and not found.defaulted
    )
    return reasons


def _type_problem(dialect: Dialect, column: Column, found: DestColumn) -> str | None:
    if found.type == "JSON":
        return None if column.json else "a JSON column holds JSON text, objects and arrays: declare the row's column Annotated[str, etl.Json()]"
    if found.type not in _TYPES[dialect][column.kind]:
        return f"a {found.type} column cannot hold a row's {column.kind} values exactly"
    match column.kind, found.type:
        case "str", "NUMBER" if found.scale != 0:
            return "text goes into a NUMBER column only as an identifier's whole number, into NUMBER(p) or NUMBER(p,0)"
        case "bool", "NUMBER" if (found.precision, found.scale) != (1, 0):
            return "a boolean in an Oracle NUMBER column is NUMBER(1)"
        case _:
            return None


def check_values(target: Target, spec: TableSpec, changes: Changes) -> list[str]:
    """Why the values the run writes do not fit the table."""
    reasons: list[str] = []
    written = {name: frame for name, frame in changes.frames().items() if len(frame)}
    for operation, frame in written.items():
        for name in frame.columns:
            found = spec.columns.get(name)
            if found is None:
                continue
            series = frame[name]
            reasons.extend(f"{operation}: {name}: {problem}" for problem in _value_problems(target.column(name), found, series, spec.dialect))
    return reasons


def _value_problems(column: Column, found: DestColumn, series: pd.Series, dialect: Dialect) -> list[str]:
    problems: list[str] = []
    values = series.dropna()
    if not found.nullable and (nulls := int(series.isna().sum())):
        problems.append(f"{nulls} nulls, and {found.type} column {found.name} is NOT NULL")
    if not len(values):
        return problems
    match column.kind:
        case "str" if found.type in _WHOLE or found.type == "NUMBER":
            problems.extend(_identifier_problems(found, values))
        case "str":
            problems.extend(_text_problems(found, values, dialect))
        case "float":
            problems.extend(_number_problems(found, values))
        case "datetime" if found.type in _SECONDS_ONLY:
            if (values.dt.microsecond != 0).any():
                problems.append(f"fractions of a second, which {found.type} does not keep")
            if found.type == "SMALLDATETIME" and (values.dt.second != 0).any():
                problems.append("seconds, which SMALLDATETIME does not keep")
        case _:
            pass
    return problems


def _number_problems(found: DestColumn, values: pd.Series) -> list[str]:
    """Why a column cannot store floats, which hold no NaN: an infinity in any; in an integer type, a value with a
    fraction, beyond 2^53, or beyond the type's range; in a decimal type, a value its scale rounds to more digits than
    its precision holds."""
    numbers = values.to_numpy(dtype="float64")
    if count := int(np.isinf(numbers).sum()):
        return [f"{count} infinite values, which {found.type} column {found.name} cannot store: replace them in the transform, with NaN for NULL"]
    match binding(found):
        case "whole":
            if fractions := int((numbers != np.trunc(numbers)).sum()):
                return [f"{fractions} values with a fraction, which {found.type} column {found.name} does not keep: round them in the transform, as the data needs"]
            low, high = _INTEGER_RANGES[found.type]
            low, high = max(low, -WHOLE), min(high, WHOLE)
            if numbers.min() < low or numbers.max() > high:
                return [f"values from {_whole(numbers.min())} to {_whole(numbers.max())}, beyond {found.type}'s {low} to {high} that a float holds exactly"]
        case "decimal" if found.precision is not None:
            places = found.scale or 0
            widest = Decimal(repr(float(np.abs(numbers).max())))
            with localcontext(prec=max(widest.adjusted() + 1, 1) + places + 1):
                rounded = widest.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
            if rounded >= (limit := 10 ** (found.precision - places)):
                return [f"values up to {format(rounded, 'f')}, and {found.type}({found.precision},{places}) holds less than {limit}"]
        case _:
            pass
    return []


def _whole(value: float) -> str:
    return str(int(value)) if np.isfinite(value) else str(value)


def _identifier_problems(found: DestColumn, values: pd.Series) -> list[str]:
    """Why an integer column cannot take text, an identifier's, which the server converts: text that is no whole
    number, or one beyond the column's range."""
    text = values.astype(str)
    if len(bad := text[~text.str.fullmatch(r"[+-]?\d+")]):
        return [f"{len(bad)} values that are not whole numbers, which {found.type} column {found.name} cannot store, such as {bad.iloc[0]!r}"]
    numbers = [int(value) for value in text]
    low, high = _INTEGER_RANGES.get(found.type, (-(10 ** (found.precision or 38)) + 1, 10 ** (found.precision or 38) - 1))
    if min(numbers) < low or max(numbers) > high:
        return [f"values from {min(numbers)} to {max(numbers)}, beyond {found.type}'s {low} to {high}"]
    return []


def _text_problems(found: DestColumn, values: pd.Series, dialect: Dialect) -> list[str]:
    """Why a string column cannot store `values`, which hold no null: a character its encoding cannot store, or a
    value longer than the column holds, in the column's own `unit`, and in bytes where it has `max_bytes` too. Each
    distinct value is encoded once."""
    problems: list[str] = []
    distinct = values.drop_duplicates()
    encoded = _Encoded.of(distinct, found.encoding) if found.encoding is not None and found.encoding not in UNICODE else None
    if encoded is not None and encoded.lost:
        problems.append(
            f"{int(values.isin(encoded.lost).sum())} values hold characters {found.charset or found.encoding} cannot store, such as"
            f" {encoded.first!r}, which the database would replace without an error: {_UNICODE_COLUMN[dialect]}"
        )
    if found.encoding is None and found.charset is not None and len(beyond := values[~_ascii(values)]):
        first = next(character for character in str(beyond.iloc[0]) if not character.isascii())
        problems.append(
            f"{len(beyond)} values hold characters beyond ASCII, such as {first!r}, and basis does not know which of them {found.charset}"
            f" stores, which the database would replace without an error: {_UNICODE_COLUMN[dialect]}, or add {found.charset} to"
            f" {_CHARSETS_AT[dialect]}"
        )
    for limit, unit in ((found.length, found.unit), (found.max_bytes, "bytes")):
        if limit is not None and (longest := _longest(distinct, unit, found.encoding, encoded)) > limit:
            measure = f"bytes in {found.charset or found.encoding}" if unit == "bytes" else unit
            problems.append(f"values up to {longest} {measure}, and the column holds {limit}")
    return problems


_UNICODE_COLUMN: dict[Dialect, str] = {
    "mssql": "store them in a Unicode column, NVARCHAR or a VARCHAR with a UTF-8 collation",
    "oracle": "store them in an NVARCHAR2 or NCLOB column, whose national character set is Unicode",
}
_CHARSETS_AT: dict[Dialect, str] = {"mssql": "Python's codecs", "oracle": "basis.core.databases.oracle.CHARSETS"}


def _ascii(values: pd.Series) -> pd.Series:
    """Whether each of `values`, which hold no null, is ASCII alone, as a mask that indexes them."""
    return frames.ascii_only(values).astype(bool)


@dataclass(frozen=True)
class _Encoded:
    """What a code page makes of the distinct values beyond ASCII, each encoded once: those it cannot store, the first
    character it cannot, and the most bytes one it stores takes."""

    lost: list[str]
    first: str
    widest: int

    @classmethod
    def of(cls, distinct: pd.Series, encoding: str) -> _Encoded:
        lost: list[str] = []
        first, widest = "", 0
        for value in distinct[~_ascii(distinct)]:
            try:
                widest = max(widest, len(str(value).encode(encoding)))
            except UnicodeEncodeError as error:
                lost.append(str(value))
                first = first or str(error.object[error.start])
        return cls(lost, first, widest)


def _longest(distinct: pd.Series, unit: Unit, encoding: str | None, encoded: _Encoded | None) -> int:
    """The longest of the values, in `unit`, and in bytes in `encoding`: ASCII is one byte in every code page."""
    match unit, encoding:
        case "characters", _:
            lengths = distinct.str.len()
        case "UTF-16 units", _:
            lengths = frames.utf16_units(distinct)
        case "bytes", "utf-8":
            lengths = frames.utf8_bytes(distinct)
        case "bytes", "utf-16-le" | "utf-16-be":
            lengths = frames.utf16_units(distinct) * 2
        case "bytes", _:
            return max(_most(distinct[_ascii(distinct)].str.len()), encoded.widest if encoded is not None else 0)
    return _most(lengths)


def _most(lengths: pd.Series) -> int:
    return int(_number(lengths.max())) if len(lengths) else 0


def _number(value: object) -> Decimal:
    """A column's min or max, which pandas types as any scalar, as an exact number."""
    match value:
        case bool():
            msg = f"{value!r} is not a number"
            raise TypeError(msg)
        case int() | float() | Decimal():
            return Decimal(value)
        case _:
            msg = f"{value!r} is not a number"
            raise TypeError(msg)
