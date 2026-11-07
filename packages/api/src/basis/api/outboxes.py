"""The outbox: the calls to other services that a route which writes a database sends (`api.Client.send`), each a row
of the outbox table that database declares (`api.outbox`), written in the request's transaction. A call therefore
commits or rolls back with the request's writes, and is made only once they have committed.

A row's life, which its `status` and `next_at` record:

1. The handler sends the call, and its row is written `pending`, due at `next_at` once the route's deadline has passed,
   so that `basis relay` never makes it while its request still may.
2. Once the transaction commits, the request makes the call, within what is left of the route's deadline, with the key
   as `Idempotency-Key`, and records how it went in a transaction of its own: `done` if the service accepted it (a
   2xx), and otherwise one failed attempt, due again `backoff_s` later. Either way the request answers as it would
   have, and says nothing of the call.
3. `basis relay`, which the repo's scheduler runs, takes each `pending` row that is due, oldest first, and makes its call
   once: `done` when the service accepts it, and otherwise due again after twice the last wait, up to `max_backoff_s`.
   A row whose `max_age_s` has passed since its commit is `expired` instead, and never made again: the relay's run fails,
   naming the call and its key, so the scheduler's alert fires.

Delivery is at least once: a request or a relay that stops after its call but before its record leaves the row
`pending`, and the call is made again, which the service drops by its `Idempotency-Key`. A relay claims a row before
its call, so two relays that run at once make each call once. Calls are made in no order the service may rely on.

A `done` or `expired` row stays, with when it ended, for whoever looks into a call. `ddl` is the table's CREATE TABLE,
which `basis ddl SERVICE --dialect DIALECT` prints; `Sql` reaches it in a real database's own SQL, and
`basis.api.fakes.Database` keeps one in memory."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Protocol

import polars as pl
from pydantic import AfterValidator

from basis.api.resources import Outbox, SendMethod
from basis.core import ddl as core_ddl
from basis.core.databases.base import Bind, Statements, naive_in_zone
from basis.core.destination import Dialect
from basis.core.model import Model
from basis.core.tables import Column

type Status = Literal["pending", "done", "expired"]
PATH_LENGTH = 2000
KEY = re.compile(r"[\x21-\x7e]{1,255}")
"""An `Idempotency-Key`: one to 255 visible ASCII characters, which a header carries as they are."""
ROUTE_LENGTH = 200
ERROR_LENGTH = 2000


def checked(path: str, key: str) -> None:
    """Refuse a call whose path or key the outbox cannot hold, or a header cannot carry: a defect in the handler."""
    if not isinstance(key, str) or not KEY.fullmatch(key):
        msg = f"the Idempotency-Key {key!r} is not one to 255 visible ASCII characters: give the id of what the call is about, as str(settlement.settlement_id)"
        raise ValueError(msg)
    if not isinstance(path, str) or not path.startswith("/") or len(path) > PATH_LENGTH:
        msg = f"the path {path[:100]!r} is not the service's own path of at most {PATH_LENGTH} characters, after its base URL: start it with /"
        raise ValueError(msg)


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
    """When it is due to be made next, by `basis relay`; None once it is `done` or `expired`."""
    attempts: int
    """How many times it has been made, or begun to be."""
    status: Status
    last_error: str | None
    """Why the last attempt failed, or None while none has."""
    ended_at: Instant | None
    """When the service accepted it, or when it aged out."""

    def named(self) -> str:
        """The call in a few words, as an ERROR line names it."""
        return f"{self.method} {self.path} to {self.service} with Idempotency-Key {self.idempotency_key} (route {self.route}, row {self.id})"


COLUMNS = tuple(Row.model_fields)


def backoff(declared: Outbox, attempts: int) -> timedelta:
    """How long a call waits after its `attempts`-th failed attempt, 1 or more: `backoff_s`, doubled after each failure
    since the first, up to `max_backoff_s`."""
    return timedelta(seconds=min(declared.backoff_s * 2 ** min(attempts - 1, 64), declared.max_backoff_s))


def again_at(declared: Outbox, row: Row, attempts: int, now: datetime) -> datetime:
    """When a call whose `attempts`-th attempt failed at `now` is due next: after its backoff, or when it ages out, if
    that is sooner, so an aged-out call is found when it ages out."""
    return min(now + backoff(declared, attempts), row.expires_at)


class Table(Protocol):
    """An outbox's table as one transaction reaches it. Each change but `add` touches a row only while it is `pending`,
    and says whether it did; `timeout_s` bounds each statement."""

    def probe(self, *, timeout_s: float) -> None:
        """Fail, as the database does, unless the table exists and the login can read it."""
        ...

    def add(self, row: Row, *, timeout_s: float) -> None: ...

    def due(self, now: datetime, limit: int, *, timeout_s: float) -> list[Row]:
        """At most `limit` `pending` rows due at `now`, the longest due first."""
        ...

    def claim(self, row: Row, until: datetime, *, timeout_s: float) -> bool:
        """Count an attempt at `row` begun, and make it due again at `until`, unless another attempt has begun since
        `row` was read: whether this one may make the call."""
        ...

    def accepted(self, row_id: str, *, attempts: int, at: datetime, timeout_s: float) -> bool: ...

    def refused(self, row_id: str, *, attempts: int, next_at: datetime, error: str, timeout_s: float) -> bool: ...

    def expire(self, row_id: str, *, at: datetime, timeout_s: float) -> bool: ...


@dataclass(frozen=True)
class Sql:
    """An outbox's table in a SQL Server or Oracle database, through a transaction's statements: every value bound, and
    every time naive in UTC, as the table stores it."""

    statements: Statements
    table: str
    dialect: Dialect

    def _bind(self, name: str) -> str:
        return f"%({name})s" if self.dialect == "mssql" else f":{name}"

    def _set(self, *names: str) -> str:
        return ", ".join(f"{name} = {self._bind(name)}" for name in names)

    def _update(self, assignments: str, binds: Mapping[str, Bind], *, timeout_s: float) -> bool:
        sql = f"UPDATE {self.table} SET {assignments} WHERE id = {self._bind('id')} AND status = 'pending'"
        return self.statements.execute(sql, binds, timeout_s=timeout_s) == 1

    def probe(self, *, timeout_s: float) -> None:
        self.statements.query(f"SELECT id FROM {self.table} WHERE 1 = 0", {}, timeout_s=timeout_s)

    def add(self, row: Row, *, timeout_s: float) -> None:
        sql = f"INSERT INTO {self.table} ({', '.join(COLUMNS)}) VALUES ({', '.join(map(self._bind, COLUMNS))})"
        binds: dict[str, Bind] = {name: _stored(value) for name, value in row.model_dump().items()}
        self.statements.execute(sql, binds, timeout_s=timeout_s)

    def due(self, now: datetime, limit: int, *, timeout_s: float) -> list[Row]:
        columns, where = ", ".join(COLUMNS), f"WHERE next_at <= {self._bind('now')} AND status = 'pending' ORDER BY next_at, id"
        if self.dialect == "mssql":
            sql = f"SELECT TOP ({self._bind('limit')}) {columns} FROM {self.table} {where}"
        else:
            sql = f"SELECT {columns} FROM {self.table} {where} FETCH FIRST {self._bind('limit')} ROWS ONLY"
        found = self.statements.query(sql, {"now": _stored(now), "limit": limit}, timeout_s=timeout_s)
        return [Row.model_validate(values) for values in found.to_pylist()]

    def claim(self, row: Row, until: datetime, *, timeout_s: float) -> bool:
        sql = f"UPDATE {self.table} SET attempts = {self._bind('next')}, next_at = {self._bind('until')} WHERE id = {self._bind('id')} AND attempts = {self._bind('seen')} AND status = 'pending'"
        binds: dict[str, Bind] = {"id": row.id, "seen": row.attempts, "next": row.attempts + 1, "until": _stored(until)}
        return self.statements.execute(sql, binds, timeout_s=timeout_s) == 1

    def accepted(self, row_id: str, *, attempts: int, at: datetime, timeout_s: float) -> bool:
        assignments = f"status = 'done', next_at = NULL, {self._set('attempts', 'ended_at')}"
        return self._update(assignments, {"id": row_id, "attempts": attempts, "ended_at": _stored(at)}, timeout_s=timeout_s)

    def refused(self, row_id: str, *, attempts: int, next_at: datetime, error: str, timeout_s: float) -> bool:
        binds: dict[str, Bind] = {"id": row_id, "attempts": attempts, "next_at": _stored(next_at), "last_error": error[:ERROR_LENGTH]}
        return self._update(self._set("attempts", "next_at", "last_error"), binds, timeout_s=timeout_s)

    def expire(self, row_id: str, *, at: datetime, timeout_s: float) -> bool:
        return self._update(f"status = 'expired', next_at = NULL, {self._set('ended_at')}", {"id": row_id, "ended_at": _stored(at)}, timeout_s=timeout_s)


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
    return Column(name, "str", nullable, pl.String(), max_length=length)


def _time(name: str, *, nullable: bool = False) -> Column:
    return Column(name, "datetime", nullable, pl.Datetime("us", "UTC"), zone="UTC")


_COLUMNS = (  # the table's, in the order of `Row`'s fields
    _text("id", 36), _text("service", 63), _text("method", 6), _text("path", PATH_LENGTH), _text("body", None, nullable=True),
    _text("idempotency_key", 255), _text("route", ROUTE_LENGTH), _text("request_id", 36, nullable=True), _time("created_at"),
    _time("expires_at"), _time("next_at", nullable=True), Column("attempts", "int", nullable=False, dtype=pl.Int64()), _text("status", 7),
    _text("last_error", ERROR_LENGTH, nullable=True), _time("ended_at", nullable=True),
)  # fmt: skip


def ddl(database: str, table: str, dialect: Dialect) -> str:
    """The outbox table's CREATE TABLE, keyed by id, and the index `basis relay` finds the rows due by: `next_at`, which
    is NULL once a row has ended, so that in Oracle the index holds only the rows still pending."""
    bare = table.rsplit(".", 1)[-1].lower()
    created = core_ddl.create_table(_Target(database, table, _COLUMNS), dialect)
    return f"{created}CREATE INDEX ix_{bare}_due ON {table} (next_at);\n"
