"""Any DB-API driver as a `Source`. Queries are SQL files, by path.

A tuple param binds as one JSON array string, which the SQL unpacks with JSON_TABLE (Oracle) or OPENJSON (SQL Server):
one bind per batch, no IN-list limits, and no SQL built from strings.

A fetch outside a transaction opens its own connection and closes it. `read_only()` opens one connection for a block of
fetches, starts a read-only transaction on it, and rolls it back at the end; its fetches take turns on the connection,
since a DB-API connection is not safe to use from two threads at once. Rows are fetched in batches of 10,000, and a
query that returns more than `max_rows` fails.
"""

import json
import logging
import threading
import time
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Protocol

from optim.logs import ms_since
from optim.sources import Columns, Param, TooManyRowsError

log = logging.getLogger(__name__)
_FETCH_ROWS = 10_000  # per round trip


class _Column(Protocol):
    def __getitem__(self, index: int, /) -> object: ...


class Cursor(Protocol):
    arraysize: int

    @property
    def description(self) -> Sequence[_Column] | None: ...
    def execute(self, statement: str, parameters: dict[str, object] = ..., /) -> object: ...
    def fetchmany(self, size: int = ..., /) -> Sequence[Iterable[object]]: ...
    def close(self) -> object: ...


class Connection(Protocol):
    def cursor(self) -> Cursor: ...
    def rollback(self) -> object: ...
    def close(self) -> object: ...


@dataclass(frozen=True)
class SqlSource:
    connect: Callable[[], Connection]
    begin_read_only: str  # this database's statement that starts a read-only, consistent transaction
    max_rows: int

    def fetch(self, query: str, params: Mapping[str, Param]) -> Columns:
        start = time.perf_counter()
        with closing(self.connect()) as connection:
            return _query(connection, query, params, max_rows=self.max_rows, connect_ms=ms_since(start))

    @contextmanager
    def read_only(self) -> Generator[Transaction]:
        start = time.perf_counter()
        with closing(self.connect()) as connection:
            transaction = Transaction(connection, self.max_rows, connect_ms=ms_since(start))
            with closing(connection.cursor()) as cursor:
                cursor.execute(self.begin_read_only)
            try:
                yield transaction
            finally:
                _end(connection)


@dataclass(frozen=True)
class Transaction:
    """One connection's read-only transaction, as `SqlSource.read_only` opened it."""

    connection: Connection
    max_rows: int
    connect_ms: float
    _turn: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def fetch(self, query: str, params: Mapping[str, Param]) -> Columns:
        with self._turn:
            return _query(self.connection, query, params, max_rows=self.max_rows, connect_ms=self.connect_ms, transaction=True)

    def read_only(self) -> nullcontext[Transaction]:
        return nullcontext(self)  # already in one


def _end(connection: Connection) -> None:
    """Roll back: the transaction only read, and ending it frees the snapshot the database kept for it. After a timeout
    the connection may be dead and the rollback fail; closing it ends the transaction anyway, and the error that ended
    the block, if any, is the one to raise."""
    try:
        connection.rollback()
    except Exception:  # logged; the connection is closed next, which ends the transaction
        log.warning("could not roll back a read-only transaction; closing its connection", exc_info=True)


def _query(connection: Connection, query: str, params: Mapping[str, Param], *, max_rows: int, connect_ms: float, transaction: bool = False) -> Columns:
    binds: dict[str, object] = {k: json.dumps(v) if isinstance(v, tuple) else v for k, v in params.items()}
    start = time.perf_counter()
    with closing(connection.cursor()) as cursor:
        cursor.arraysize = _FETCH_ROWS
        cursor.execute(read_sql(str(query)), binds)
        executed = ms_since(start)
        names = [str(column[0]).lower() for column in cursor.description or ()]
        rows: list[Iterable[object]] = []
        while batch := cursor.fetchmany(_FETCH_ROWS):
            rows.extend(batch)
            if len(rows) > max_rows:
                raise TooManyRowsError(query, max_rows)
    timings = {"connect_ms": connect_ms, "execute_ms": executed, "fetch_ms": round(ms_since(start) - executed, 1)}
    log.info("query", extra={"query": query, "rows": len(rows), "transaction": transaction} | timings)
    return _columns(names, rows)


@cache
def read_sql(path: str) -> str:
    """A SQL file's text."""
    return Path(path).read_text(encoding="utf-8")


def _columns(names: Sequence[str], rows: Sequence[Iterable[object]]) -> Columns:
    """Transpose row tuples, as a cursor returns them, into columns."""
    if not rows:
        return {name: [] for name in names}
    return {name: list(values) for name, values in zip(names, zip(*rows, strict=True), strict=True)}
