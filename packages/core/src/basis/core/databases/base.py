"""What every database module provides, and what they share. A database is read through a `Reader`, inside one
transaction that only reads, and written through a `Writer`, inside one transaction that commits only when every table
the run writes there, and the ledger row of each, has been written. Identifiers in statements are a row's column
names and a table's name, which the part that declares them restricts to letters, digits, underscores and `$`, as
`basis.job`'s rows and outputs do, so they are never quoted and never come from data; every value is bound.

Every statement a reader or a writer runs, and each commit and rollback, has a span, on the tracer of the run that
runs it (`basis.core.traces.tracer`). A caller that writes its own SQL, such as an API's request, runs it through
`Statements` instead: one connection's transaction, whose statements bind their values by name, each under a deadline
of its own, and which the caller gives a span of its own, with its deadline."""

import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
from opentelemetry.trace import Span, SpanKind

from basis.core import frames, traces
from basis.core.destination import Binding, TableSpec
from basis.core.dialects import SqlDialect
from basis.core.interval import Interval
from basis.core.tables import Changes, Target

log = logging.getLogger(__name__)
SYSTEMS = {"mssql": "microsoft.sql_server", "oracle": "oracle.db"}
"""OpenTelemetry's `db.system.name` for each kind of database."""
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
    """One output's publish, as the ledger records it: a table's, in the transaction that wrote it, or a message's,
    in the transaction that commits once it is sent."""

    run_id: str
    job: str
    output: str
    table: str
    """Where it went: the table, or a message's destination, as `slack:desk` or `email:desk`."""
    interval: Interval | None
    rerun_safe: bool
    counts: Counts
    published_at: datetime
    version: str
    detail: str | None = None
    """What else the ledger keeps of it, as a JSON object: a message's text's `sha256`."""


class Reader(Protocol):
    def fetch(self, sql: str, interval: Interval | None, *, what: str, given: Mapping[str, Bind] | None = None) -> pd.DataFrame:
        """The rows a query returns, its columns Arrow-backed, of the types the driver gave them, binding the interval
        where it names it, and each of `given` it names."""
        ...

    def batches(self, sql: str, interval: Interval | None, *, what: str) -> Iterator[pa.RecordBatch]:
        """The rows a query returns, as Arrow batches of at most `FETCH_ROWS`, each as the driver gives it, read as
        they are consumed, with no limit on the rows: a copy's, which holds one batch at a time."""
        ...

    def describe(self, table: str) -> TableSpec | None:
        """The table as the database describes it, or None if it does not exist."""
        ...


class Writer(Reader, Protocol):
    def published(self, run_id: str) -> bool:
        """Whether the ledger holds a publish of this run to this database."""
        ...

    def recorded(self, run_id: str) -> dict[str, Counts]:
        """What the ledger holds of this run, by output: what the database counted when each committed, so a resume of
        a run stopped after its commit reports what that commit wrote."""
        ...

    def lock(self, job: str, output: str) -> None:
        """Hold `job`'s `output`'s lock until the transaction ends: every publish of an output takes it before it reads
        the ledger or writes, so two runs of one output take turns, and neither deletes or inserts beside the other.
        A second writer waits, within the database's timeout."""
        ...

    def overlapping(self, job: str, output: str, interval: Interval | None) -> list[str]:
        """The runs the ledger holds of `job`'s `output` for an interval that overlaps `interval`, or, with no interval,
        every run of it. The caller holds the output's `lock`, so no other run of it records one meanwhile."""
        ...

    def apply(self, target: Target, changes: Changes, spec: TableSpec) -> Counts: ...

    def record(self, entry: LedgerEntry) -> None: ...

    def commit(self) -> None: ...


type Bind = str | int | float | Decimal | bool | date | datetime | UUID | bytes | None
"""A value a statement binds by name, as the driver converts it."""


class Statements(Protocol):
    """One connection's transaction, for statements a caller writes itself: queries, and, through the write login,
    statements that change rows. `timeout_s` bounds each statement, and the settings' `timeout_s` bounds it too."""

    def query(self, sql: str, binds: Mapping[str, Bind], *, timeout_s: float) -> pa.Table:
        """The rows a query returns, as the driver typed them, with its columns named in lower case, which is how
        both dialects then name them; more than the settings' `max_rows` fails."""
        ...

    def execute(self, sql: str, binds: Mapping[str, Bind], *, timeout_s: float) -> int:
        """The rows a statement changed. A transaction that only reads refuses it."""
        ...

    def commit(self) -> None:
        """Commit what the statements changed. A transaction that only reads refuses it."""
        ...


class Database(Protocol):
    @property
    def name(self) -> str: ...

    def reading(self) -> AbstractContextManager[Reader]:
        """A reader whose queries share one transaction that only reads, rolled back at the end."""
        ...

    def writing(self) -> AbstractContextManager[Writer]:
        """A writer whose changes commit only on `commit()`, and roll back otherwise."""
        ...

    def statements(self, *, writes: bool) -> AbstractContextManager[Statements]:
        """A connection for statements of the caller's own, in one transaction: through the write login where
        `writes`, which commits only on `commit()`; otherwise through the read login, in a transaction that only reads,
        as `reading`'s does. Either rolls back what it has not committed when it ends."""
        ...

    @property
    def writable(self) -> bool:
        """Whether this machine's settings let a run publish to the database: a write login, and a ledger."""
        ...


LEDGER_COLUMNS = (
    "run_id", "job", "output", "table_name", "interval_start", "interval_end", "rerun_safe",
    "deleted", "upserted", "inserted_new", "inserted", "published_at", "etl_version", "detail",
)  # fmt: skip


def ledger_values(entry: LedgerEntry) -> tuple[object, ...]:
    """An entry's values, in `LEDGER_COLUMNS` order, with every time naive UTC."""
    interval = entry.interval.binds() if entry.interval is not None else {"interval_start": None, "interval_end": None}
    counts = entry.counts
    return (
        entry.run_id, entry.job, entry.output, entry.table, interval["interval_start"], interval["interval_end"], entry.rerun_safe,
        counts.deleted, counts.upserted, counts.inserted_new, counts.inserted, naive_in_zone(entry.published_at, None), entry.version, entry.detail,
    )  # fmt: skip


def binds_named(sql: str, dialect: SqlDialect, interval: Interval | None, *, what: str, given: Mapping[str, Bind] | None = None) -> dict[str, Bind]:
    """The interval's binds that a query names, in `dialect`'s binds, and each of `given` it names, such as a
    watermark."""
    names = [name for name in ("interval_start", "interval_end") if dialect.binds(sql, name)]
    if names and interval is None:
        msg = f"{what} binds the interval ({', '.join(names)}), and this run has none: give it one"
        raise ValueError(msg)
    extra: dict[str, Bind] = {name: value for name, value in (given or {}).items() if dialect.binds(sql, name)}
    if interval is None:
        return extra
    binds = interval.binds()
    return {**{name: binds[name] for name in names}, **extra}


type Rows = Callable[[str, Mapping[str, object]], list[tuple[object, ...]]]
"""A writer's query of its transaction: the rows a statement returns, each statement a span."""


def published(rows: Rows, dialect: SqlDialect, ledger: str, run_id: str) -> bool:
    """Whether `ledger` holds a row of the run `run_id`: `Writer.published`, in either dialect."""
    found = rows(f"SELECT COUNT(*) FROM {ledger} WHERE run_id = {dialect.bind('run_id')}", {"run_id": run_id})
    return bool(found) and int(str(found[0][0])) > 0


def recorded(rows: Rows, dialect: SqlDialect, ledger: str, run_id: str) -> dict[str, Counts]:
    """What `ledger` counted of each output the run `run_id` published: `Writer.recorded`."""
    found = rows(f"SELECT output, deleted, upserted, inserted_new, inserted FROM {ledger} WHERE run_id = {dialect.bind('run_id')}", {"run_id": run_id})
    return {str(row[0]): Counts(*(int(str(value)) for value in row[1:])) for row in found}


def overlapping(rows: Rows, dialect: SqlDialect, ledger: str, *, job: str, output: str, interval: Interval | None) -> list[str]:
    """The runs `ledger` has of the job's output for an interval overlapping `interval`, or at all: `Writer.overlapping`."""
    binds: dict[str, object] = {"job": job, "output": output}
    sql = f"SELECT DISTINCT run_id FROM {ledger} WHERE job = {dialect.bind('job')} AND output = {dialect.bind('output')}"
    if interval is not None:
        binds |= interval.binds()
        sql += f" AND interval_start < {dialect.bind('interval_end')} AND interval_end > {dialect.bind('interval_start')}"
    return sorted({str(row[0]).strip() for row in rows(sql, binds)})


def for_writing(target: Target, frame: pd.DataFrame) -> pa.Table:
    """A conformed frame's values as the database stores them, as Arrow (`frames.to_arrow`): each datetime as a naive
    time in its column's zone."""
    table = frames.to_arrow(frame)
    for column in target.columns:
        if column.kind == "datetime" and column.zone is not None and column.name in table.column_names:
            index = table.column_names.index(column.name)
            table = table.set_column(index, column.name, frames.local(table.column(index), column.zone))
    return table


def naive_in_zone(moment: datetime, zone: str | None) -> datetime:
    """An instant as a naive time in `zone`, as a datetime column stores it; a date column compares with naive UTC."""
    return moment.astimezone(ZoneInfo(zone)).replace(tzinfo=None) if zone is not None else moment.replace(tzinfo=None)


def lower_cased(table: pa.Table) -> pa.Table:
    """`table` with its columns named in lower case: Oracle names an unquoted column in upper case, SQL Server as the
    query wrote it."""
    return table.rename_columns([name.lower() for name in table.column_names])


def number_text(values: pa.ChunkedArray | pa.Array, how: Binding) -> pa.ChunkedArray | pa.Array:
    """Floats as text, as a decimal or an integer column takes them (`destination.binding`): each as its shortest
    decimal text, in plain notation, which the column's scale then rounds, or a whole number as its digits; a null, a
    frame's NaN, stays a null."""
    if how == "whole":
        return pc.cast(pc.cast(values, pa.int64()), pa.string())
    return plain(pc.cast(values, pa.string()))


def plain(text: pa.ChunkedArray | pa.Array) -> pa.ChunkedArray | pa.Array:
    """Number text in plain notation: Arrow writes a float's shortest text with an exponent past 10^21 or below 10^-6,
    and a decimal's below 10^-6, as `1e-07` and `1E-10`, which SQL Server cannot convert to a decimal; only text with one
    is rewritten, in Python."""
    if not pc.call_function("any", [pc.call_function("match_substring_regex", [text], pc.MatchSubstringOptions("[eE]"))]).as_py():
        return text
    return pa.array([format(Decimal(value), "f") if value is not None and ("e" in value or "E" in value) else value for value in text.to_pylist()], pa.string())


def rows_of(table: pa.Table) -> list[tuple[object, ...]]:
    """`table`'s rows as the values a driver binds: `Decimal`, `date`, naive `datetime`, `bool`, `int`, `float`,
    `str` and None."""
    return list(zip(*(column.to_pylist() for column in table.columns), strict=True))


def slices(table: pa.Table) -> Iterator[pa.Table]:
    """`table`, `WRITE_ROWS` rows at a time."""
    for offset in range(0, table.num_rows, WRITE_ROWS):
        yield table.slice(offset, WRITE_ROWS)


def end_reading(database: str, roll_back: Callable[[], object]) -> None:
    """End a transaction that only read, freeing the snapshot the database kept for it. A rollback that fails, as on a
    connection that a call timeout broke, is logged and not raised: closing the connection, which follows, ends the
    transaction anyway, and the error that ended the block, if any, is the one to raise."""
    try:
        roll_back()
    except Exception:  # logged; the connection is closed next, which ends the transaction
        log.warning("could not roll back a transaction that only read; closing its connection", extra={"database": database}, exc_info=True)


def check_count(what: str, expected: int, counted: int) -> None:
    """A statement that touched a different number of rows than the run computed: the transaction rolls back."""
    if counted != expected:
        msg = f"{what} wrote {counted} rows, and the run computed {expected}: nothing is committed"
        raise RuntimeError(msg)


def span(database: str, kind: str, operation: str, sql: str | None = None) -> AbstractContextManager[Span]:
    """The span of one statement on `database`, of `kind` (`mssql` or `oracle`), named after its first keyword, or of
    `operation`, such as a commit, without SQL of its own; on the tracer of the run that runs it."""
    attributes: dict[str, str] = {"db.system.name": SYSTEMS[kind], "basis.resource": database}
    if sql is not None:
        attributes["db.query.text"] = sql
    return traces.tracer().start_as_current_span(f"{database} {operation}", kind=SpanKind.CLIENT, attributes=attributes)


def statement(database: str, kind: str, sql: str) -> AbstractContextManager[Span]:
    """The span of `sql` on `database`: `span`, named after its first keyword, such as `warehouse SELECT`."""
    words = sql.split(None, 1)
    return span(database, kind, words[0].upper() if words else "statement", sql)
