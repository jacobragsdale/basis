"""A fake of each kind of resource, for tests, which `api.build` takes in place of what the settings configure:

    app = api.build(service, settings=Settings.given(), fakes={WAREHOUSE: fakes.Database(answers={BOND: [row]}), CLOCK: fakes.Clock(at=...), SOLVE: fakes.Compute()})

A fake stands in only for what the resource reaches: the database's connection, the other service's network, the
system's clock. The fakes of another service, the clock and the ids are basis-core's (`basis.core.fakes`), which a
job's tests use too. Everything around it is the app's own, as in production: each call's deadline and span, the
transaction that commits only after the response validates, the checks of every reply, and the cache's loads and
expiry. A resource a test gives no fake opens as its settings say, so a test that fakes nothing reaches nothing only
when its settings configure nothing."""

import threading
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

import pyarrow as pa

from basis.api.compute import ComputeFunction
from basis.api.outboxes import Row
from basis.api.resources import Bind, Resource
from basis.core.databases.base import Statements
from basis.core.fakes import Clock, Http, Ids

__all__ = ["Answer", "Cache", "Clock", "Compute", "Database", "Fake", "Faked", "Fakes", "Http", "Ids", "Outbox", "Respond", "Rows", "Statement", "Transaction"]

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

    Its outbox, where a database declares one, is in memory, and needs no answers: `outbox` holds its rows, which a
    test may start with rows of its own, as `basis relay` finds them. A row a transaction adds is there once the
    transaction commits; a change to a row is there at once, as a compare-and-set, as the databases' own `UPDATE`
    makes it, and is undone if its transaction rolls back."""

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

    def keep(self, rows: Iterable[Row]) -> None:
        """Commit the outbox's rows a transaction added."""
        with self._lock:
            self._outbox.update({row.id: row for row in rows})

    def swap(self, row_id: str, *, was: str, seen: int, to: Mapping[str, object]) -> tuple[Row, Row] | None:
        """Change the outbox's row to `to`, at once and as one step, only while its status is `was` and its attempts
        `seen`: the row before and after, or None where it changed nothing."""
        with self._lock:
            found = self._outbox.get(row_id)
            if found is None or found.status != was or found.attempts != seen:
                return None
            changed = Row.model_validate(found.model_dump() | dict(to))
            self._outbox[row_id] = changed
            return found, changed

    def unswap(self, before: Row, after: Row) -> None:
        """Undo a change of a transaction that rolled back, unless the row has changed again since."""
        with self._lock:
            if self._outbox.get(before.id) == after:
                self._outbox[before.id] = before

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
    added: dict[str, Row] = field(default_factory=dict[str, Row])
    """The outbox's rows this transaction has added since it last committed, which only it sees until it commits."""
    swapped: list[tuple[Row, Row]] = field(default_factory=list[tuple[Row, Row]])
    """Each change of an outbox's row this transaction has made since it last committed, before and after."""

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
        self.database.keep(self.added.values())
        self.added.clear()
        self.swapped.clear()

    def roll_back(self) -> None:
        """What ending the transaction without committing takes: forgetting the outbox's rows it added, and undoing
        its changes of others."""
        self.added.clear()
        for before, after in reversed(self.swapped):
            self.database.unswap(before, after)
        self.swapped.clear()

    def _refuse_reads(self, what: str) -> None:
        if not self.writes:
            msg = f"the transaction only reads, so it cannot {what}"
            raise RuntimeError(msg)


@dataclass(frozen=True)
class Outbox:
    """A fake database's outbox, as one transaction reaches it: `basis.api.outboxes.Table`, in memory. Its reads see
    the rows other transactions committed and the ones this one added."""

    transaction: Transaction

    def _writes(self) -> None:
        if not self.transaction.writes:
            msg = "the transaction only reads, so it cannot change the outbox's rows"
            raise RuntimeError(msg)

    def probe(self, *, timeout_s: float) -> None:
        del timeout_s

    def add(self, row: Row, *, timeout_s: float) -> None:
        del timeout_s
        self._writes()
        self.transaction.added[row.id] = row

    def due(self, now: datetime, limit: int, *, timeout_s: float) -> list[Row]:
        del timeout_s
        seen = [*self.transaction.database.outbox, *self.transaction.added.values()]
        due = [row for row in seen if row.status in {"pending", "refused"} and row.next_at is not None and row.next_at <= now]
        return sorted(due, key=lambda row: (row.next_at, row.id))[:limit]

    def update(self, row_id: str, *, was: str, seen: int, to: Mapping[str, object], timeout_s: float) -> bool:
        del timeout_s
        self._writes()
        if (added := self.transaction.added.get(row_id)) is not None:  # its own, which no other transaction sees yet
            if added.status != was or added.attempts != seen:
                return False
            self.transaction.added[row_id] = Row.model_validate(added.model_dump() | dict(to))
            return True
        if (swapped := self.transaction.database.swap(row_id, was=was, seen=seen, to=to)) is None:
            return False
        self.transaction.swapped.append(swapped)
        return True


class Cache:
    """The cache as it runs, on a clock of the test's own, which only `advance` moves: a value expires when the test
    has advanced past its time to live."""

    def __init__(self) -> None:
        self._seconds = 0.0

    def monotonic(self) -> float:
        return self._seconds

    def advance(self, seconds: float) -> None:
        self._seconds += seconds


class Compute:
    """A compute function run in this process, on a thread, instead of in the pool, so that a test starts no process:
    its arguments and its result still cross by pickling, so a test catches a value the pool could not carry, and its
    route's deadline still bounds the wait. A test of the pool itself fakes nothing."""


type Fake = Database | Http | Cache | Clock | Ids | Compute
type Faked = Resource | ComputeFunction[..., object]
"""What a test may fake: a resource, or a compute function."""
type Fakes = Mapping[Faked, Fake]
"""What `api.build(..., fakes=...)` takes: a fake for each resource or compute function a test fakes."""
