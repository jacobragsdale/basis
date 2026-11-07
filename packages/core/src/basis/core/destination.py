"""A destination table as its database describes it, and the checks that what a run writes fits it. Pure: a database
module reads the description, and these functions only compare.

`check_table` runs before anything loads, so a table that has drifted from its row fails a run in seconds: a column
the row has and the table does not, a type that cannot hold the row's values exactly, or a column the table requires
that the row leaves out. `check_values` runs after the transform, on the values themselves: a string longer than its
column, or holding a character its column's code page cannot store, a number its column's precision cannot hold, a
null where the table forbids one, or a time of day finer than the column keeps."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

import polars as pl

from basis.core.tables import Changes, Column, Kind, Target

type Dialect = Literal["mssql", "oracle"]


@dataclass(frozen=True)
class DestColumn:
    name: str
    """Lower-case, as a row names it."""
    type: str
    """The database's own type name, upper-case, without its size: `NVARCHAR`, `NUMBER`, `TIMESTAMP`."""
    length: int | None
    """Characters a string column holds; None when unbounded, or not a string."""
    precision: int | None
    """Digits a number holds; None where the type sets them, or for Oracle's unconstrained NUMBER."""
    scale: int | None
    nullable: bool
    generated: bool
    """An identity, computed or virtual column, which the table fills."""
    defaulted: bool
    encoding: str | None = None
    """Python's codec for the code page a string column stores its text in, such as `cp1252` for a SQL Server
    `varchar` under `SQL_Latin1_General_CP1_CI_AS`; None where it holds any Unicode text, as an `nvarchar` or a UTF-8
    `varchar` does, or for a column that is not a string."""


@dataclass(frozen=True)
class TableSpec:
    dialect: Dialect
    table: str
    columns: dict[str, DestColumn]


_TYPES: dict[Dialect, dict[Kind, frozenset[str]]] = {
    "mssql": {
        "int": frozenset({"TINYINT", "SMALLINT", "INT", "BIGINT", "DECIMAL", "NUMERIC"}),
        "float": frozenset({"FLOAT", "REAL"}),
        "decimal": frozenset({"DECIMAL", "NUMERIC"}),
        "str": frozenset({"CHAR", "VARCHAR", "NCHAR", "NVARCHAR", "TEXT", "NTEXT"}),
        "bool": frozenset({"BIT"}),
        "date": frozenset({"DATE", "DATETIME2", "DATETIME", "SMALLDATETIME"}),
        "datetime": frozenset({"DATETIME2", "DATETIME", "SMALLDATETIME"}),
    },
    "oracle": {
        "int": frozenset({"NUMBER"}),
        "float": frozenset({"BINARY_DOUBLE", "BINARY_FLOAT", "FLOAT", "NUMBER"}),
        "decimal": frozenset({"NUMBER"}),
        "str": frozenset({"CHAR", "NCHAR", "VARCHAR2", "NVARCHAR2", "CLOB", "NCLOB"}),
        "bool": frozenset({"BOOLEAN", "NUMBER"}),
        "date": frozenset({"DATE", "TIMESTAMP"}),
        "datetime": frozenset({"TIMESTAMP", "DATE"}),
    },
}
"""The types that hold each kind of column's values exactly, given the precision checks below."""

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
    if found.type not in _TYPES[dialect][column.kind]:
        return f"a {found.type} column cannot hold a row's {column.kind} values exactly"
    match column.kind, found.type:
        case "int", "DECIMAL" | "NUMERIC" | "NUMBER" if found.scale not in {None, 0} and found.precision is not None:
            return f"{found.type}({found.precision},{found.scale}) has a scale, which an int column does not need: use a scale of 0"
        case "float", "NUMBER" if found.precision is not None:
            return f"NUMBER({found.precision},{found.scale}) rounds a float: use BINARY_DOUBLE, or a Decimal column"
        case "decimal", _ if found.precision is not None and found.scale is not None and column.scale is not None and found.scale < column.scale:
            return f"{found.type}({found.precision},{found.scale}) keeps fewer decimal places than the row's {column.scale}"
        case "bool", "NUMBER" if (found.precision, found.scale) != (1, 0):
            return "a boolean in an Oracle NUMBER column is NUMBER(1)"
        case _:
            return None


def check_values(target: Target, spec: TableSpec, changes: Changes) -> list[str]:
    """Why the values the run writes do not fit the table."""
    reasons: list[str] = []
    frames = {name: frame for name, frame in changes.frames().items() if frame.height}
    for operation, frame in frames.items():
        for name in frame.columns:
            found = spec.columns.get(name)
            if found is None:
                continue
            series = frame.get_column(name)
            reasons.extend(f"{operation}: {name}: {problem}" for problem in _value_problems(target.column(name), found, series))
    return reasons


def _value_problems(column: Column, found: DestColumn, series: pl.Series) -> list[str]:
    problems: list[str] = []
    values = series.drop_nulls()
    if not found.nullable and (nulls := series.null_count()):
        problems.append(f"{nulls} nulls, and {found.type} column {found.name} is NOT NULL")
    if not values.len():
        return problems
    match column.kind:
        case "str":
            problems.extend(_text_problems(found, values))
        case "int":
            low, high = _INTEGER_RANGES.get(found.type, (-(10 ** (found.precision or 38)) + 1, 10 ** (found.precision or 38) - 1))
            if _number(values.min()) < low or _number(values.max()) > high:
                problems.append(f"values from {values.min()} to {values.max()}, beyond {found.type}'s {low} to {high}")
        case "decimal" if found.precision is not None:
            limit = 10 ** (found.precision - (found.scale or 0))
            if _number(values.abs().max()) >= limit:
                problems.append(f"values up to {values.abs().max()}, and {found.type}({found.precision},{found.scale}) holds less than {limit}")
        case "datetime" if found.type in _SECONDS_ONLY:
            if (values.dt.microsecond() != 0).any():
                problems.append(f"fractions of a second, which {found.type} does not keep")
            if found.type == "SMALLDATETIME" and (values.dt.second() != 0).any():
                problems.append("seconds, which SMALLDATETIME does not keep")
        case _:
            pass
    return problems


def _text_problems(found: DestColumn, values: pl.Series) -> list[str]:
    problems: list[str] = []
    if found.length is not None and (longest := _number(values.str.len_chars().max())) > found.length:
        problems.append(f"values up to {longest} characters, and the column holds {found.length}")
    if found.encoding is not None and (lost := _unstorable(values, found.encoding)):
        count, character = lost
        problems.append(
            f"{count} values hold characters code page {found.encoding} cannot store, such as {character!r}, which the database would"
            " replace without an error: store them in a Unicode column, NVARCHAR or a VARCHAR with a UTF-8 collation"
        )
    return problems


def _unstorable(values: pl.Series, encoding: str) -> tuple[int, str] | None:
    """How many of `values` hold a character `encoding` cannot store, and the first such character; None if every one
    is stored exactly. A code page holds ASCII, so only the others are encoded."""
    count, first = 0, ""
    for value in values.filter(values.str.contains(r"[^\x00-\x7F]")):
        try:
            str(value).encode(encoding)
        except UnicodeEncodeError as error:
            count, first = count + 1, first or str(error.object[error.start])
    return (count, first) if count else None


def _number(value: object) -> Decimal:
    """A frame's min or max, which polars types as any scalar, as an exact number."""
    match value:
        case bool():
            msg = f"{value!r} is not a number"
            raise TypeError(msg)
        case int() | float() | Decimal():
            return Decimal(value)
        case _:
            msg = f"{value!r} is not a number"
            raise TypeError(msg)
