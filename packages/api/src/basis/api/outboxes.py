"""The outbox: the calls to other services that a route which writes a database sends (`api.Client.send`), each a row
of the outbox table that database declares (`api.outbox`), written in the request's transaction. A call therefore
commits or rolls back with the request's writes, and is made only once they have committed.

A row's life, which its `status` and `next_at` record:

1. The handler sends the call, and its row is written `pending`, due at `next_at` when the route's deadline has
   passed, so that `basis relay` never makes it while its request still may.
2. Once the transaction commits, the request makes each call it sent, at once and within what is left of the route's
   deadline, with the key as `Idempotency-Key`, and records how each went in a transaction of its own. Whatever it
   records, the request answers as it would have, and says nothing of the call.
3. `basis relay`, which the repo's scheduler runs, takes each row that is due, the longest due first. It claims a
   `pending` row, makes its call once and records how it went; it reports a `refused` one. A row whose `max_age_s` has
   passed since its commit is `expired` instead, and never made again.

How an attempt is recorded, by the request or the relay, depends on how the service answered:

- a 2xx: `done`;
- a 5xx, no answer, a call past its deadline, or one of the 4xx in `RETRIED` (408, 409, 425 and 429): one more failed
  attempt, due again after a wait that starts at `backoff_s` and doubles after each failure, up to `max_backoff_s`;
- any other 4xx: `refused`, made no more. A relay reports it at once; one the request recorded is due to the next relay,
  to report.

The rule is the status's, so it is the same for every kind of `api.ServiceError` a call fails with: a `Timeout`, an
`Unavailable` (a 502, a 503, a 429, or no answer) and a `Conflict` (a 409) are made again; a `NotFound`, a `Rejected`
and a `Denied` are refused; and a `Failed` is made again for a 5xx or a 408 or 425, and refused for any other 4xx.

A relay's run that reports a refused or an expired call fails, its one ERROR line naming each such call, its key and
how it ended, so the scheduler's alert fires.

Delivery is at least once: a request or a relay that stops after its call but before its record leaves the row
`pending`, and the call is made again, which the service drops by its `Idempotency-Key`. Every change to a row but its
insert expects the status and the attempts its writer last saw (`Table.update`), and a relay claims a row before its
call, so two relays that run at once, or a relay and a late request, make each call once and never undo each other's
record. Calls are made in no order the service may rely on.

A `done`, `refused` or `expired` row stays, with when it ended, for whoever looks into a call. `ddl` is the table's CREATE TABLE,
which `basis ddl SERVICE --dialect DIALECT` prints; `Sql` reaches it in a real database's own SQL, and
`basis.api.fakes.Database` keeps one in memory."""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Annotated, Literal, Protocol

from pydantic import AfterValidator

from basis.api.resources import Outbox, SendMethod, checked_path
from basis.core import ddl as core_ddl
from basis.core.databases.base import Bind, Statements, naive_in_zone
from basis.core.dialects import Dialect, of
from basis.core.errors import ServiceError
from basis.core.model import Model
from basis.core.tables import Column

type Status = Literal["pending", "done", "refused", "expired"]
type Value = str | int | datetime | None
"""A value of a row's column."""
PATH_LENGTH = 2000
KEY = re.compile(r"[\x21-\x7e]{1,255}")
"""An `Idempotency-Key`: one to 255 visible ASCII characters, which a header carries as they are."""
ROUTE_LENGTH = 200
ERROR_LENGTH = 2000
NAME_LENGTH = 128
"""The longest name SQL Server and Oracle (12.2 on) give a table, a constraint or an index."""
RETRIED = frozenset({408, 409, 425, 429})
"""The 4xx a call is made again after, as after a 5xx: a request timeout, a conflict (under the Idempotency-Key draft,
a call with the same key still being processed), too early, and too many requests. Any other 4xx refuses the call."""


def checked(path: object, key: object) -> None:
    """Refuse a call whose path or key the outbox cannot hold, or a header cannot carry: a defect in the handler."""
    if not isinstance(key, str) or not KEY.fullmatch(key):
        msg = f"the Idempotency-Key {key!r} is not one to 255 visible ASCII characters: give the id of what the call is about, as str(settlement.settlement_id)"
        raise ValueError(msg)
    if not isinstance(path, str) or not path.startswith("/") or len(path) > PATH_LENGTH:
        shown = path[:100] if isinstance(path, str) else path
        msg = f"the path {shown!r} is not the service's own path of at most {PATH_LENGTH} characters, after its base URL: give a str that starts with /"
        raise ValueError(msg)
    checked_path(path)


def _utc(moment: datetime) -> datetime:
    """A time the database stores naive, in UTC, as the instant it is."""
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


type Instant = Annotated[datetime, AfterValidator(_utc)]


class Row(Model):
    """One call in an outbox, as its table holds it, with its columns' names."""

    id: str
    """A UUIDv7, so rows sort by when they were sent."""
    service: str
    """The other service, by the name its `api.http` declaration gives."""
    method: SendMethod
    path: str
    """The service's own path, after its base URL."""
    body: str | None
    """The body's JSON, or None for a call without one."""
    idempotency_key: str
    route: str
    """The route whose request sent it."""
    request_id: str | None
    """The request's id, which its log records carry; None where none was bound."""
    created_at: Instant
    """When the handler sent it, in the request's transaction."""
    expires_at: Instant
    """When it ages out: `max_age_s` after `created_at`."""
    next_at: Instant | None
    """When `basis relay` next takes the row: to make its call while it is `pending`, or to report it once its service
    has refused it; None once it is `done`, `expired`, or reported."""
    attempts: int
    """How many times it has been made, or begun to be: each change to the row but its insert expects the number it
    last saw, so that of two attempts that overlap, the second changes nothing."""
    status: Status
    last_error: str | None
    """Why the last attempt failed, or None while none has."""
    ended_at: Instant | None
    """When the service accepted it or refused it for good, or when it aged out."""

    def named(self) -> str:
        """The call in a few words, as an ERROR line names it."""
        return f"{self.method} {self.path} to {self.service} with Idempotency-Key {self.idempotency_key} (route {self.route}, row {self.id})"


COLUMNS = tuple(Row.model_fields)


@dataclass(frozen=True)
class Failure:
    """Why an attempt at a call failed."""

    error: str
    reply: int | None
    """The status the service answered with, or None where it gave none, such as a call past its deadline."""

    @classmethod
    def of(cls, failed: ServiceError) -> Failure:
        """The attempt that failed with `failed`, the kind of a call's failure: its detail, and its status."""
        return cls(str(failed), failed.status)

    @property
    def permanent(self) -> bool:
        """Whether the service refused the call for good: a 4xx but those in `RETRIED`."""
        return self.reply is not None and HTTPStatus.BAD_REQUEST <= self.reply < HTTPStatus.INTERNAL_SERVER_ERROR and self.reply not in RETRIED


def backoff(declared: Outbox, attempts: int) -> timedelta:
    """How long a call waits after its `attempts`-th failed attempt, 1 or more: `backoff_s`, doubled after each failure
    since the first, up to `max_backoff_s`."""
    return timedelta(seconds=min(declared.backoff_s * 2 ** min(attempts - 1, 64), declared.max_backoff_s))


def again_at(declared: Outbox, row: Row, attempts: int, now: datetime) -> datetime:
    """When a call whose `attempts`-th attempt failed at `now` is due next: after its backoff, or when it ages out, if
    that is sooner, so an aged-out call is found when it ages out."""
    return min(now + backoff(declared, attempts), row.expires_at)


class Table(Protocol):
    """An outbox's table as one transaction reaches it; `timeout_s` bounds each statement."""

    def probe(self, *, timeout_s: float) -> None:
        """Fail, as the database does, unless the table exists and the login can read it."""
        ...

    def add(self, row: Row, *, timeout_s: float) -> None: ...

    def due(self, now: datetime, limit: int, *, timeout_s: float) -> list[Row]:
        """At most `limit` rows whose `next_at` is at or before `now`, the longest due first: each call still to make,
        and each one its service refused that is not yet reported."""
        ...

    def update(self, row_id: str, *, was: Status, seen: int, to: Mapping[str, Value], timeout_s: float) -> bool:
        """Set the row's columns `to`, in one statement, only while its status is `was` and its attempts `seen`: whether
        it did. Of two transactions that change one row, as two relays, or a request and a relay, the second then
        changes nothing."""
        ...


def claim(table: Table, row: Row, until: datetime, *, timeout_s: float) -> bool:
    """Count an attempt at `row` begun, due again at `until` should it never be recorded, unless another attempt has
    begun since `row` was read: whether this one may make the call."""
    return table.update(row.id, was="pending", seen=row.attempts, to={"attempts": row.attempts + 1, "next_at": until}, timeout_s=timeout_s)


def record(table: Table, declared: Outbox, row: Row, failure: Failure | None, *, seen: int, attempts: int, at: datetime, reported: bool, timeout_s: float) -> Status | None:
    """Record how the `attempts`-th attempt at `row`'s call went, at `at`, unless the row has changed since its attempts
    were `seen`: `done`; `refused` for good, due to `basis relay` to report at once unless `reported` says the caller
    reports it itself; or `pending`, due again after its backoff. The status it recorded, or None where it recorded
    nothing."""
    status: Status
    if failure is None:
        status, changed = "done", {"status": "done", "attempts": attempts, "next_at": None, "ended_at": at}
    elif failure.permanent:
        status = "refused"
        changed = {"status": status, "attempts": attempts, "next_at": None if reported else at, "ended_at": at, "last_error": failure.error[:ERROR_LENGTH]}
    else:
        status, changed = "pending", {"attempts": attempts, "next_at": again_at(declared, row, attempts, at), "last_error": failure.error[:ERROR_LENGTH]}
    return status if table.update(row.id, was="pending", seen=seen, to=changed, timeout_s=timeout_s) else None


def expire(table: Table, row: Row, at: datetime, *, timeout_s: float) -> bool:
    """Make `row`'s call no more, once it has aged out: whether no attempt had changed the row since it was read."""
    return table.update(row.id, was="pending", seen=row.attempts, to={"status": "expired", "next_at": None, "ended_at": at}, timeout_s=timeout_s)


def reported(table: Table, row: Row, *, timeout_s: float) -> bool:
    """Mark `row`, which its service refused, as reported, so the next run reports it no more: whether it was not yet."""
    return table.update(row.id, was="refused", seen=row.attempts, to={"next_at": None}, timeout_s=timeout_s)


@dataclass(frozen=True)
class Sql:
    """An outbox's table in a SQL Server or Oracle database, through a transaction's statements: every value bound, and
    every time naive in UTC, as the table stores it."""

    statements: Statements
    table: str
    dialect: Dialect

    def _bind(self, name: str) -> str:
        return of(self.dialect).bind(name)

    def probe(self, *, timeout_s: float) -> None:
        self.statements.query(f"SELECT id FROM {self.table} WHERE 1 = 0", {}, timeout_s=timeout_s)

    def add(self, row: Row, *, timeout_s: float) -> None:
        sql = f"INSERT INTO {self.table} ({', '.join(COLUMNS)}) VALUES ({', '.join(map(self._bind, COLUMNS))})"
        binds: dict[str, Bind] = {name: _stored(value) for name, value in row.model_dump().items()}
        self.statements.execute(sql, binds, timeout_s=timeout_s)

    def due(self, now: datetime, limit: int, *, timeout_s: float) -> list[Row]:
        columns, where = ", ".join(COLUMNS), f"WHERE next_at <= {self._bind('now')} AND status IN ('pending', 'refused') ORDER BY next_at, id"
        sql = of(self.dialect).first(columns, self.table, where, self._bind("limit"))
        found = self.statements.query(sql, {"now": _stored(now), "limit": limit}, timeout_s=timeout_s)
        return [Row.model_validate(values) for values in found.to_pylist()]

    def update(self, row_id: str, *, was: Status, seen: int, to: Mapping[str, Value], timeout_s: float) -> bool:
        if unknown := sorted(set(to) - set(COLUMNS)):
            msg = f"an outbox row has no columns {unknown}"
            raise ValueError(msg)
        assignments = ", ".join(f"{name} = {self._bind(f'to_{name}')}" for name in to)
        sql = f"UPDATE {self.table} SET {assignments} WHERE id = {self._bind('id')} AND status = {self._bind('was')} AND attempts = {self._bind('seen')}"
        binds: dict[str, Bind] = {"id": row_id, "was": was, "seen": seen} | {f"to_{name}": _stored(value) for name, value in to.items()}
        return self.statements.execute(sql, binds, timeout_s=timeout_s) == 1


def _stored(value: object) -> Bind:
    """A row's value as a statement binds it: a time naive in UTC."""
    if isinstance(value, datetime):
        return naive_in_zone(value, "UTC")
    if value is None or isinstance(value, str | int):
        return value
    msg = f"an outbox row holds {value!r}, which no column stores"
    raise TypeError(msg)


@dataclass(frozen=True)
class _Target:
    """The outbox table as `basis.core.ddl.create_table` reads a table: its columns, in `COLUMNS`' order, and its key."""

    database: str
    table: str
    columns: tuple[Column, ...]
    key: tuple[str, ...] | None = ("id",)

    def column(self, name: str) -> Column:
        return next(column for column in self.columns if column.name == name)


def _text(name: str, length: int | None, *, nullable: bool = False) -> Column:
    return Column(name, "str", nullable, max_length=length)


def _time(name: str, *, nullable: bool = False) -> Column:
    return Column(name, "datetime", nullable, zone="UTC")


_COLUMNS = (  # the table's, in the order of `Row`'s fields
    _text("id", 36), _text("service", 63), _text("method", 6), _text("path", PATH_LENGTH), _text("body", None, nullable=True),
    _text("idempotency_key", 255), _text("route", ROUTE_LENGTH), _text("request_id", 36, nullable=True), _time("created_at"),
    _time("expires_at"), _time("next_at", nullable=True), Column("attempts", "float", nullable=False), _text("status", 7),
    _text("last_error", ERROR_LENGTH, nullable=True), _time("ended_at", nullable=True),
)  # fmt: skip


def ddl(database: str, table: str, dialect: Dialect) -> str:
    """The outbox table's CREATE TABLE, keyed by id, and the index `basis relay` finds the rows due by: `next_at`, which
    is NULL once a row has ended, so that in Oracle the index holds only the rows the relay still takes."""
    created = core_ddl.create_table(_Target(database, table, _COLUMNS), dialect)
    return f"{created}CREATE INDEX {index(table, dialect)} ON {table} (next_at);\n"


def index(table: str, dialect: Dialect) -> str:
    """The name of the index on `table`'s `next_at`: `ix_NAME_due`, or, past the 128 characters a name may have, its
    name cut short with a hash of the whole table's, so that two outboxes' never collide. In Oracle it is in the
    table's schema, as SQL Server's index always is, so outboxes of one name in two schemas each have their own."""
    schema, _, bare = table.lower().rpartition(".")
    name = f"ix_{bare}_due"
    if len(name) > NAME_LENGTH:
        digest = hashlib.sha256(table.lower().encode()).hexdigest()[:8]
        name = f"ix_{bare[: NAME_LENGTH - len('ix___due') - len(digest)]}_{digest}_due"
    return f"{schema}.{name}" if schema and of(dialect).schema_indexes else name
