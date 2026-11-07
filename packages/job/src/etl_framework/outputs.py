"""What a job writes. Each output is a `Table` on the job's `Outputs`: its row, the database and table it is written
to, its key if it has one, and how it is written, which every table names, since nothing about a table is assumed.

The core writes a table through four operations, applied in one transaction in this order: delete (every row, rows
in a range, or rows matching given values), upsert by key, insert where the key is new, and insert. Each write mode
builds them from the frame the transform returns for the table:

| Mode | Operations |
|---|---|
| `Append()` | insert |
| `Replace()` | delete every row, then insert |
| `ReplaceWhere(interval=..., matching=...)` | delete the rows in the run's interval, or matching the frame's values, then insert |
| `Upsert()` | upsert by key |
| `InsertNew()` | insert the rows whose key is not in the table |
| `Manual()` | whatever `Changes` the transform returns |

A table written with `Manual()` takes `Changes` from the transform, which builds anything else: a history table, a
dedupe against rows already there (the destination read as an input), a watermark."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Self, overload

import polars as pl

from etl_framework.rows import IDENTIFIER, Row
from etl_framework.rows import Column as RowColumn

TABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,127}(\.[A-Za-z_][A-Za-z0-9_$]{0,127})?$")


@dataclass(frozen=True)
class Append:
    pass


@dataclass(frozen=True)
class Replace:
    pass


@dataclass(frozen=True)
class ReplaceWhere:
    """Delete the rows whose `interval` column is in the run's interval, and whose `matching` columns equal the
    values of a row of the frame, then insert the frame. Each row the frame inserts must be one the delete covers, or
    a rerun would insert it twice."""

    interval: str | None = None
    matching: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.interval is None and not self.matching:
            msg = "ReplaceWhere names the rows it replaces: interval=COLUMN, matching=(COLUMN, ...), or both"
            raise ValueError(msg)


@dataclass(frozen=True)
class Upsert:
    pass


@dataclass(frozen=True)
class InsertNew:
    pass


@dataclass(frozen=True)
class Manual:
    pass


type FrameWrite = Append | Replace | ReplaceWhere | Upsert | InsertNew
"""The modes that build changes from a frame."""
type Write = FrameWrite | Manual


@dataclass(frozen=True)
class Range:
    """Rows whose `column` is at or after `start` and before `end`. A date column compares as its midnight UTC."""

    column: str
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            msg = f"a range's ends are instants: give {self.start} and {self.end} a zone"
            raise ValueError(msg)
        object.__setattr__(self, "start", self.start.astimezone(UTC))
        object.__setattr__(self, "end", self.end.astimezone(UTC))


@dataclass(frozen=True, eq=False)
class Delete:
    """The rows to delete: in `within`, and matching a row of `matching` in each of its columns. Neither deletes every
    row."""

    within: Range | None = None
    matching: pl.DataFrame | None = None


@dataclass(frozen=True, eq=False)
class Changes:
    """What a run does to one table, applied in this order."""

    delete: Delete | None = None
    upsert: pl.DataFrame | None = None
    insert_new: pl.DataFrame | None = None
    insert: pl.DataFrame | None = None

    @property
    def rerun_safe(self) -> bool:
        """Whether applying the same changes twice leaves the table as applying them once: an insert is, only after a
        delete that covers every row it inserts, which the checks require."""
        return self.insert is None or self.delete is not None

    def frames(self) -> dict[str, pl.DataFrame]:
        """Each operation's frame, by operation: what a snapshot stores."""
        found = {"upsert": self.upsert, "insert_new": self.insert_new, "insert": self.insert}
        if self.delete is not None and self.delete.matching is not None:
            found["delete_matching"] = self.delete.matching
        return {name: frame for name, frame in found.items() if frame is not None}


@dataclass(frozen=True)
class Target:
    """Where a table is written, and how."""

    row: type[Row]
    database: str
    table: str
    write: Write
    key: tuple[str, ...] | None = field(default=None)

    @property
    def key_columns(self) -> list[RowColumn]:
        return [self.row.column(name) for name in self.key or ()]


class Table[V: (pl.DataFrame, Changes)]:
    """A declared output, named by its attribute on the job's `Outputs`. On an instance, its value is the frame the
    transform returned, or for `Manual()`, the `Changes`."""

    @overload
    def __init__(self: Table[Changes], row: type[Row], *, database: str, table: str, write: Manual, key: tuple[str, ...] | None = None) -> None: ...
    @overload
    def __init__(self: Table[pl.DataFrame], row: type[Row], *, database: str, table: str, write: FrameWrite, key: tuple[str, ...] | None = None) -> None: ...
    def __init__(self, row: type[Row], *, database: str, table: str, write: Write, key: tuple[str, ...] | None = None) -> None:
        self.target = Target(row, database, table, write, key)
        self._name: str | None = None
        _validate(self.target)

    @property
    def name(self) -> str:
        if self._name is None:
            msg = "an output is named by its attribute on a job's Outputs class; this one is not on one"
            raise TypeError(msg)
        return self._name

    def __set_name__(self, owner: type, name: str) -> None:
        if not IDENTIFIER.match(name):
            msg = f"{owner.__qualname__}.{name}: an output's name is lower-case letters, digits and underscores"
            raise TypeError(msg)
        if self._name not in {None, name}:
            msg = f"{owner.__qualname__}.{name} is already declared as {self._name!r}: declare each output once per name"
            raise TypeError(msg)
        self._name = name

    @overload
    def __get__(self, instance: None, owner: type) -> Self: ...
    @overload
    def __get__(self, instance: Outputs, owner: type) -> V: ...
    def __get__(self, instance: Outputs | None, owner: type) -> Self | V:
        if instance is None:
            return self
        value = instance.values[self.name]
        expected = Changes if isinstance(self.target.write, Manual) else pl.DataFrame
        if not isinstance(value, expected):
            msg = f"{self.name} holds a {type(value).__name__}, not a {expected.__name__}"
            raise TypeError(msg)
        return value  # ty: ignore[invalid-return-type] -- the isinstance above is V's runtime check; ty cannot relate a constrained TypeVar to a class object


def _validate(target: Target) -> None:
    where = target.table
    if not TABLE_NAME.match(target.table):
        msg = f"{where}: a table is TABLE or SCHEMA.TABLE, letters, digits, underscores and $, never quoted"
        raise ValueError(msg)
    if not IDENTIFIER.match(target.database):
        msg = f"{where}: database {target.database!r} is lower-case letters, digits and underscores, as in ETL_DATABASES__{target.database.upper()}__KIND"
        raise ValueError(msg)
    names = target.row.names()
    if target.key is not None and not target.key:
        msg = f"{where}: a key names at least one column; leave key out for a table without one"
        raise ValueError(msg)
    if target.key is not None and (unknown := [name for name in target.key if name not in names]):
        msg = f"{where}: its key names columns its row does not have: {unknown}"
        raise ValueError(msg)
    if target.key is not None and (nullable := [column.name for column in target.key_columns if column.nullable]):
        msg = f"{where}: its key's columns may not be null, but {nullable} are `| None`"
        raise ValueError(msg)
    match target.write:
        case Upsert() | InsertNew() if target.key is None:
            msg = f"{where}: {type(target.write).__name__}() needs the table's key, as key=('id',)"
            raise ValueError(msg)
        case ReplaceWhere(interval=interval, matching=matching):
            if interval is not None and target.row.column(interval).kind not in {"date", "datetime"}:
                msg = f"{where}: ReplaceWhere(interval={interval!r}) needs a date or datetime column"
                raise ValueError(msg)
            if unknown := [name for name in matching if name not in names]:
                msg = f"{where}: ReplaceWhere(matching=...) names columns its row does not have: {unknown}"
                raise ValueError(msg)
            if nullable := [name for name in matching if target.row.column(name).nullable]:
                msg = f"{where}: ReplaceWhere(matching=...) columns may not be null, since null matches nothing: {nullable}"
                raise ValueError(msg)
        case _:
            pass


class Outputs:
    """Base for a job's `Outputs`. Each class attribute that is a `Table` is one the job writes. The transform returns
    an instance with every output, by name: `Outputs(positions=frame)`."""

    def __init__(self, **values: pl.DataFrame | Changes) -> None:
        declared = self.declared()
        if missing := sorted(set(declared) - set(values)):
            msg = f"{type(self).__qualname__} is missing outputs {missing}: give every output it declares"
            raise TypeError(msg)
        if unknown := sorted(set(values) - set(declared)):
            msg = f"{type(self).__qualname__} declares no outputs named {unknown}; it declares {sorted(declared)}"
            raise TypeError(msg)
        for name, table in declared.items():
            expected = Changes if isinstance(table.target.write, Manual) else pl.DataFrame
            if not isinstance(values[name], expected):
                msg = f"{name} is written with {type(table.target.write).__name__}(), so its value is a {expected.__name__}, not a {type(values[name]).__name__}"
                raise TypeError(msg)
        self.values: Mapping[str, pl.DataFrame | Changes] = values

    @classmethod
    def declared(cls) -> dict[str, Table[pl.DataFrame] | Table[Changes]]:
        return {name: value for klass in reversed(cls.__mro__) for name, value in vars(klass).items() if isinstance(value, Table)}
