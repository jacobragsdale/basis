"""What every database module provides, and what they share. A database is read through a `Reader`, inside one
transaction that only reads, and written through a `Writer`, inside one transaction that commits only when every table
the run writes there, and the ledger row of each, has been written. Identifiers in statements are a row's column
names and a table's name, which `etl_framework.rows` and `etl_framework.outputs` restrict to letters, digits,
underscores and `$`, so they are never quoted and never come from data; every value is bound."""

import re
from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

import polars as pl

from etl_framework.destination import TableSpec
from etl_framework.interval import Interval
from etl_framework.outputs import Changes, Target

FETCH_ROWS = 10_000
"""Rows per round trip when reading."""
WRITE_ROWS = 50_000
"""Rows per batched statement when writing, so a large table's binds never need one buffer for all of it."""


class TooManyRowsError(ValueError):
    def __init__(self, what: str, max_rows: int) -> None:
        super().__init__(f"{what} returned more than {max_rows} rows, the database's max_rows: is a filter missing, or does the limit need raising?")


@dataclass(frozen=True)
class Counts:
    """What a run's changes did to one table, as the database counted it."""

    deleted: int = 0
    upserted: int = 0
    inserted_new: int = 0
    inserted: int = 0


@dataclass(frozen=True)
class LedgerEntry:
    """One table's publish, as the ledger records it, in the transaction that wrote the table."""

    run_id: str
    job: str
    output: str
    table: str
    interval: Interval | None
    rerun_safe: bool
    counts: Counts
    published_at: datetime
    version: str


class Reader(Protocol):
    def fetch(self, sql: str, interval: Interval | None, *, what: str) -> pl.DataFrame:
        """The rows a query returns, its columns as the driver typed them, binding the interval where it names it."""
        ...

    def describe(self, table: str) -> TableSpec | None:
        """The table as the database describes it, or None if it does not exist."""
        ...


class Writer(Reader, Protocol):
    def published(self, run_id: str) -> bool:
        """Whether the ledger holds a publish of this run to this database."""
        ...

    def overlapping(self, job: str, output: str, interval: Interval) -> list[str]:
        """The runs the ledger holds of `job`'s `output` for an interval that overlaps `interval`, locked until the
        transaction ends, so two runs of one interval never both insert."""
        ...

    def apply(self, target: Target, changes: Changes, spec: TableSpec) -> Counts: ...

    def record(self, entry: LedgerEntry) -> None: ...

    def commit(self) -> None: ...


class Database(Protocol):
    @property
    def name(self) -> str: ...

    def reading(self) -> AbstractContextManager[Reader]:
        """A reader whose queries share one transaction that only reads, rolled back at the end."""
        ...

    def writing(self) -> AbstractContextManager[Writer]:
        """A writer whose changes commit only on `commit()`, and roll back otherwise."""
        ...

    @property
    def writable(self) -> bool:
        """Whether this machine's settings let it write the database."""
        ...


LEDGER_COLUMNS = (
    "run_id", "job", "output", "table_name", "interval_start", "interval_end", "rerun_safe",
    "deleted", "upserted", "inserted_new", "inserted", "published_at", "etl_version",
)  # fmt: skip


def ledger_values(entry: LedgerEntry) -> tuple[object, ...]:
    """An entry's values, in `LEDGER_COLUMNS` order, with every time naive UTC."""
    interval = entry.interval.binds() if entry.interval is not None else {"interval_start": None, "interval_end": None}
    counts = entry.counts
    return (
        entry.run_id, entry.job, entry.output, entry.table, interval["interval_start"], interval["interval_end"], entry.rerun_safe,
        counts.deleted, counts.upserted, counts.inserted_new, counts.inserted, naive_in_zone(entry.published_at, None), entry.version,
    )  # fmt: skip


def binds_named(sql: str, pattern: str, interval: Interval | None, *, what: str) -> dict[str, datetime]:
    """The interval's binds that a query names, by `pattern`, a regular expression with a `{name}` field."""
    names = [name for name in ("interval_start", "interval_end") if re.search(pattern.format(name=name), sql)]
    if names and interval is None:
        msg = f"{what} binds the interval ({', '.join(names)}), and this run has none: give it one"
        raise ValueError(msg)
    if interval is None:
        return {}
    binds = interval.binds()
    return {name: binds[name] for name in names}


def for_writing(target: Target, frame: pl.DataFrame) -> pl.DataFrame:
    """A frame's values as the database stores them: each datetime as a naive time in its column's zone."""
    converted = [
        pl.col(column.name).dt.convert_time_zone(column.zone).dt.replace_time_zone(None)
        for column in target.row.columns
        if column.kind == "datetime" and column.name in frame.columns and column.zone is not None
    ]
    return frame.with_columns(converted) if converted else frame


def naive_in_zone(moment: datetime, zone: str | None) -> datetime:
    """An instant as a naive time in `zone`, as a datetime column stores it; a date column compares with naive UTC."""
    return moment.astimezone(ZoneInfo(zone)).replace(tzinfo=None) if zone is not None else moment.replace(tzinfo=None)


def slices(frame: pl.DataFrame) -> Iterator[pl.DataFrame]:
    yield from frame.iter_slices(WRITE_ROWS)


def check_count(what: str, expected: int, counted: int) -> None:
    """A statement that touched a different number of rows than the run computed: the transaction rolls back."""
    if counted != expected:
        msg = f"{what} wrote {counted} rows, and the run computed {expected}: nothing is committed"
        raise RuntimeError(msg)
