"""A fake of each kind of resource, for tests, which `api.build` takes in place of what the settings configure:

    app = api.build(service, settings=Settings.given(), fakes={WAREHOUSE: fakes.Database(answers={BOND: [row]}), CLOCK: fakes.Clock(at=...), SOLVE: fakes.Compute()})

A fake stands in only for what the resource reaches: the database's connection, the other service's network, the
system's clock. Everything around it is the app's own, as in production: each call's deadline and span, the
transaction that commits only after the response validates, the checks of every reply, and the cache's loads and
expiry. A resource a test gives no fake opens as its settings say, so a test that fakes nothing reaches nothing only
when its settings configure nothing."""

import threading
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx2
import pyarrow as pa
from starlette.types import ASGIApp

from basis.api.compute import ComputeFunction
from basis.api.outboxes import Row
from basis.api.resources import Bind, Resource
from basis.core.databases.base import Statements

type Rows = Sequence[Mapping[str, object]]


@dataclass(frozen=True)
class Respond:
    """An answer that depends on the statement's binds, or that waits or raises as the database would."""

    to: Callable[[Mapping[str, Bind]], Rows | int]


type Answer = Rows | int | Respond
"""What a fake database answers a statement: a query's rows, each a mapping of its columns in lower case; the count a
statement changed; or a `Respond`, whose function of the binds gives either."""


@dataclass(frozen=True)
class Statement:
    """A statement that changed rows, with its binds, in a transaction the fake committed or rolled back."""

    sql: str
    binds: Mapping[str, Bind]


class Database:
    """A database whose every statement the test answers, by its SQL exactly, with `answers`; a statement it has no
    answer for fails. Each request's statements that change rows end in `committed`, when the app commits its
    transaction, or in `rolled_back`, when it does not.

    Its outbox, where a database declares one, is in memory, and needs no answers: `outbox` holds its rows as the
    transactions that committed left them, which a test may start with rows of its own, as `basis relay` finds them."""

    def __init__(self, *, answers: Mapping[str, Answer] | None = None, outbox: Iterable[Row] = ()) -> None:
        self.answers: dict[str, Answer] = dict(answers or {})
        self.committed: list[Statement] = []
        self.rolled_back: list[Statement] = []
        self._lock = threading.Lock()
        self._outbox: dict[str, Row] = {row.id: row for row in outbox}

    @property
    def outbox(self) -> list[Row]:
        """The outbox's rows, as committed, in the order they were sent."""
        with self._lock:
            return sorted(self._outbox.values(), key=lambda row: (row.created_at, row.id))

    def keep(self, rows: Mapping[str, Row]) -> None:
        """Commit the outbox's rows a transaction added or changed."""
        with self._lock:
            self._outbox.update(rows)

    @contextmanager
    def statements(self, *, writes: bool) -> Generator[Statements]:
        transaction = self.transaction(writes=writes)
        try:
            yield transaction
        finally:
            if not transaction.committed:
                transaction.roll_back()
            with self._lock:
                (self.committed if transaction.committed else self.rolled_back).extend(transaction.changed)

    def transaction(self, *, writes: bool) -> Transaction:
        """A new transaction. A test whose database commits or rolls back slowly, or fails to, returns a `Transaction`
        of its own whose `commit` or `roll_back` does."""
        return Transaction(self, writes=writes)

    def answer(self, sql: str, binds: Mapping[str, Bind]) -> Rows | int:
        with self._lock:
            found = self.answers.get(sql)
        if found is None:
            msg = f"the fake database has no answer for {sql!r}: give it one, as fakes.Database(answers={{SQL: rows}}), with the SQL the handler runs"
            raise LookupError(msg)
        return found.to(binds) if isinstance(found, Respond) else found


@dataclass
class Transaction:
    database: Database
    writes: bool
    changed: list[Statement] = field(default_factory=list[Statement])
    committed: bool = False
    outboxed: dict[str, Row] = field(default_factory=dict[str, Row])
    """The outbox's rows this transaction has added or changed since it last committed, as it sees them now."""

    def outbox(self) -> Outbox:
        """The database's outbox, as this transaction reaches it."""
        return Outbox(self)

    def query(self, sql: str, binds: Mapping[str, Bind], *, timeout_s: float) -> pa.Table:
        del timeout_s  # the app's deadline bounds a fake's answer, as it does a database's
        rows = self.database.answer(sql, binds)
        if isinstance(rows, int):
            msg = f"the fake database answers {sql!r} with a count, and the handler queries it: answer it with rows"
            raise TypeError(msg)
        return pa.Table.from_pylist([dict(row) for row in rows]) if rows else pa.table({})

    def execute(self, sql: str, binds: Mapping[str, Bind], *, timeout_s: float) -> int:
        del timeout_s
        self._refuse_reads("change rows")
        count = self.database.answer(sql, binds)
        if not isinstance(count, int):
            msg = f"the fake database answers {sql!r} with rows, and the handler executes it: answer it with the count it changes"
            raise TypeError(msg)
        self.changed.append(Statement(sql, dict(binds)))
        return count

    def commit(self) -> None:
        self._refuse_reads("commit")
        self.committed = True
        self.database.keep(self.outboxed)
        self.outboxed.clear()

    def roll_back(self) -> None:
        """What ending the transaction without committing takes: forgetting the outbox's rows it changed."""
        self.outboxed.clear()

    def _refuse_reads(self, what: str) -> None:
        if not self.writes:
            msg = f"the transaction only reads, so it cannot {what}"
            raise RuntimeError(msg)


@dataclass(frozen=True)
class Outbox:
    """A fake database's outbox, as one transaction reaches it: `basis.api.outboxes.Table`, in memory. Its reads see
    what has committed and what the transaction has changed itself."""

    transaction: Transaction

    def _seen(self) -> dict[str, Row]:
        return {row.id: row for row in self.transaction.database.outbox} | self.transaction.outboxed

    def _writes(self) -> None:
        if not self.transaction.writes:
            msg = "the transaction only reads, so it cannot change the outbox's rows"
            raise RuntimeError(msg)

    def _change(self, row_id: str, **changed: object) -> bool:
        self._writes()
        found = self._seen().get(row_id)
        if found is None or found.status != "pending":
            return False
        self.transaction.outboxed[row_id] = Row.model_validate(found.model_dump() | changed)
        return True

    def probe(self, *, timeout_s: float) -> None:
        del timeout_s

    def add(self, row: Row, *, timeout_s: float) -> None:
        del timeout_s
        self._writes()
        self.transaction.outboxed[row.id] = row

    def due(self, now: datetime, limit: int, *, timeout_s: float) -> list[Row]:
        del timeout_s
        due = [row for row in self._seen().values() if row.status == "pending" and row.next_at is not None and row.next_at <= now]
        return sorted(due, key=lambda row: (row.next_at, row.id))[:limit]

    def claim(self, row: Row, until: datetime, *, timeout_s: float) -> bool:
        del timeout_s
        found = self._seen().get(row.id)
        return found is not None and found.attempts == row.attempts and self._change(row.id, attempts=row.attempts + 1, next_at=until)

    def accepted(self, row_id: str, *, attempts: int, at: datetime, timeout_s: float) -> bool:
        del timeout_s
        return self._change(row_id, status="done", next_at=None, attempts=attempts, ended_at=at)

    def refused(self, row_id: str, *, attempts: int, next_at: datetime, error: str, timeout_s: float) -> bool:
        del timeout_s
        return self._change(row_id, attempts=attempts, next_at=next_at, last_error=error)

    def expire(self, row_id: str, *, at: datetime, timeout_s: float) -> bool:
        del timeout_s
        return self._change(row_id, status="expired", next_at=None, ended_at=at)


class Http:
    """Another service, as an ASGI app the test gives, such as a FastAPI app with the routes the handler calls, or the
    other service's own basis app. Calls reach it in the process, through the app's own client."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    def client(self, name: str) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app), base_url=f"http://{name}")


class Cache:
    """The cache as it runs, on a clock of the test's own, which only `advance` moves: a value expires when the test
    has advanced past its time to live."""

    def __init__(self) -> None:
        self._seconds = 0.0

    def monotonic(self) -> float:
        return self._seconds

    def advance(self, seconds: float) -> None:
        self._seconds += seconds


class Clock:
    """The time is `at`, until the test advances it."""

    def __init__(self, *, at: datetime) -> None:
        if at.tzinfo is None:
            msg = f"the fake clock's time {at} has no time zone: give it one, as datetime(..., tzinfo=UTC)"
            raise ValueError(msg)
        self._at = at.astimezone(UTC)

    def now(self) -> datetime:
        return self._at

    def advance(self, by: timedelta) -> None:
        self._at += by


class Ids:
    """UUIDv7s that count up from 1, in their random bits, at the epoch: `00000000-0000-7000-8000-000000000001`, then
    `...0002`, so a test can name each id a handler makes."""

    def __init__(self) -> None:
        self._made = 0
        self._lock = threading.Lock()

    def new(self) -> UUID:
        with self._lock:
            self._made += 1
            made = self._made
        return UUID(int=(7 << 76) | (0b10 << 62) | made)


class Compute:
    """A compute function run in this process, on a thread, instead of in the pool, so that a test starts no process:
    its arguments and its result still cross by pickling, so a test catches a value the pool could not carry, and its
    route's deadline still bounds the wait. A test of the pool itself fakes nothing."""


type Fake = Database | Http | Cache | Clock | Ids | Compute
type Faked = Resource | ComputeFunction[..., object]
"""What a test may fake: a resource, or a compute function."""
type Fakes = Mapping[Faked, Fake]
"""What `api.build(..., fakes=...)` takes: a fake for each resource or compute function a test fakes."""
