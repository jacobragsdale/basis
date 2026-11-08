"""What a run writes to a table, in the terms every part and every database share: a table's columns, the target a part
writes, and its changes. Pure: these are declarations and frames, and nothing here reads or writes.

A table is written through four operations, applied in one transaction in this order: delete (every row, rows in a
range, or rows matching given values), upsert by key, insert where the key is new, and insert. A part builds them as it
will; `basis.job`'s write modes build them from a transform's frames."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

import pandas as pd

IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,127}$")
"""A column name: lower-case, so it reads the same unquoted in SQL Server and Oracle, and never needs quoting."""

type Kind = Literal["float", "str", "bool", "date", "datetime"]
"""What a column holds. A number of any kind is a float: an amount or a count is one, and an identifier is text."""


@dataclass(frozen=True)
class Column:
    """A table's column. Its type in a frame follows from its kind, by the one table in `basis.core.frames`."""

    name: str
    kind: Kind
    nullable: bool
    max_length: int | None = None
    zone: str | None = None


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
    matching: pd.DataFrame | None = None


@dataclass(frozen=True, eq=False)
class Changes:
    """What a run does to one table, applied in this order."""

    delete: Delete | None = None
    upsert: pd.DataFrame | None = None
    insert_new: pd.DataFrame | None = None
    insert: pd.DataFrame | None = None

    @property
    def rerun_safe(self) -> bool:
        """Whether applying the same changes twice leaves the table as applying them once: an insert is, only after a
        delete that covers every row it inserts, which the checks require."""
        return self.insert is None or self.delete is not None

    def frames(self) -> dict[str, pd.DataFrame]:
        """Each operation's frame, by operation: what a snapshot stores."""
        found = {"upsert": self.upsert, "insert_new": self.insert_new, "insert": self.insert}
        if self.delete is not None and self.delete.matching is not None:
            found["delete_matching"] = self.delete.matching
        return {name: frame for name, frame in found.items() if frame is not None}


class Target(Protocol):
    """Where a table is written: what the databases and publishing need of the target a part declares, such as
    `basis.job.outputs.Target`."""

    @property
    def database(self) -> str: ...

    @property
    def table(self) -> str: ...

    @property
    def key(self) -> tuple[str, ...] | None: ...

    @property
    def columns(self) -> tuple[Column, ...]:
        """The table's columns, in order."""
        ...

    def column(self, name: str) -> Column: ...
