"""A fake of each resource `basis.core.resources` declares, for tests, which a part opens in place of what the settings
configure. A fake stands in only for what the resource reaches: the other service's network, the system's clock, the
ids' randomness. Everything around it is the part's own, as in production: each call's deadline and span, the checks
of every reply, and the kind of `basis.core.errors.ServiceError` a failure is.

A fake service fails on demand, so a test reaches every error path without a mock: `pricing.fails("/prices/X",
Unavailable)` answers each request for that path as a service that fails so does, every time, or `times=1` once and
then as its app does. Its answer is what a real service's would be, a status or a body that is not JSON, so the part's
own client makes the kind of it, as it makes a real service's, and retries it as it would: a fake service's client
waits as its `waits` do, which take no time, so a GET made again until its deadline makes every attempt at once.
`Failures` is how a fake keeps what it fails with, which every part's fakes share."""

import json
import smtplib
import threading
from collections.abc import Awaitable, Callable, Generator, Hashable, Mapping, MutableMapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from email.message import EmailMessage
from http import HTTPStatus
from typing import Any, Literal
from uuid import UUID

import httpx2
import pandas as pd
import pyarrow as pa

from basis.core.databases.base import LEDGER_COLUMNS, Bind, Counts, LedgerEntry, Statements, check_count, for_writing, naive_in_zone, rows_of
from basis.core.databases.failures import failure
from basis.core.ddl import ORACLE_VARCHAR_LIMIT
from basis.core.destination import DestColumn, Dialect, TableSpec, Unit
from basis.core.dialects import of
from basis.core.errors import DATABASE_KINDS, KINDS, BadReply, Conflict, Denied, Failed, NotFound, Rejected, ServiceError, Timeout, Unavailable
from basis.core.interval import Interval
from basis.core.mongo import Upserted
from basis.core.tables import Changes, Delete, Target

type _Message = MutableMapping[str, Any]
type ASGIApp = Callable[[_Message, Callable[[], Awaitable[_Message]], Callable[[_Message], Awaitable[None]]], Awaitable[None]]
"""An ASGI app, such as FastAPI's or Starlette's, or another service's own basis app."""

ANSWERS: dict[type[ServiceError], int] = {
    Timeout: HTTPStatus.GATEWAY_TIMEOUT,
    Unavailable: HTTPStatus.SERVICE_UNAVAILABLE,
    NotFound: HTTPStatus.NOT_FOUND,
    Conflict: HTTPStatus.CONFLICT,
    Rejected: HTTPStatus.UNPROCESSABLE_CONTENT,
    Denied: HTTPStatus.FORBIDDEN,
    BadReply: HTTPStatus.OK,
    Failed: HTTPStatus.INTERNAL_SERVER_ERROR,
}
"""The status a fake service that fails with each kind answers; `BadReply`'s 2xx has a body that is not JSON."""
NOT_JSON = b"not JSON"


@dataclass
class _Failing:
    kind: type[ServiceError]
    left: int | None
    """How many more calls it fails, or None for every one."""


class Failures[K: Hashable]:
    """What a fake fails with, by key, such as a service's path or a database's statement: a kind of `ServiceError`,
    every time, or the next `times`, which the fake takes as each call is made."""

    def __init__(self, what: str, kinds: tuple[type[ServiceError], ...]) -> None:
        self._what = what
        """The fake, as its refusals name it: `a fake service`."""
        self._kinds = kinds
        """The kinds it may fail with: those of what it fakes."""
        self._failing: dict[K, _Failing] = {}
        self._lock = threading.Lock()

    def set(self, key: K, kind: type[ServiceError], times: int | None) -> None:
        """Fail each call `key` names with `kind`: every one, or the next `times`."""
        if not (isinstance(kind, type) and kind in self._kinds):
            names = ", ".join(found.__name__ for found in self._kinds)
            msg = f"{self._what} fails with one of {names}, the kinds of ServiceError it can raise, not {getattr(kind, '__name__', kind)!r}"
            raise TypeError(msg)
        if times is not None and (isinstance(times, bool) or not isinstance(times, int) or times < 1):
            msg = f"{self._what} fails a whole number of times, 1 or more, or every time with times=None, not {times!r}"
            raise ValueError(msg)
        with self._lock:
            self._failing[key] = _Failing(kind, times)

    def take(self, *keys: K, only: frozenset[type[ServiceError]] | None = None) -> tuple[K, type[ServiceError]] | None:
        """The first of `keys` that fails, and its kind, counting it as failed once; or None. With `only`, a failure of
        another kind is left as it is, and not taken."""
        with self._lock:
            for key in keys:
                if (failing := self._failing.get(key)) is None or (only is not None and failing.kind not in only):
                    continue
                if failing.left is not None:
                    failing.left -= 1
                    if failing.left == 0:
                        del self._failing[key]
                return key, failing.kind
        return None


class Waits:
    """Waits that take no time: each is kept, in `waited`, and the clock the client reads moves by them alone, so a call
    made again while its deadline allows is made as often as the backoff says, at once, however long each attempt
    takes on the machine. Each waits all of its backoff. An attempt itself still has what is left of the deadline,
    in real time."""

    def __init__(self) -> None:
        self.waited: list[float] = []
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return sum(self.waited)

    async def sleep(self, seconds: float) -> None:
        with self._lock:
            self.waited.append(seconds)

    def jitter(self) -> float:
        return 1.0


class Http:
    """Another service, as an ASGI app the test gives, such as a FastAPI app with the routes the code calls. Calls reach
    it in the process, through the part's own client, unless the test has made a path fail (`fails`), and the client
    waits between attempts as `waits` does."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.waits = Waits()
        self._failing = Failures[str]("a fake service", KINDS)

    def fails(self, path: str, kind: type[ServiceError], *, times: int | None = None) -> None:
        """Answer each request for `path`, the service's own after its base URL and without its query, as a service
        that fails with `kind`: every one, or the next `times`, after which the app answers again."""
        if not isinstance(path, str) or not path.startswith("/"):
            msg = f"a fake service fails a path of its own, after its base URL, such as /prices/X, not {path!r}"
            raise ValueError(msg)
        self._failing.set(path, kind, times)

    def client(self, name: str) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.serve), base_url=f"http://{name}")

    async def serve(self, scope: _Message, receive: Callable[[], Awaitable[_Message]], send: Callable[[_Message], Awaitable[None]]) -> None:
        """The service as the fake serves it, the test's failures and then its app, which `client` reaches in the
        process, and a test may also serve on a socket, to check the part's client makes the same of both."""
        if scope["type"] != "http" or (taken := self._failing.take(str(scope["path"]))) is None:
            await self.app(scope, receive, send)
            return
        while (await receive()).get("more_body", False):
            pass
        kind = taken[1]
        status = ANSWERS[kind]
        body = NOT_JSON if kind is BadReply else b'{"failed": "%s"}' % kind.__name__.encode()
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


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
    `...0002`, so a test can name each id the code makes."""

    def __init__(self) -> None:
        self._made = 0
        self._lock = threading.Lock()

    def new(self) -> UUID:
        with self._lock:
            self._made += 1
            made = self._made
        return UUID(int=(7 << 76) | (0b10 << 62) | made)


SLACK_KINDS: tuple[type[ServiceError], ...] = (Timeout, Unavailable, NotFound, Rejected, Denied, Failed)
"""The kinds a Slack channel's post raises, real or fake: never a `Conflict` or a `BadReply`, since its reply is read
for its status alone."""
EMAIL_KINDS: tuple[type[ServiceError], ...] = (Timeout, Unavailable, Rejected, Denied, Failed)
"""The kinds an email's send raises, real or fake."""


class Slack:
    """A Slack channel, as its webhook answers: each message posted keeps its text, in `posted`, unless the test has
    made the channel fail (`fails`), when the webhook answers as a failing one does, every time, or the next `times`.
    The part's own poster posts to it, through `transport`, as it posts to Slack."""

    def __init__(self) -> None:
        self.posted: list[str] = []
        self._failing = Failures[str]("a fake Slack channel", SLACK_KINDS)

    def fails(self, kind: type[ServiceError], *, times: int | None = None) -> None:
        self._failing.set("post", kind, times)

    def client(self) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self._answer))

    def _answer(self, request: httpx2.Request) -> httpx2.Response:
        if (taken := self._failing.take("post")) is not None:
            return httpx2.Response(ANSWERS[taken[1]], text="failing")
        sent: object = json.loads(request.content)
        if not isinstance(sent, dict) or not isinstance(text := sent.get("text"), str):
            return httpx2.Response(HTTPStatus.BAD_REQUEST, text="invalid_payload")
        self.posted.append(text)
        return httpx2.Response(HTTPStatus.OK, text="ok")


_SMTP_FAILURES: dict[type[ServiceError], Callable[[], Exception]] = {
    Timeout: lambda: TimeoutError("timed out"),
    Unavailable: lambda: smtplib.SMTPConnectError(421, b"4.3.2 service not available"),
    Rejected: lambda: smtplib.SMTPRecipientsRefused({"someone@example.com": (550, b"5.1.1 no such user")}),
    Denied: lambda: smtplib.SMTPAuthenticationError(535, b"5.7.8 authentication failed"),
    Failed: lambda: smtplib.SMTPDataError(554, b"5.6.0 message refused"),
}
"""What a relay that fails with each kind does, as `smtplib` raises it."""


class Email:
    """An SMTP relay: each message sent to it keeps what it was, in `sent`, unless the test has made the relay fail
    (`fails`), when it fails as a relay that does, every time, or the next `times`. The part's own mailer opens a
    session with it through `connect`, as it does with a relay."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        self.logins: list[str] = []
        self.refused: set[str] = set()
        """Recipients the relay refuses, as one that has no such user does: it sends to the rest, and says which."""
        self.delay_s = 0.0
        """How long the relay takes to accept a message, in real time, as a slow one does."""
        self._failing = Failures[str]("a fake email relay", EMAIL_KINDS)

    def fails(self, kind: type[ServiceError], *, times: int | None = None) -> None:
        self._failing.set("send", kind, times)

    def refuses(self, *recipients: str) -> None:
        self.refused |= set(recipients)

    def connect(self, relay: object, timeout_s: float) -> _Session:
        del relay, timeout_s  # a fake relay is reached in the process, at once
        return _Session(self)


class _Session:
    """One session with a fake relay, as `smtplib.SMTP` gives one."""

    def __init__(self, relay: Email) -> None:
        self._relay = relay

    def login(self, user: str, password: str) -> object:
        del password
        self._relay.logins.append(user)
        return None

    def send_message(self, msg: EmailMessage) -> object:
        if (taken := self._relay._failing.take("send")) is not None:  # noqa: SLF001 -- the session is its relay's
            failure = _SMTP_FAILURES[taken[1]]()
            raise failure
        if self._relay.delay_s:
            threading.Event().wait(self._relay.delay_s)  # real time, as a slow relay takes
        recipients = [address.strip() for address in str(msg["To"]).split(",")]
        refused = {address: (550, b"5.1.1 no such user") for address in recipients if address in self._relay.refused}
        if len(refused) == len(recipients):
            raise smtplib.SMTPRecipientsRefused(refused)
        self._relay.sent.append(msg)
        return refused

    def quit(self) -> object:
        return None


MONGO_KINDS: tuple[type[ServiceError], ...] = (Timeout, Unavailable, Conflict, Rejected, Denied, Failed)
"""The kinds a Mongo database's reads and upserts raise, real or fake."""


class Mongo:
    """A Mongo database in memory: each collection a list of documents, in `collections`, which a test fills and reads.
    A query matches by equality and by `$eq`, `$ne`, `$gt`, `$gte`, `$lt`, `$lte` and `$in` on a field, as a job's
    filters do; an upsert sets fields by key, as Mongo's does. It fails on demand (`fails`), every time or the next
    `times`, with a kind of `ServiceError`: its finds, its upserts, or both."""

    def __init__(self) -> None:
        self.collections: dict[str, list[dict[str, object]]] = {}
        self.unique: dict[str, list[tuple[str, ...]]] = {}
        """Each collection's unique indexes, by their fields, beside `_id`'s, which every collection has."""
        self._failing = Failures[str]("a fake Mongo database", MONGO_KINDS)

    def fails(self, kind: type[ServiceError], *, times: int | None = None, on: Literal["find", "upsert", "any"] = "any") -> None:
        self._failing.set(on, kind, times)

    def find(self, collection: str, query: Mapping[str, object], *, limit: int) -> list[dict[str, object]]:
        self._fail("find")
        return [dict(document) for document in self.collections.get(collection, []) if _matches(document, query)][:limit]

    def upsert(self, collection: str, key: Sequence[str], documents: Sequence[Mapping[str, object]]) -> Upserted:
        self._fail("upsert")
        held = self.collections.setdefault(collection, [])
        matched = modified = upserted = 0
        for document in documents:
            found = next((there for there in held if all(there.get(field) == document[field] for field in key)), None)
            if found is None:
                held.append(dict(document))
                upserted += 1
                continue
            matched += 1
            if any(found.get(field) != value for field, value in document.items()):
                found.update(document)
                modified += 1
        return Upserted(matched, modified, upserted)

    def unique_keys(self, collection: str) -> list[tuple[str, ...]]:
        self._fail("find")
        return [("_id",), *self.unique.get(collection, [])]

    def _fail(self, operation: str) -> None:
        if (taken := self._failing.take(operation, "any")) is not None:
            failure = taken[1]("a fake Mongo database fails", resource="mongo")
            raise failure


_COMPARE: dict[str, Callable[[Any, Any], bool]] = {
    "$eq": lambda found, wanted: found == wanted,
    "$ne": lambda found, wanted: found != wanted,
    "$gt": lambda found, wanted: found is not None and found > wanted,
    "$gte": lambda found, wanted: found is not None and found >= wanted,
    "$lt": lambda found, wanted: found is not None and found < wanted,
    "$lte": lambda found, wanted: found is not None and found <= wanted,
    "$in": lambda found, wanted: found in wanted,
    "$nin": lambda found, wanted: found not in wanted,
}


def _matches(document: Mapping[str, object], query: Mapping[str, object]) -> bool:
    """Whether `document` matches `query`, as Mongo's does for a job's filters: a field by a dotted path into nested
    documents, an array field by any of its elements, the comparison operators, and `$and`, `$or` and `$nor`."""
    for name, wanted in query.items():
        if name in {"$and", "$or", "$nor"}:
            clauses = [_matches(document, {str(field): value for field, value in clause.items()}) for clause in wanted if isinstance(clause, Mapping)] if isinstance(wanted, list) else []
            if not {"$and": all, "$or": any, "$nor": lambda found: not any(found)}[name](clauses):
                return False
            continue
        found = _at(document, name)
        candidates = [found, *found] if isinstance(found, list) else [found]
        if isinstance(wanted, Mapping) and wanted and all(str(operator).startswith("$") for operator in wanted):
            for operator, value in wanted.items():
                each = all if operator in {"$ne", "$nin"} else any  # a negation holds of every element of an array
                if not each(_COMPARE[str(operator)](candidate, value) for candidate in candidates):
                    return False
        elif wanted not in candidates:
            return False
    return True


def _at(document: Mapping[str, object], path: str) -> object:
    """The value at a dotted path of nested documents, or None where there is none."""
    found: object = document
    for part in path.split("."):
        found = found.get(part) if isinstance(found, Mapping) else None
    return found


type Fake = Http | Clock | Ids | Slack | Email | Mongo


type Operation = Literal["fetch", "describe", "published", "overlapping", "apply", "record", "commit", "any"]
"""What a fake database's `fails` makes fail: one of a reader's or a writer's calls, or each."""


@dataclass
class _Held:
    """A table of a fake database: its description, and its rows as the database holds them, each a dict of its
    columns' values, a datetime naive in its column's zone (`databases.base.for_writing`)."""

    spec: TableSpec
    key: tuple[str, ...] | None
    rows: list[dict[str, object]]


class Database:
    """A database in memory, for tests, which `basis.core.databases.Database` describes: its tables, each made by
    `create` as `basis ddl`'s `CREATE TABLE` makes it, primary key and all, and its ledger, `entries`. A writer applies a
    run's changes as SQL Server's and Oracle's writers do, the same counts and the same refusals, to copies that become
    the database's only on `commit()`, as a transaction's changes do: a block that ends without one leaves nothing. The
    rerun guard's lookup, `overlapping`, holds the database's lock until the writer's block ends, as the real ledgers'
    row locks do, so a second writer of the same output waits for the first and then sees its ledger row. With
    `ledger=None`, its ledger table does not exist.

    A query is answered from `answers`, by its SQL exactly; a query it has no answer for fails, so a test says what each
    returns. `fails` makes a call fail with a kind of `ServiceError`, every time or the next `times`, as the real
    database's failures reach a part (`basis.core.databases.failures`). The contract suite in basis-core's tests holds
    it to SQL Server's and Oracle's writers."""

    def __init__(self, name: str, *, dialect: Dialect = "mssql", writable: bool = True, ledger: str | None = "etl.publishes") -> None:
        self.name = name
        self.dialect: Dialect = dialect
        self._writable = writable
        self.ledger = ledger
        """Its ledger table's name, which `describe` finds; None where there is none, and every call that reads or
        writes the ledger fails, as a statement on a table that does not exist does."""
        self.tables: dict[str, _Held] = {}
        self.entries: list[LedgerEntry] = []
        """The ledger, as committed."""
        self.answers: dict[str, pd.DataFrame | Callable[[Mapping[str, Bind]], pd.DataFrame]] = {}
        """What each query returns, by its SQL: its rows, or a function of what it binds that gives them."""
        self._failing = Failures[Operation]("a fake database", DATABASE_KINDS)
        self._lock = threading.Lock()

    @property
    def writable(self) -> bool:
        return self._writable

    def create(self, target: Target, *, spec: TableSpec | None = None) -> None:
        """Make `target`'s table, as `basis ddl` prints it for this database's dialect, or as `spec` describes it."""
        self.tables[target.table] = _Held(spec or described(target, self.dialect), target.key, [])

    def rows(self, table: str) -> list[dict[str, object]]:
        """The table's committed rows, as the database holds them."""
        return [dict(row) for row in self.tables[table].rows]

    def fails(self, kind: type[ServiceError], *, times: int | None = None, on: Operation = "any") -> None:
        self._failing.set(on, kind, times)

    def reading(self) -> AbstractContextManager[_DatabaseReader]:
        return nullcontext(_DatabaseReader(self, self.tables))

    @contextmanager
    def writing(self) -> Generator[_DatabaseWriter]:
        writer = _DatabaseWriter(self, {table: _Held(held.spec, held.key, [dict(row) for row in held.rows]) for table, held in self.tables.items()})
        try:
            yield writer
        finally:
            if writer.locked:
                self._lock.release()

    def statements(self, *, writes: bool) -> AbstractContextManager[Statements]:
        """Statements of the caller's own: each query answered from `answers`, by its SQL, as a reader's is. A
        statement that changes rows it cannot run, since it holds no SQL: basis-api's `fakes.Database` answers an
        API's, by their SQL."""
        return nullcontext(_DatabaseStatements(self, writes=writes))

    def answer(self, sql: str, binds: Mapping[str, Bind], *, what: str) -> pd.DataFrame:
        """What the query `sql` returns, binding `binds`, from `answers`."""
        if (answer := self.answers.get(sql)) is None:
            msg = f"the fake database {self.name} has no answer for {what}: give one, as `answers[SQL] = frame`"
            raise LookupError(msg)
        return answer.copy() if isinstance(answer, pd.DataFrame) else answer(binds)

    def fail(self, operation: Operation) -> None:
        if (taken := self._failing.take(operation, "any")) is not None:
            raise failure(self.name, taken[1], RuntimeError(f"the fake database fails {operation}"))


def described(target: Target, dialect: Dialect) -> TableSpec:
    """The table `basis ddl` makes for `target` in `dialect`, as the database would describe it."""
    columns: dict[str, DestColumn] = {}
    for column in target.columns:
        length = column.max_length if column.kind == "str" else None
        if column.kind == "str" and dialect == "oracle" and (length is None or length > ORACLE_VARCHAR_LIMIT):
            kind, length = "CLOB", None
        elif column.kind == "str":
            kind = "JSON" if column.json and dialect == "mssql" else ("NVARCHAR" if dialect == "mssql" else "VARCHAR2")
        else:
            kind = {"float": ("FLOAT", "BINARY_DOUBLE"), "bool": ("BIT", "NUMBER"), "date": ("DATE", "DATE"), "datetime": ("DATETIME2", "TIMESTAMP")}[column.kind][dialect == "oracle"]
        unit: Unit = "UTF-16 units" if kind == "NVARCHAR" else "characters"
        encoding = ("utf-16-le" if dialect == "mssql" else "utf-8") if column.kind == "str" else None
        precision = 1 if (column.kind, dialect) == ("bool", "oracle") else None
        scale = 0 if precision is not None else (6 if column.kind == "datetime" else None)
        columns[column.name] = DestColumn(column.name, kind, length, precision, scale, column.nullable, generated=False, defaulted=False, unit=unit, encoding=encoding)
    return TableSpec(dialect, target.table, columns)


@dataclass
class _DatabaseReader:
    database: Database
    tables: dict[str, _Held]
    """The tables it reads: the database's, or a writer's copies of them."""

    def fetch(self, sql: str, interval: Interval | None, *, what: str) -> pd.DataFrame:
        self.database.fail("fetch")
        return self.database.answer(sql, interval.binds() if interval is not None else {}, what=f"{what}'s query")

    def describe(self, table: str) -> TableSpec | None:
        self.database.fail("describe")
        if table == self.database.ledger:
            return TableSpec(self.database.dialect, table, {name: DestColumn(name, "NVARCHAR", None, None, None, nullable=True, generated=False, defaulted=False) for name in LEDGER_COLUMNS})
        return held.spec if (held := self.tables.get(table)) is not None else None


@dataclass
class _DatabaseStatements:
    database: Database
    writes: bool

    def query(self, sql: str, binds: Mapping[str, Bind], *, timeout_s: float) -> pa.Table:  # noqa: ARG002 -- the Statements protocol's signature
        self.database.fail("fetch")
        return pa.Table.from_pandas(self.database.answer(sql, binds, what=f"the query {sql!r}"), preserve_index=False)

    def execute(self, sql: str, binds: Mapping[str, Bind], *, timeout_s: float) -> int:
        msg = f"the fake database {self.database.name} runs no statement that changes rows ({sql!r}): basis-api's fakes.Database answers an API's"
        raise NotImplementedError(msg)

    def commit(self) -> None:
        if not self.writes:
            msg = f"the fake database {self.database.name}'s statements only read, as the read login's do: there is nothing to commit"
            raise RuntimeError(msg)


@dataclass
class _DatabaseWriter(_DatabaseReader):
    entries: list[LedgerEntry] = field(default_factory=list[LedgerEntry])
    locked: bool = False

    def published(self, run_id: str) -> bool:
        self.database.fail("published")
        self._ledger()
        return any(entry.run_id == run_id for entry in [*self.database.entries, *self.entries])

    def recorded(self, run_id: str) -> dict[str, Counts]:
        self.database.fail("published")
        self._ledger()
        return {entry.output: entry.counts for entry in [*self.database.entries, *self.entries] if entry.run_id == run_id}

    def lock(self, job: str, output: str) -> None:  # noqa: ARG002 -- the Writer protocol's signature; one lock serves every output
        self.database.fail("overlapping")
        if not self.locked:
            self.database._lock.acquire()  # noqa: SLF001 -- the database's own lock, which its writer holds until its block ends
            self.locked = True

    def overlapping(self, job: str, output: str, interval: Interval | None) -> list[str]:
        self.database.fail("overlapping")
        self._ledger()
        found = {e.run_id: e.interval for e in [*self.database.entries, *self.entries] if (e.job, e.output) == (job, output)}
        if interval is None:
            return sorted(found)
        return sorted(run for run, at in found.items() if at is not None and at.start < interval.end and interval.start < at.end)

    def apply(self, target: Target, changes: Changes, spec: TableSpec) -> Counts:  # noqa: ARG002 -- the Writer protocol's signature
        self.database.fail("apply")
        held = self.tables.get(target.table)
        if held is None:
            msg = f"the fake database {self.database.name} has no table {target.table}"
            raise failure(self.database.name, Failed, RuntimeError(msg))
        deleted = upserted = inserted_new = inserted = 0
        if changes.delete is not None:
            deleted = self._delete(target, held, changes.delete)
        if changes.upsert is not None and len(changes.upsert):
            upserted = self._upsert(target, held, self._values(target, changes.upsert))
        if changes.insert_new is not None and len(changes.insert_new):
            inserted_new = self._insert_new(target, held, self._values(target, changes.insert_new))
        if changes.insert is not None and len(changes.insert):
            inserted = self._insert(held, self._values(target, changes.insert))
        return Counts(deleted, upserted, inserted_new, inserted)

    def _delete(self, target: Target, held: _Held, delete: Delete) -> int:
        def deleted(row: dict[str, object]) -> bool:
            if (within := delete.within) is not None:
                zone = target.column(within.column).zone
                value = _instant(row.get(within.column))
                if value is None or not naive_in_zone(within.start, zone) <= value < naive_in_zone(within.end, zone):
                    return False
            if delete.matching is not None:
                return any(all(_equal(row.get(name), wanted.get(name)) for name in delete.matching.columns) for wanted in self._values(target, delete.matching))
            return True

        kept = [row for row in held.rows if not deleted(row)]
        count, held.rows = len(held.rows) - len(kept), kept
        return count

    def _upsert(self, target: Target, held: _Held, rows: list[dict[str, object]]) -> int:
        key = target.key or ()
        values = [name for name in rows[0] if name not in key]
        if not values:  # every column is the key: a row the table has is upserted already
            self._insert_new(target, held, rows)
            return len(rows)
        updated = 0
        for row in rows:
            found = [there for there in held.rows if all(_equal(there.get(name), row[name]) for name in key)]
            for there in found:
                there.update({name: row[name] for name in values})
            updated += len(found)
        inserted = self._insert_new(target, held, rows)
        check_count(f"the upsert into {target.table}", len(rows), updated + inserted)
        return updated + inserted

    def _insert_new(self, target: Target, held: _Held, rows: list[dict[str, object]]) -> int:
        key = target.key or ()
        new = [row for row in rows if not any(all(_equal(there.get(name), row[name]) for name in key) for there in held.rows)]
        return self._insert(held, new)

    def _insert(self, held: _Held, rows: list[dict[str, object]]) -> int:
        for row in rows:
            for name, column in held.spec.columns.items():
                if row.get(name) is None and not column.nullable:
                    raise failure(self.database.name, Rejected, RuntimeError(f"cannot insert NULL into {held.spec.table}.{name}"))
            if held.key is not None and any(all(_equal(there.get(name), row.get(name)) for name in held.key) for there in held.rows):
                raise failure(self.database.name, Conflict, RuntimeError(f"a row of {held.spec.table} already has the key {tuple(row.get(name) for name in held.key)}"))
            held.rows.append({name: row.get(name) for name in held.spec.columns})
        return len(rows)

    def _ledger(self) -> None:
        if self.database.ledger is None:
            raise failure(self.database.name, Failed, RuntimeError(f"the fake database {self.database.name} has no ledger table"))

    def _values(self, target: Target, frame: pd.DataFrame) -> list[dict[str, object]]:
        """A conformed frame's rows as the database stores them: each datetime naive in its column's zone, and on
        Oracle, which stores an empty string as NULL, each empty string a null."""
        table = for_writing(target, frame)
        names = [str(name) for name in table.column_names]
        rows = [dict(zip(names, values, strict=True)) for values in rows_of(table)]
        if of(self.database.dialect).empty_is_null:
            return [{name: None if value == "" else value for name, value in row.items()} for row in rows]
        return rows

    def record(self, entry: LedgerEntry) -> None:
        self.database.fail("record")
        self._ledger()
        if any((there.run_id, there.output) == (entry.run_id, entry.output) for there in [*self.database.entries, *self.entries]):
            raise failure(self.database.name, Conflict, RuntimeError(f"the ledger already has run {entry.run_id}'s {entry.output}"))
        self.entries.append(entry)

    def commit(self) -> None:
        self.database.fail("commit")
        self.database.tables = self.tables
        self.database.entries.extend(self.entries)
        self.tables = {table: _Held(held.spec, held.key, [dict(row) for row in held.rows]) for table, held in self.tables.items()}
        self.entries = []


def _instant(value: object) -> datetime | None:
    """A date or datetime value as a naive datetime, as a date column compares: its midnight."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time())
    return None


def _equal(found: object, wanted: object) -> bool:
    """Whether two values match as SQL's `=` does: a null matches nothing."""
    return found is not None and wanted is not None and found == wanted
