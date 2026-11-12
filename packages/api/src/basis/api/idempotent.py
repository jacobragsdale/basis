"""A request that can be sent twice: an inbound `Idempotency-Key`, as the IETF's Idempotency-Key draft has it, kept in
the request's transaction with what the request answered, so a UI that lost an answer, to a dropped connection or a
timeout, sends the request again and gets the same answer, and the service changes nothing more.

A route that writes a database declares `idempotency="required"` or `"optional"`, and the database the table its keys
are kept in, `api.database(..., idempotency=api.idempotency(TABLE, keep_s=...))`, which `basis ddl` creates. A request
to it with an `Idempotency-Key` header:

1. reads its key's row, in the request's transaction, by the route, the user and the key, so two users' keys, or one
   user's keys to two routes, never meet. A row older than `keep_s` is deleted, and the request is a new one;
2. where it finds the row, and the request is the one it kept, the same method, path, query and body by its
   `fingerprint`, answers what the row holds, without running the handler: the same status, and the same JSON;
   where the request is another, refuses it, `IdempotencyKeyReusedError`, a 422 naming the header;
3. where it finds none, writes the row, which a second request with the key, before this one commits, waits on or
   collides with: a collision answers `IdempotencyKeyInUseError`, a 409, which a UI retries, as the draft says;
4. runs the handler, writes its answer into the row, and commits with the request's own writes, so the key is kept
   exactly when they are.

A request whose handler fails, or whose transaction rolls back, keeps nothing, so it runs again when sent again: only
a success is kept. A route that requires a key refuses a request without one, `IdempotencyKeyMissingError`, a 400; one
where it is optional runs each request without one as it comes. A key is one to 255 visible ASCII characters, as the
outbox's are, and any other is an invalid request, a 422 that names the header.

Rows are not swept: one older than `keep_s` is deleted when its key is sent again, and a table grows with the requests
that write. Each row is small, 64 characters of id and fingerprint, the key, the route, the user and the answer; a
repo removes old ones with its own job where that matters."""

import hashlib
from dataclasses import dataclass
from typing import Protocol

from pydantic import JsonValue

from basis.api.outboxes import KEY, ROUTE_LENGTH, Instant, _stored, _Target, _text, _time  # the outbox's own table helpers
from basis.core import ddl as core_ddl
from basis.core.databases.base import Bind, Statements
from basis.core.dialects import Dialect, of
from basis.core.model import Model
from basis.core.tables import Column

HEADER = "Idempotency-Key"
SCHEMA: dict[str, JsonValue] = {"type": "string", "minLength": 1, "maxLength": 255, "pattern": "^[!-~]+$"}
"""The header's value, as the document gives it: one to 255 visible ASCII characters, as `outboxes.KEY` reads one."""
SUBJECT_LENGTH = 255
COLUMNS = ("id", "idempotency_key", "route", "subject", "fingerprint", "created_at", "status_code", "body")


class Kept(Model):
    """One key's row: the request it was sent with, and what that request answered, once it has."""

    id: str
    """The key's id: a hash of the route, the user and the key, so each is its own."""
    idempotency_key: str
    route: str
    subject: str | None
    """The user who sent it, or None for an anonymous request."""
    fingerprint: str
    created_at: Instant
    status_code: int | None
    body: str | None
    """The JSON it answered, or None for a 204."""


def ident(route: str, subject: str | None, key: str) -> str:
    """The id of `key`, sent by `subject` to `route`."""
    return hashlib.sha256("\0".join((route, subject or "", key)).encode()).hexdigest()


def fingerprint(method: str, path: str, query: str, body: bytes) -> str:
    """The request as it was sent: its method, its path, its query and its body, each byte of it."""
    digest = hashlib.sha256()
    for part in (method.encode(), path.encode(), query.encode(), body):
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def valid(key: str) -> bool:
    return KEY.fullmatch(key) is not None


class Table(Protocol):
    """A database's table of keys, as one transaction reaches it."""

    def find(self, key_id: str, *, timeout_s: float) -> Kept | None: ...
    def add(self, row: Kept, *, timeout_s: float) -> None: ...
    def complete(self, key_id: str, status_code: int, body: str | None, *, timeout_s: float) -> None: ...
    def forget(self, key_id: str, *, timeout_s: float) -> None: ...


@dataclass(frozen=True)
class Sql:
    """The table in a SQL Server or Oracle database, through a transaction's statements: every value bound, and every
    time naive in UTC, as the table stores it."""

    statements: Statements
    table: str
    dialect: Dialect

    def _bind(self, name: str) -> str:
        return of(self.dialect).bind(name)

    def find(self, key_id: str, *, timeout_s: float) -> Kept | None:
        found = self.statements.query(f"SELECT {', '.join(COLUMNS)} FROM {self.table} WHERE id = {self._bind('id')}", {"id": key_id}, timeout_s=timeout_s)
        rows = found.to_pylist()
        return Kept.model_validate(rows[0]) if rows else None

    def add(self, row: Kept, *, timeout_s: float) -> None:
        sql = f"INSERT INTO {self.table} ({', '.join(COLUMNS)}) VALUES ({', '.join(map(self._bind, COLUMNS))})"
        binds: dict[str, Bind] = {name: _stored(value) for name, value in row.model_dump().items()}
        self.statements.execute(sql, binds, timeout_s=timeout_s)

    def complete(self, key_id: str, status_code: int, body: str | None, *, timeout_s: float) -> None:
        sql = f"UPDATE {self.table} SET status_code = {self._bind('status_code')}, body = {self._bind('body')} WHERE id = {self._bind('id')}"
        self.statements.execute(sql, {"id": key_id, "status_code": status_code, "body": body}, timeout_s=timeout_s)

    def forget(self, key_id: str, *, timeout_s: float) -> None:
        self.statements.execute(f"DELETE FROM {self.table} WHERE id = {self._bind('id')}", {"id": key_id}, timeout_s=timeout_s)


_COLUMNS = (  # the table's, in the order of `Kept`'s fields
    _text("id", 64), _text("idempotency_key", 255), _text("route", ROUTE_LENGTH), _text("subject", SUBJECT_LENGTH, nullable=True), _text("fingerprint", 64),
    _time("created_at"), Column("status_code", "float", nullable=True), _text("body", None, nullable=True),
)  # fmt: skip


def ddl(database: str, table: str, dialect: Dialect) -> str:
    """The table's CREATE TABLE, keyed by id."""
    return core_ddl.create_table(_Target(database, table, _COLUMNS), dialect)
