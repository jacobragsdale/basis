"""The resources a service declares: everything its handlers reach outside the process. Each is declared by name, with
everything it needs stated, and none of it guessed:

    WAREHOUSE = api.database("warehouse", deadline_s=5, max_concurrent=8)
    PRICING = api.http("pricing", deadline_s=2)
    TERMS = api.cache("bond_terms", Terms, ttl_s=300, max_entries=10_000, deadline_s=5)
    CLOCK = api.clock("clock")
    IDS = api.ids("ids")

    service = api.Service(name="accruals", settings=Settings, resources=[WAREHOUSE, PRICING, TERMS, CLOCK, IDS], routes=[...])

The app opens each one when it starts, and readiness reports 503 until all of them are open; it closes them when it
stops. Every call a handler makes through one that can wait has a deadline, the declared `deadline_s`, and every call
has a span of its own; for tests each resource has a fake, in `basis.api.fakes`. Another service, the clock and the
ids are basis-core's resources, which jobs declare too (`basis.core.resources`); the database's request transaction,
its outbox and the cache are the API's own. A handler asks for a resource with
`api.given`, as it asks for its settings, annotated with what it gets, and only in its own parameters, never in a
dependency of its own, so that a route's signature shows everything it reaches:

| Declared with | A handler asks for | Configured by |
|---|---|---|
| `api.database(name, deadline_s=, max_concurrent=)` | `api.Reader`, or `api.Writer` in a route that writes it | basis-core's `BASIS_DATABASES__NAME__*` |
| `api.http(name, deadline_s=)` | `api.Client` | `BASIS_HTTP__NAME__URL`, and `__CA_BUNDLE` and `__PROXY` where it needs them |
| `api.cache(name, Value, ttl_s=, max_entries=, deadline_s=)` | `api.Cache[Value]` | nothing: it is in the process |
| `api.clock(name)` | `api.Clock` | nothing |
| `api.ids(name)` | `api.Ids` | nothing |

A database's statements in one request share one transaction. A route that writes commits it only after its handler
returns and its response validates, and rolls it back when the handler raises or the response does not validate; a
route that reads never commits. A statement that fails, or does not finish, rolls the transaction back even when the
handler carries on, and a request whose writes were rolled back never answers 2xx. A request commits one transaction,
so a route writes one database at most, and a GET route writes none. The clock and the ids are resources so that a
recorded request replays the times and ids it saw; they never wait, so they have no deadline.

A call that changes another service's state goes after the commit, through the outbox of the database the route writes
(`Client.send`, and `basis.api.outboxes`), which the database declares with its table and how long its calls are retried:

    WAREHOUSE = api.database("warehouse", deadline_s=5, max_concurrent=8, outbox=api.outbox("accruals_outbox", max_age_s=86_400, backoff_s=60, max_backoff_s=3_600))

Database, HTTP and cache calls are awaited, so a handler that asks for one is `async def`."""

import dataclasses
import math
import re
import types
import typing
from collections.abc import Awaitable, Callable, Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from http import HTTPStatus
from typing import Annotated, Generic, Literal, Protocol, TypeAliasType, TypeVar, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel

from basis.api.compute import ComputeFunction
from basis.api.errors import Error, ServiceError
from basis.core import resources as core
from basis.core.resources import NAME, Body, ClockResource, HttpMethod, HttpResource, IdsResource, Query, QueryValue
from basis.core.settings import LEDGER as TABLE

__all__ = [
    "NAME", "TABLE", "TABLE_LENGTH", "Bind", "Body", "Cache", "CacheResource", "CallFailedError", "Client", "Clock", "ClockResource", "DatabaseResource",
    "DeadlineExceededError", "HttpMethod", "HttpResource", "Ids", "IdsResource", "Opened", "Outbox", "Query", "QueryValue", "Reader", "Resource", "SendMethod",
    "Session", "Writer", "cache", "check_outboxes", "clock", "database", "http", "ids", "outbox",
]  # fmt: skip

type Bind = str | int | float | Decimal | bool | date | datetime | UUID | bytes | None
"""A value a statement binds by name, as basis-core's `databases.Bind`, which a service's modules do not import."""
type SendMethod = Literal["POST", "PUT", "PATCH", "DELETE"]
"""The methods of a call that changes another service's state, which `Client.send` makes."""
TABLE_LENGTH = 125
"""The longest name of an outbox's table, after its schema, so that its key's constraint, `pk_NAME`, has the 128
characters SQL Server and Oracle allow a name."""


@dataclass(frozen=True)
class Outbox:
    """A database's outbox: the table where each call a route that writes the database sends to another service waits,
    from the request's commit until the service accepts it (see `Client.send` and `basis.api.outboxes`)."""

    table: str
    """The table, `name` or `schema.name`, as `basis ddl SERVICE --dialect DIALECT` creates it: the service's own,
    in the database, which the routes' write login writes."""
    max_age_s: float
    """Seconds from a call's commit in which it is still made: one its service has not accepted by then ages out, and
    `basis relay` makes it no more, and fails, naming the call and its key. It must be longer than the deadline of each
    route that writes the database, and a backoff after it, which the service checks."""
    backoff_s: float
    """Seconds after a call's first failed attempt before `basis relay` makes it again; each failure after doubles the
    wait, up to `max_backoff_s`."""
    max_backoff_s: float
    """The longest wait between two attempts at one call."""


@dataclass(frozen=True)
class DatabaseResource:
    name: str
    """The name its settings are under, as `BASIS_DATABASES__WAREHOUSE__KIND` for `warehouse`."""
    deadline_s: float
    """Seconds each statement may take, its connection's login and its wait for a thread included, and the commit or
    rollback that ends a request's transaction."""
    max_concurrent: int
    """The most of its statements that run at once, each on a thread of the database's own pool: what the database can
    take from one replica of the API. A statement that waits for a thread waits within its deadline."""
    outbox: Outbox | None = None
    """Where the calls its routes send to other services wait for their commit and their service, or None for a
    database whose routes send none."""


V_co = TypeVar("V_co", covariant=True)


@dataclass(frozen=True)
class CacheResource(Generic[V_co]):  # noqa: UP046 -- covariant, so any cache is a Resource; ty infers a frozen dataclass's parameter invariant
    name: str
    value: type[V_co]
    """What it holds: one value is shared by every request that reads it, so it never changes once made."""
    ttl_s: float
    """Seconds a value is kept after it is loaded."""
    max_entries: int
    """The most values it keeps; past that, the one read longest ago goes."""
    deadline_s: float
    """Seconds a read may take, waiting for a load included, whether its own or another request's."""


type Resource = DatabaseResource | HttpResource | CacheResource[object] | ClockResource | IdsResource


def database(name: str, *, deadline_s: float, max_concurrent: int, outbox: Outbox | None = None) -> DatabaseResource:
    """A database basis-core's settings configure under `name`, SQL Server or Oracle, whose statements a handler
    writes in that database's SQL; with `outbox`, where the calls its routes send wait (`api.outbox`)."""
    if isinstance(max_concurrent, bool) or not isinstance(max_concurrent, int) or max_concurrent < 1:
        msg = f"database {name} runs at most {max_concurrent!r} statements at once: give it max_concurrent, a whole number of 1 or more"
        raise ServiceError(msg)
    if outbox is not None and not isinstance(outbox, Outbox):
        msg = f"database {name}'s outbox is {outbox!r}: declare it with api.outbox(TABLE, max_age_s=..., backoff_s=..., max_backoff_s=...)"
        raise ServiceError(msg)
    return DatabaseResource(_named(name, "database"), _deadline(name, deadline_s), max_concurrent, outbox)


def outbox(table: str, *, max_age_s: float, backoff_s: float, max_backoff_s: float) -> Outbox:
    """A database's outbox, in `table`, whose calls are made until their service accepts them, for `max_age_s`
    seconds from their commit at most: again `backoff_s` seconds after a first failure, and each time after waiting
    twice as long as before, up to `max_backoff_s`."""
    if not isinstance(table, str) or not re.fullmatch(TABLE, table):
        msg = f"api.outbox({table!r}): an outbox's table is a name, or schema.name, of letters, digits, underscores and $, which its SQL names unquoted"
        raise ServiceError(msg)
    if len(bare := table.rpartition(".")[2]) > TABLE_LENGTH:
        msg = f"outbox {table}'s name is {len(bare)} characters: give it {TABLE_LENGTH} at most, after its schema, so its key's constraint, pk_NAME, fits the 128 both databases allow"
        raise ServiceError(msg)
    for field, seconds in (("max_age_s", max_age_s), ("backoff_s", backoff_s), ("max_backoff_s", max_backoff_s)):
        if isinstance(seconds, bool) or not isinstance(seconds, int | float) or not 0 < seconds < math.inf:
            msg = f"outbox {table}'s {field} is {seconds!r}: give it the seconds, above 0"
            raise ServiceError(msg)
    if backoff_s > max_backoff_s:
        msg = f"outbox {table} waits {backoff_s:g}s after a first failure, longer than its max_backoff_s of {max_backoff_s:g}s: give max_backoff_s at least backoff_s"
        raise ServiceError(msg)
    return Outbox(table, max_age_s, backoff_s, max_backoff_s)


def check_outboxes(service: str, written: Iterable[tuple[str, float, DatabaseResource]]) -> None:
    """That each outbox leaves room past the deadline of every route that writes its database, each `(route, deadline,
    database)`: a call whose request could not make it is due to `basis relay` when the route's deadline passes, and
    again a backoff after a failed attempt, so an outbox whose calls age out before then would have none made by the
    relay."""
    for route, deadline_s, database in written:
        if (declared := database.outbox) is not None and declared.max_age_s <= deadline_s + declared.backoff_s:
            msg = (
                f"service {service}'s outbox {declared.table} ages its calls out after {declared.max_age_s:g}s, and route {route}, which writes {database.name},"
                f" has {deadline_s:g}s to answer, after which a call it sent waits a backoff of {declared.backoff_s:g}s for basis relay:"
                f" give the outbox a max_age_s above {deadline_s + declared.backoff_s:g}s"
            )
            raise ServiceError(msg)


def http(name: str, *, deadline_s: float) -> HttpResource:
    """Another HTTP service, whose base URL is the setting `BASIS_HTTP__NAME__URL`, and which answers in JSON:
    basis-core's, as a job declares it too."""
    with _declaring():
        return core.http(name, deadline_s=deadline_s)


def cache[V](name: str, value: type[V], *, ttl_s: float, max_entries: int, deadline_s: float) -> CacheResource[V]:
    """Values of `value` kept in the process for `ttl_s` seconds, each loaded once however many requests ask for it
    at the same time."""
    _named(name, "cache")
    if ttl_s <= 0:
        msg = f"cache {name} keeps values for {ttl_s}s: give it ttl_s, the seconds a value is kept, above 0"
        raise ServiceError(msg)
    if max_entries < 1:
        msg = f"cache {name} keeps at most {max_entries} values: give it max_entries of 1 or more"
        raise ServiceError(msg)
    if not _immutable(value):
        held = getattr(value, "__qualname__", repr(value))
        msg = f"cache {name} holds {held}, which can change, and every request that reads it shares one: hold a frozen model or dataclass whose fields are too, a tuple of them, or a scalar"
        raise ServiceError(msg)
    return CacheResource(name, value, ttl_s, max_entries, _deadline(name, deadline_s))


_SCALARS = (str, bytes, int, float, Decimal, date, time, timedelta, UUID, types.NoneType, Enum)
"""Types whose values never change: a bool is an int, a datetime a date, and an Enum's members are fixed."""


def _immutable(kind: object, within: frozenset[type] = frozenset()) -> bool:
    """Whether no value of `kind` can change once made, as far as its type proves: a scalar, a frozen pydantic model or
    dataclass whose fields are immutable too, or a tuple, frozenset, union or literal of those. Anything else, such as
    a dict, a list or a plain class, cannot be proved so. `within` holds the models being checked, for one that refers
    to itself."""
    if isinstance(kind, TypeAliasType):
        return _immutable(kind.__value__, within)
    origin = get_origin(kind)
    if origin is Annotated:
        return _immutable(get_args(kind)[0], within)
    if origin is Literal:
        return True
    if origin in {typing.Union, types.UnionType, tuple, frozenset}:
        return all(argument is Ellipsis or _immutable(argument, within) for argument in get_args(kind))
    return origin is None and isinstance(kind, type) and _immutable_class(kind, within)


def _immutable_class(kind: type, within: frozenset[type]) -> bool:
    if kind in within or issubclass(kind, _SCALARS):
        return True
    if issubclass(kind, BaseModel):
        return kind.model_config.get("frozen") is True and all(_immutable(found.annotation, within | {kind}) for found in kind.model_fields.values())
    if dataclasses.is_dataclass(kind):
        frozen = getattr(getattr(kind, "__dataclass_params__", None), "frozen", False) is True
        hints = typing.get_type_hints(kind, include_extras=True)
        return frozen and all(_immutable(hints[found.name], within | {kind}) for found in dataclasses.fields(kind))
    return False


def clock(name: str) -> ClockResource:
    """The time, from the system's clock, or a fake's in a test: basis-core's."""
    with _declaring():
        return core.clock(name)


def ids(name: str) -> IdsResource:
    """New ids, each a UUIDv7, which sorts by when it was made: basis-core's."""
    with _declaring():
        return core.ids(name)


def _named(name: str, kind: str) -> str:
    with _declaring():
        return core.named(name, kind)


def _deadline(name: str, deadline_s: float) -> float:
    with _declaring():
        return core.seconds(name, deadline_s)


@contextmanager
def _declaring() -> Generator[None]:
    """basis-core's checks of a resource's declaration, whose failure is a service's: the one place their error becomes
    a `ServiceError`, so the API and basis-core never disagree on what a name or a deadline is."""
    try:
        yield
    except core.ResourceError as error:
        raise ServiceError(str(error)) from None


class Reader(Protocol):
    """A database, as a route that reads it sees it: queries in one transaction that only reads, which the app rolls
    back when the request ends. Each binds its values by name, `%(name)s` in SQL Server and `:name` in Oracle, and
    each row it returns is validated as `row`, whose fields are the columns' names in lower case."""

    async def fetch[R](self, row: type[R], sql: str, /, **binds: Bind) -> list[R]: ...

    async def fetch_one[R](self, row: type[R], sql: str, /, **binds: Bind) -> R | None:
        """The one row a query returns, or None when it returns none; more than one is an error."""
        ...


class Writer(Reader, Protocol):
    """A database, as a route that writes it sees it: its queries and its statements share one transaction, which
    commits after the handler returns and its response validates."""

    async def execute(self, sql: str, /, **binds: Bind) -> int:
        """The rows the statement changed."""
        ...


class Client(core.Client, Protocol):
    """Another HTTP service, as basis-core's `Client` calls it, and `send`. Each call's reply is validated as `reply`,
    from JSON; a reply that is not a 2xx, a service that cannot be reached, or a body that is not a `reply` raises
    `CallFailedError`. `path` is the service's own, after its base URL, and anything in it that a request supplied must
    be validated first: a path parameter's pattern, say. Each call carries the request's trace on, as `traceparent`."""

    async def send(self, method: SendMethod, path: str, body: Body = None, /, *, key: str) -> None:
        """A call that changes the other service's state, with `key` as its `Idempotency-Key`: the service drops a call
        whose key it has seen, so the call is safe to make more than once. `key` is one to 255 visible ASCII characters,
        such as the id of what the call is about.

        In a route that writes a database, the call is a row of that database's outbox, written in the request's
        transaction, so it commits or rolls back with the writes: once they commit, the call is made, still within the
        route's deadline, and if the service does not accept it then, `basis relay` makes it again until the service
        does, until it ages out, or until a 4xx but 408, 409, 425 or 429 refuses it for good. The request answers as if
        the call was made either way, and a call whose writes roll back is never made. In a route that writes no
        database, the call is made at once, and fails as `call` does. The reply's body is never read."""
        ...


class Cache[V](Protocol):
    async def get(self, key: str, load: Callable[[], Awaitable[V]], /) -> V:
        """The value kept for `key`, or the one `load` makes when none is, or when it is older than the cache's
        `ttl_s`. While one request loads a key, others that ask for it wait for that load. A load that fails keeps
        nothing, and every request waiting on it fails with it."""
        ...


Clock = core.Clock
Ids = core.Ids


class DeadlineExceededError(core.DeadlineExceededError, Error):
    """basis-core's, as the 504 problem a route answers."""

    status = HTTPStatus.GATEWAY_TIMEOUT
    title = "A call took longer than its deadline"

    @classmethod
    def of(cls, error: core.DeadlineExceededError) -> DeadlineExceededError:
        """basis-core's `error` as the problem it is in a request: the same resource, deadline and detail."""
        return error if isinstance(error, cls) else cls(error.resource, error.deadline_s, detail=str(error))


class CallFailedError(core.CallFailedError, Error):
    """Another service's failure, basis-core's, as the 502 problem a route answers: a handler that has something better
    to say, such as a 404 of its own when the other service has no such thing, catches it and raises its own error."""

    status = HTTPStatus.BAD_GATEWAY
    title = "A call to another service failed"

    @classmethod
    def of(cls, error: core.CallFailedError) -> CallFailedError:
        """basis-core's `error` as the problem it is in a request: the same detail, resource and reply."""
        return error if isinstance(error, cls) else cls(str(error), resource=error.resource, reply=error.reply)


class Opened(Protocol):
    """The resources a started app opened, which `api.given` hands each request."""

    def session(self, resource: DatabaseResource, *, writes: bool) -> Session: ...

    def handle(self, resource: HttpResource | CacheResource[object] | ClockResource | IdsResource) -> object: ...

    def compute(self, declared: ComputeFunction[..., object]) -> object:
        """What a handler awaits to run `declared`: in the pool, or in this process where a test fakes it."""
        ...


class Session(Writer, Protocol):
    """One request's transaction in one database."""

    async def end(self, *, commit: bool) -> None:
        """Commit, where `commit` and the route writes, and otherwise roll back; then give back the connection."""
        ...

    def abandon(self) -> None:
        """Roll back and give back the connection once nothing still runs on it, without waiting for it."""
        ...
