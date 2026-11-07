"""The resources a service declares: everything its handlers reach outside the process. Each is declared by name, with
everything it needs stated, and none of it guessed:

    WAREHOUSE = api.database("warehouse", deadline_s=5)
    PRICING = api.http("pricing", deadline_s=2)
    TERMS = api.cache("bond_terms", Terms, ttl_s=300, max_entries=10_000, deadline_s=5)
    CLOCK = api.clock("clock")
    IDS = api.ids("ids")

    service = api.Service(name="accruals", settings=Settings, resources=[WAREHOUSE, PRICING, TERMS, CLOCK, IDS], routes=[...])

The app opens each one when it starts, and readiness reports 503 until all of them are open; it closes them when it
stops. Every call a handler makes through one has a deadline, the declared `deadline_s`, and a span of its own, and
for tests each has a fake, in `basis.api.fakes`. A handler asks for a resource with `api.given`, as it asks for its
settings, annotated with what it gets:

| Declared with | A handler asks for | Configured by |
|---|---|---|
| `api.database(name, deadline_s=)` | `api.Reader`, or `api.Writer` in a route that writes it | basis-core's `BASIS_DATABASES__NAME__*` |
| `api.http(name, deadline_s=)` | `api.Client` | `BASIS_HTTP__NAME__URL` |
| `api.cache(name, Value, ttl_s=, max_entries=, deadline_s=)` | `api.Cache[Value]` | nothing: it is in the process |
| `api.clock(name)` | `api.Clock` | nothing |
| `api.ids(name)` | `api.Ids` | nothing |

A database's statements in one request share one transaction. A route that writes commits it only after its handler
returns and its response validates, and rolls it back when the handler raises or the response does not validate; a
route that reads never commits. A request commits one transaction, so a route writes one database at most, and a GET
route writes none. The clock and the ids are resources so that a recorded request replays the times and ids it saw;
they never wait, so they have no deadline.

Database, HTTP and cache calls are awaited, so a handler that asks for one is `async def`."""

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from http import HTTPStatus
from typing import Generic, Literal, Protocol, TypeVar
from uuid import UUID

from pydantic import BaseModel, JsonValue

from basis.api.errors import Error, ServiceError

NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")
"""Lower-case letters, digits and underscores, so that a resource's settings are variables every shell can set."""

type Bind = str | int | float | Decimal | bool | date | datetime | UUID | bytes | None
"""A value a statement binds by name, as basis-core's `databases.Bind`, which a service's modules do not import."""
type HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
type QueryValue = str | int | float | Decimal | bool | date | datetime | UUID
type Body = BaseModel | JsonValue


@dataclass(frozen=True)
class DatabaseResource:
    name: str
    """The name its settings are under, as `BASIS_DATABASES__WAREHOUSE__KIND` for `warehouse`."""
    deadline_s: float
    """Seconds each statement may take, its connection's login included."""


@dataclass(frozen=True)
class HttpResource:
    name: str
    """The name its settings are under, as `BASIS_HTTP__PRICING__URL` for `pricing`."""
    deadline_s: float
    """Seconds each call may take, from the first byte sent to the last byte of the reply."""


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


@dataclass(frozen=True)
class ClockResource:
    name: str


@dataclass(frozen=True)
class IdsResource:
    name: str


type Resource = DatabaseResource | HttpResource | CacheResource[object] | ClockResource | IdsResource


def database(name: str, *, deadline_s: float) -> DatabaseResource:
    """A database basis-core's settings configure under `name`, SQL Server or Oracle, whose statements a handler
    writes in that database's SQL."""
    return DatabaseResource(_named(name, "database"), _deadline(name, deadline_s))


def http(name: str, *, deadline_s: float) -> HttpResource:
    """Another HTTP service, whose base URL is the setting `BASIS_HTTP__NAME__URL`, and which answers in JSON."""
    return HttpResource(_named(name, "http"), _deadline(name, deadline_s))


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
    if (isinstance(value, type) and issubclass(value, BaseModel) and value.model_config.get("frozen") is not True) or (is_dataclass(value) and not value.__dataclass_params__.frozen):  # ty: ignore[unresolved-attribute] -- a dataclass type has its params
        msg = f"cache {name} holds {value.__qualname__}, which can change, and every request that reads it shares one: make it frozen"
        raise ServiceError(msg)
    return CacheResource(name, value, ttl_s, max_entries, _deadline(name, deadline_s))


def clock(name: str) -> ClockResource:
    """The time, from the system's clock, or a fake's in a test."""
    return ClockResource(_named(name, "clock"))


def ids(name: str) -> IdsResource:
    """New ids, each a UUIDv7, which sorts by when it was made."""
    return IdsResource(_named(name, "ids"))


def _named(name: str, kind: str) -> str:
    if not NAME.fullmatch(name):
        msg = f"api.{kind}({name!r}): a resource's name is lower-case letters, digits and underscores, starting with a letter, so its settings are variables any shell sets"
        raise ServiceError(msg)
    return name


def _deadline(name: str, deadline_s: float) -> float:
    if not deadline_s > 0:  # NaN too
        msg = f"{name}'s deadline is {deadline_s}s: give it deadline_s, the seconds each call may take, above 0"
        raise ServiceError(msg)
    return deadline_s


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


class Client(Protocol):
    """Another HTTP service. Each call's reply is validated as `reply`, from JSON; a reply that is not a 2xx, a
    service that cannot be reached, or a body that is not a `reply` raises `CallFailedError`. `path` is the service's own,
    after its base URL, and anything in it that a request supplied must be validated first: a path parameter's pattern,
    say. Each call carries the request's trace on, as `traceparent`."""

    async def call[T](self, method: HttpMethod, path: str, reply: type[T], /, *, query: Mapping[str, QueryValue | Sequence[QueryValue]] | None = None, body: Body = None) -> T: ...

    async def get[T](self, path: str, reply: type[T], /, *, query: Mapping[str, QueryValue | Sequence[QueryValue]] | None = None) -> T: ...

    async def post[T](self, path: str, reply: type[T], /, *, query: Mapping[str, QueryValue | Sequence[QueryValue]] | None = None, body: Body = None) -> T: ...


class Cache[V](Protocol):
    async def get(self, key: str, load: Callable[[], Awaitable[V]], /) -> V:
        """The value kept for `key`, or the one `load` makes when none is, or when it is older than the cache's
        `ttl_s`. While one request loads a key, others that ask for it wait for that load. A load that fails keeps
        nothing, and every request waiting on it fails with it."""
        ...


class Clock(Protocol):
    def now(self) -> datetime:
        """The time now, in UTC."""
        ...


class Ids(Protocol):
    def new(self) -> UUID:
        """A new UUIDv7."""
        ...


class DeadlineExceededError(Error):
    status = HTTPStatus.GATEWAY_TIMEOUT
    title = "A call took longer than its deadline"

    def __init__(self, resource: str, deadline_s: float) -> None:
        super().__init__(f"{resource} did not answer within its deadline of {deadline_s:g}s")
        self.resource = resource
        self.deadline_s = deadline_s


class CallFailedError(Error):
    """Another service's failure: a handler that has something better to say, such as a 404 of its own when the other
    service has no such thing, catches it and raises its own error."""

    status = HTTPStatus.BAD_GATEWAY
    title = "A call to another service failed"

    def __init__(self, detail: str, *, resource: str, reply: int | None) -> None:
        super().__init__(detail)
        self.resource = resource
        self.reply = reply
        """The status the other service replied with, or None when it gave no reply."""


class Opened(Protocol):
    """The resources a started app opened, which `api.given` hands each request."""

    def session(self, resource: DatabaseResource, *, writes: bool) -> Session: ...

    def handle(self, resource: HttpResource | CacheResource[object] | ClockResource | IdsResource) -> object: ...


class Session(Writer, Protocol):
    """One request's transaction in one database."""

    async def end(self, *, commit: bool) -> None:
        """Commit, where `commit` and the route writes, and otherwise roll back; then give back the connection."""
        ...

    def abandon(self) -> None:
        """Roll back and give back the connection once nothing still runs on it, without waiting for it."""
        ...
