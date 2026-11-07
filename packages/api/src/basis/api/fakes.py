"""A fake of each kind of resource, for tests, which `api.build` takes in place of what the settings configure:

    app = api.build(service, settings=Settings.given(), fakes={WAREHOUSE: fakes.Database(answers={BOND: [row]}), CLOCK: fakes.Clock(at=...)})

A fake stands in only for what the resource reaches: the database's connection, the other service's network, the
system's clock. Everything around it is the app's own, as in production: each call's deadline and span, the
transaction that commits only after the response validates, the checks of every reply, and the cache's loads and
expiry. A resource a test gives no fake opens as its settings say, so a test that fakes nothing reaches nothing only
when its settings configure nothing."""

import threading
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx2
import pyarrow as pa
from starlette.types import ASGIApp

from basis.api.resources import Bind
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
    transaction, or in `rolled_back`, when it does not."""

    def __init__(self, *, answers: Mapping[str, Answer] | None = None) -> None:
        self.answers: dict[str, Answer] = dict(answers or {})
        self.committed: list[Statement] = []
        self.rolled_back: list[Statement] = []
        self._lock = threading.Lock()

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

    def roll_back(self) -> None:
        """What ending the transaction without committing takes, which is nothing here."""

    def _refuse_reads(self, what: str) -> None:
        if not self.writes:
            msg = f"the transaction only reads, so it cannot {what}"
            raise RuntimeError(msg)


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


type Fake = Database | Http | Cache | Clock | Ids
