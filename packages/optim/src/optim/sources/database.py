"""A database as a source, read through basis-core's dialects (`basis.core.databases`): each fetch through the read
login, in a transaction that only reads, at the isolation the database's settings give, with a deadline on every
statement and `max_rows` on every result. A query is a SQL file, by path, in the database's own dialect: it binds the
run's params by name, as `:as_of_date` in Oracle and `%(as_of_date)s` in SQL Server.

A tuple param binds as one JSON array string, which the SQL unpacks with JSON_TABLE (Oracle) or OPENJSON (SQL Server):
one bind per batch, no IN-list limits, and no SQL built from strings.

A fetch outside a transaction reads in a transaction of its own. `read_only()` opens one for a block of fetches, so
reads that depend on each other, such as a batch's holdings and the rest of its rows, see the database at one moment;
its fetches take turns on the transaction's one connection, since a connection is not safe to use from two threads at
once."""

import json
import logging
import threading
import time
from collections.abc import Generator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Protocol, cast

from basis.core.databases import Bind, Statements
from basis.core.logs import ms_since
from optim.sources import Columns, Param

log = logging.getLogger(__name__)


class Readable(Protocol):
    """What a source needs of basis-core's `Database`."""

    @property
    def name(self) -> str: ...

    def statements(self, *, writes: bool) -> AbstractContextManager[Statements]: ...


@dataclass(frozen=True)
class DatabaseSource:
    database: Readable
    timeout_s: float
    """Each statement's deadline: the database's own `TIMEOUT_S`."""

    def fetch(self, query: str, params: Mapping[str, Param]) -> Columns:
        start = time.perf_counter()
        with self.database.statements(writes=False) as statements:
            return _query(statements, self.database.name, query, params, timeout_s=self.timeout_s, connect_ms=ms_since(start))

    @contextmanager
    def read_only(self) -> Generator[Transaction]:
        start = time.perf_counter()
        with self.database.statements(writes=False) as statements:
            yield Transaction(statements, self.database.name, self.timeout_s, connect_ms=ms_since(start))


@dataclass(frozen=True)
class Transaction:
    """One transaction that only reads, as `DatabaseSource.read_only` opened it."""

    statements: Statements
    database: str
    timeout_s: float
    connect_ms: float
    _turn: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def fetch(self, query: str, params: Mapping[str, Param]) -> Columns:
        with self._turn:
            return _query(self.statements, self.database, query, params, timeout_s=self.timeout_s, connect_ms=self.connect_ms, transaction=True)

    def read_only(self) -> nullcontext[Transaction]:
        return nullcontext(self)  # already in one


def _query(statements: Statements, database: str, query: str, params: Mapping[str, Param], *, timeout_s: float, connect_ms: float, transaction: bool = False) -> Columns:
    binds: dict[str, Bind] = {name: json.dumps(value) if isinstance(value, tuple) else value for name, value in params.items()}
    start = time.perf_counter()
    table = statements.query(read_sql(query), binds, timeout_s=timeout_s)
    log.info("query", extra={"query": query, "database": database, "rows": table.num_rows, "transaction": transaction, "connect_ms": connect_ms, "query_ms": ms_since(start)})
    return cast("Columns", table.to_pydict())  # pyarrow is untyped; named in lower case, as basis-core names them in both dialects


@cache
def read_sql(path: str) -> str:
    """A SQL file's text."""
    return Path(path).read_text(encoding="utf-8")
