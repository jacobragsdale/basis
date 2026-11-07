"""The resources an app opens when it starts, through basis-core's databases, an httpx2 client, the process's memory
and the system's clock, or through the fakes a test gives; and what every call through one gets, real or fake: a
deadline, and a span of its own on the app's tracer.

A deadline is the declared `deadline_s`, from when the call is made: a call that has not finished by then raises
`DeadlineExceededError`, a 504 problem. The database drivers block, so a database's statements run in worker threads, one
at a time on the request's one connection, and the driver's own timeout, set to what is left of the deadline, stops a
statement on the server too. A statement that misses its deadline may still be running when the handler hears so:
its connection takes no more statements, and the request's transaction rolls back once it ends, never committing,
even when the handler carries on.

`plan` checks, when the app is built, that the settings configure every resource that has no fake, so that a missing
setting stops the app before it serves; `Resources.open`, in the app's lifespan, opens them, checking that each
database answers, through each login the routes use, within its deadline."""

import asyncio
import contextvars
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol, assert_never
from uuid import UUID, uuid7

import httpx2
import pyarrow as pa
from opentelemetry.trace import SpanKind, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic_core import to_json

from basis.api import fakes
from basis.api.resources import Bind, Body, CacheResource, CallFailedError, ClockResource, DatabaseResource, DeadlineExceededError, HttpMethod, HttpResource, IdsResource, QueryValue, Resource
from basis.api.service import Service, ServiceError
from basis.api.settings import Settings
from basis.core.databases import Statements, open_database
from basis.core.settings import MssqlSettings, SettingsError

log = logging.getLogger(__name__)
_PROPAGATOR = TraceContextTextMapPropagator()
_SYSTEMS = {"mssql": "microsoft.sql_server", "oracle": "oracle.db"}
"""OpenTelemetry's `db.system.name` for each kind of database."""


class DatabasePort(Protocol):
    """What a database resource reaches: basis-core's database, or a fake."""

    def statements(self, *, writes: bool) -> AbstractContextManager[Statements]: ...


@dataclass(frozen=True)
class Plan:
    """How an app opens its resources: each name's fake, where a test gave one, and otherwise its settings."""

    service: Service
    settings: Settings
    fakes: Mapping[str, fakes.Fake]


def plan(service: Service, settings: Settings, given: Mapping[Resource, fakes.Fake]) -> Plan:
    """The resources' plan, after checking that each fake fakes a resource of the service's, of its kind, and that the
    settings configure every resource that has none: a database, and its write login where a route writes it, and
    another service's URL."""
    by_name: dict[str, fakes.Fake] = {}
    for resource, fake in given.items():
        if resource not in service.resources:
            msg = f"a fake is given for {resource.name}, which service {service.name} does not declare: fake the service's own resources"
            raise ServiceError(msg)
        if not _fakes(resource, fake):
            msg = f"{resource.name} is given a fakes.{type(fake).__name__}, which does not fake its kind of resource: give it the fake of its kind"
            raise ServiceError(msg)
        by_name[resource.name] = fake
    written = {route.writes.name for route in service.routes if route.writes is not None}
    unset: list[str] = []
    for resource in service.resources:
        if resource.name in by_name:
            continue
        variable = resource.name.upper()
        match resource:
            case DatabaseResource():
                if (database := settings.databases.get(resource.name)) is None:
                    unset.append(f"BASIS_DATABASES__{variable}__KIND and the rest of its block, for the database {resource.name}")
                elif resource.name in written and database.write is None:
                    login = "CONNECTION_STRING" if isinstance(database, MssqlSettings) else "USER and __PASSWORD"
                    unset.append(f"BASIS_DATABASES__{variable}__WRITE__{login}, the login through which routes write {resource.name}")
            case HttpResource():
                if resource.name not in settings.http:
                    unset.append(f"BASIS_HTTP__{variable}__URL, the base URL of the service {resource.name}")
            case CacheResource() | ClockResource() | IdsResource():
                pass
    if unset:
        msg = f"service {service.name}'s resources are not all configured, so the API cannot start: set {'; '.join(unset)}"
        raise SettingsError(msg)
    return Plan(service, settings, by_name)


def _fakes(resource: Resource, fake: fakes.Fake) -> bool:
    match resource:
        case DatabaseResource():
            return isinstance(fake, fakes.Database)
        case HttpResource():
            return isinstance(fake, fakes.Http)
        case CacheResource():
            return isinstance(fake, fakes.Cache)
        case ClockResource():
            return isinstance(fake, fakes.Clock)
        case IdsResource():
            return isinstance(fake, fakes.Ids)
        case unreachable:
            assert_never(unreachable)


class Resources:
    """The resources an app's lifespan opened, which `api.given` hands its requests."""

    def __init__(self, tracer: Tracer, *, linger_s: float | None) -> None:
        self._tracer = tracer
        self._databases: dict[str, tuple[DatabasePort, str]] = {}
        self._handles: dict[str, object] = {}
        self._clients: list[httpx2.AsyncClient] = []
        self._closing: set[asyncio.Task[None]] = set()
        """Sessions rolling back once a statement that missed its deadline ends, which shutdown waits for."""
        self._linger_s = linger_s
        """How long shutdown waits for them: a login and a statement, at the longest the settings allow, or, where every
        database is a fake, until they end, which the fakes' answers decide."""

    @classmethod
    async def open(cls, planned: Plan, tracer: Tracer) -> Resources:
        real = [planned.settings.databases[resource.name] for resource in planned.service.resources if isinstance(resource, DatabaseResource) and resource.name not in planned.fakes]
        opened = cls(tracer, linger_s=2 * max(database.timeout_s for database in real) if real else None)
        written = {route.writes.name for route in planned.service.routes if route.writes is not None}
        try:
            for resource in planned.service.resources:
                await opened._open(resource, planned, writes=resource.name in written)
        except BaseException:
            await opened.close()
            raise
        return opened

    async def _open(self, resource: Resource, planned: Plan, *, writes: bool) -> None:
        fake = planned.fakes.get(resource.name)
        tracer = self._tracer
        match resource:
            case DatabaseResource():
                if isinstance(fake, fakes.Database):
                    port: DatabasePort = fake
                    system = "fake"
                else:
                    port = open_database(planned.settings, resource.name)
                    system = _SYSTEMS[planned.settings.database(resource.name).kind]
                await _answers(resource, port, system, tracer, writes=writes)
                self._databases[resource.name] = (port, system)
            case HttpResource():
                if isinstance(fake, fakes.Http):
                    client = fake.client(resource.name)
                else:
                    base = planned.settings.http[resource.name].url
                    client = httpx2.AsyncClient(base_url=str(base), trust_env=False)  # basis's settings alone, never HTTP_PROXY and its kind
                self._clients.append(client)
                self._handles[resource.name] = HttpClient(resource, client, tracer)
            case CacheResource():
                monotonic = fake.monotonic if isinstance(fake, fakes.Cache) else time.monotonic
                self._handles[resource.name] = MemoryCache(resource, monotonic, tracer)
            case ClockResource():
                self._handles[resource.name] = SystemClock(resource, fake.now if isinstance(fake, fakes.Clock) else _utc_now, tracer)
            case IdsResource():
                self._handles[resource.name] = NewIds(resource, fake.new if isinstance(fake, fakes.Ids) else uuid7, tracer)
            case unreachable:
                assert_never(unreachable)

    def session(self, resource: DatabaseResource, *, writes: bool) -> DatabaseSession:
        port, system = self._databases[resource.name]
        return DatabaseSession(resource, port, writes=writes, system=system, tracer=self._tracer, closing=self._closing)

    def handle(self, resource: HttpResource | CacheResource[object] | ClockResource | IdsResource) -> object:
        return self._handles[resource.name]

    async def close(self) -> None:
        """Close the HTTP clients, and wait, for as long as a login and a statement may take, for the sessions still
        rolling back after a statement missed its deadline."""
        for client in self._clients:
            await client.aclose()
        if self._closing:
            _, waiting = await asyncio.wait(self._closing, timeout=self._linger_s)
            if waiting:
                log.warning("%s database connections still run statements that missed their deadlines", len(waiting), extra={"connections": len(waiting)})


async def _answers(resource: DatabaseResource, port: DatabasePort, system: str, tracer: Tracer, *, writes: bool) -> None:
    """Check that the database answers within its deadline, through its read login, and its write login too where a
    route writes it: a wrong setting fails at startup, not at the first request."""

    def connect() -> None:
        for login in (False, True) if writes else (False,):
            with port.statements(writes=login):
                pass

    with tracer.start_as_current_span(f"{resource.name} open", kind=SpanKind.CLIENT, attributes={"db.system.name": system, "basis.resource": resource.name}):
        connecting = _in_thread(connect)
        deadline = asyncio.timeout(resource.deadline_s)
        try:
            async with deadline:
                await asyncio.shield(connecting)
        except TimeoutError as error:
            if deadline.expired():
                raise DeadlineExceededError(resource.name, resource.deadline_s) from error
            raise


class DatabaseSession:
    """One request's transaction in one database, on one connection, which the first statement opens. Its statements
    run one at a time, each in a worker thread, under its own deadline. `end` commits it, where the route writes, or
    rolls it back."""

    def __init__(self, resource: DatabaseResource, port: DatabasePort, *, writes: bool, system: str, tracer: Tracer, closing: set[asyncio.Task[None]]) -> None:  # noqa: PLR0913 -- what the session runs through, each by keyword
        self._resource = resource
        self._port = port
        self._writes = writes
        self._system = system
        self._tracer = tracer
        self._closing = closing
        self._turn = asyncio.Lock()
        self._stack: ExitStack | None = None
        self._statements: Statements | None = None
        self._running: asyncio.Task[None] | None = None
        """Ends when a statement that missed its deadline, and may still be running on the connection, ends."""
        self._missed = False
        self._ended = False

    async def fetch[R](self, row: type[R], sql: str, /, **binds: Bind) -> list[R]:
        table = await self._call("fetch", sql, lambda statements, timeout_s: statements.query(sql, binds, timeout_s=timeout_s))
        adapter = TypeAdapter(row)
        try:
            return [adapter.validate_python(found) for found in table.to_pylist()]
        except ValidationError as error:
            error.add_note(f"the rows database {self._resource.name} returned are not {getattr(row, '__qualname__', row)}: match the query's columns, in lower case, to its fields")
            raise

    async def fetch_one[R](self, row: type[R], sql: str, /, **binds: Bind) -> R | None:
        rows = await self.fetch(row, sql, **binds)
        if len(rows) > 1:
            msg = f"a query of database {self._resource.name} returned {len(rows)} rows, and fetch_one takes one at most: filter it to one, or fetch them all"
            raise LookupError(msg)
        return rows[0] if rows else None

    async def execute(self, sql: str, /, **binds: Bind) -> int:
        if not self._writes:
            msg = f"database {self._resource.name}'s transaction only reads: a route writes it by asking for an api.Writer, and is not a GET"
            raise RuntimeError(msg)
        return await self._call("execute", sql, lambda statements, timeout_s: statements.execute(sql, binds, timeout_s=timeout_s))

    async def _call[T](self, operation: str, sql: str, work: Callable[[Statements, float], T]) -> T:
        resource = self._resource
        attributes = {"db.system.name": self._system, "db.query.text": sql, "basis.resource": resource.name, "basis.deadline_s": resource.deadline_s}
        with self._tracer.start_as_current_span(f"{resource.name} {operation}", kind=SpanKind.CLIENT, attributes=attributes) as span:
            async with self._turn:
                self._check_usable()
                deadline_at = time.monotonic() + resource.deadline_s
                running = _in_thread(lambda: self._run(work, deadline_at))
                deadline = asyncio.timeout(resource.deadline_s)
                try:
                    async with deadline:
                        result = await asyncio.shield(running)
                except TimeoutError as error:  # the deadline, or a login that timed out on its own
                    self._missed = True
                    if not running.done():
                        self._running = asyncio.get_running_loop().create_task(_ended(running))
                    raise DeadlineExceededError(resource.name, resource.deadline_s) from error
                except Exception as error:
                    if time.monotonic() >= deadline_at:  # the driver's own timeout, at the deadline
                        self._missed = True
                        raise DeadlineExceededError(resource.name, resource.deadline_s) from error
                    raise
                except BaseException:  # cancelled from outside, as when the server stops at once
                    if not running.done():
                        self._running = asyncio.get_running_loop().create_task(_ended(running))
                    raise
            if isinstance(result, pa.Table):
                span.set_attribute("db.response.returned_rows", result.num_rows)
            return result

    def _run[T](self, work: Callable[[Statements, float], T], deadline_at: float) -> T:
        """In a worker thread: open the connection on the first statement, and run `work` with what is left of the
        deadline, which the driver's timeout enforces."""
        statements = self._statements
        if statements is None:
            stack = ExitStack()
            statements = stack.enter_context(self._port.statements(writes=self._writes))
            self._stack, self._statements = stack, statements
        if (left_s := deadline_at - time.monotonic()) <= 0:
            msg = f"the deadline passed while database {self._resource.name} connected"
            raise TimeoutError(msg)
        return work(statements, left_s)

    def _check_usable(self) -> None:
        if self._ended:
            msg = f"the request's transaction in database {self._resource.name} has ended: a resource serves the request that was given it, and only while it runs"
            raise RuntimeError(msg)
        if self._running is not None:
            msg = f"database {self._resource.name} is still running a statement that missed its deadline, on the request's one connection: after a DeadlineExceededError, make no more calls to it"
            raise RuntimeError(msg)

    async def end(self, *, commit: bool) -> None:
        async with self._turn:
            if self._ended:
                return
            self._ended = True
            if self._running is not None:
                self._close_after(self._running)
            elif self._stack is not None:
                stack, statements = self._stack, self._statements
                if commit and self._writes and not self._missed and statements is not None:
                    with self._tracer.start_as_current_span(f"{self._resource.name} commit", kind=SpanKind.CLIENT, attributes={"db.system.name": self._system, "basis.resource": self._resource.name}):
                        await _finish(lambda: _commit(statements, stack))
                else:
                    try:
                        await _finish(stack.close)
                    except Exception:  # a connection a timeout broke: closing it ends the transaction, and the request's own error is the one to answer
                        log.warning("could not roll back database %s's transaction; its connection is closed", self._resource.name, exc_info=True, extra={"resource": self._resource.name})
            if commit and self._writes and self._missed:
                raise DeadlineExceededError(self._resource.name, self._resource.deadline_s)  # what changed is rolled back, never committed

    def abandon(self) -> None:
        if self._ended:
            return
        self._ended = True
        self._close_after(self._running)

    def _close_after(self, running: asyncio.Task[None] | None) -> None:
        """Roll back and give back the connection once the statement still running on it ends, without waiting for it."""
        task = asyncio.get_running_loop().create_task(self._close_when_done(running))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def _close_when_done(self, running: asyncio.Task[None] | None) -> None:
        if running is not None:
            await running
        if (stack := self._stack) is not None:
            try:
                await _in_thread(stack.close)
            except Exception:  # nothing waits on this: the request has its answer, and the connection is closed with it
                log.warning("could not roll back database %s's transaction after a statement missed its deadline", self._resource.name, exc_info=True, extra={"resource": self._resource.name})


async def _ended[T](running: asyncio.Future[T]) -> None:
    """Wait for a statement that missed its deadline to end, however it ends: its outcome no longer matters."""
    await asyncio.wait([running])
    if not running.cancelled():
        running.exception()  # retrieved, so the loop logs no warning of an error nobody read


def _commit(statements: Statements, stack: ExitStack) -> None:
    with stack:
        statements.commit()


async def _finish(work: Callable[[], object]) -> None:
    """Run `work` in a worker thread to its end, even when the request is cancelled meanwhile: a transaction is always
    committed or rolled back, and its connection given back."""
    await asyncio.shield(_in_thread(work))


class HttpClient:
    def __init__(self, resource: HttpResource, client: httpx2.AsyncClient, tracer: Tracer) -> None:
        self._resource = resource
        self._client = client
        self._tracer = tracer

    async def get[T](self, path: str, reply: type[T], /, *, query: Mapping[str, QueryValue | Sequence[QueryValue]] | None = None) -> T:
        return await self.call("GET", path, reply, query=query)

    async def post[T](self, path: str, reply: type[T], /, *, query: Mapping[str, QueryValue | Sequence[QueryValue]] | None = None, body: Body = None) -> T:
        return await self.call("POST", path, reply, query=query, body=body)

    async def call[T](self, method: HttpMethod, path: str, reply: type[T], /, *, query: Mapping[str, QueryValue | Sequence[QueryValue]] | None = None, body: Body = None) -> T:
        resource = self._resource
        attributes = {"http.request.method": method, "url.path": path, "basis.resource": resource.name, "basis.deadline_s": resource.deadline_s}
        with self._tracer.start_as_current_span(f"{resource.name} {method}", kind=SpanKind.CLIENT, attributes=attributes) as span:
            headers: dict[str, str] = {}
            _PROPAGATOR.inject(headers)  # this call's span, as the other service's parent
            content: bytes | None = None
            if body is not None:
                content = body.model_dump_json().encode() if isinstance(body, BaseModel) else to_json(body)
                headers["content-type"] = "application/json"
            deadline = asyncio.timeout(resource.deadline_s)
            try:
                async with deadline:
                    response = await self._client.request(method, path, params=_query(query), content=content, headers=headers, timeout=resource.deadline_s)
            except TimeoutError as error:
                if deadline.expired():
                    raise DeadlineExceededError(resource.name, resource.deadline_s) from error
                raise
            except httpx2.TimeoutException as error:  # one of the client's own timeouts, each the deadline
                raise DeadlineExceededError(resource.name, resource.deadline_s) from error
            except httpx2.TransportError as error:
                msg = f"{resource.name} could not be reached for {method} {path}: {type(error).__name__}"
                raise CallFailedError(msg, resource=resource.name, reply=None) from error
            span.set_attribute("http.response.status_code", response.status_code)
            if not response.is_success:
                log.warning(
                    "%s answered %s to %s %s", resource.name, response.status_code, method, path, extra={"resource": resource.name, "reply": response.status_code, "body": response.text[:2000]}
                )
                msg = f"{resource.name} answered {response.status_code} to {method} {path}"
                raise CallFailedError(msg, resource=resource.name, reply=response.status_code)
            try:
                return TypeAdapter(reply).validate_json(response.content)
            except ValidationError as error:
                name = getattr(reply, "__qualname__", str(reply))
                log.warning(
                    "%s answered %s %s with a body that is not a %s",
                    resource.name,
                    method,
                    path,
                    name,
                    extra={"resource": resource.name, "errors": error.errors(include_url=False, include_input=False)},
                )
                msg = f"{resource.name} answered {method} {path} with a body that is not a {name}"
                raise CallFailedError(msg, resource=resource.name, reply=response.status_code) from error


def _query(query: Mapping[str, QueryValue | Sequence[QueryValue]] | None) -> dict[str, str | list[str]] | None:
    if query is None:
        return None
    encoded: dict[str, str | list[str]] = {}
    for name, value in query.items():
        encoded[name] = _text(value) if isinstance(value, str | int | float | Decimal | date | UUID) else [_text(item) for item in value]
    return encoded


def _text(value: QueryValue) -> str:
    match value:
        case bool():
            return "true" if value else "false"
        case date():  # a datetime too
            return value.isoformat()
        case _:
            return str(value)


class _Abandoned(Exception):  # noqa: N818 -- not an error a caller sees: a waiter loads the value itself
    """The request loading a value stopped before it finished."""


class MemoryCache[V]:
    """Values kept in the process, by key, each until its time to live passes, and the most recently read up to
    `max_entries`. The event loop is the only thread that touches it."""

    def __init__(self, resource: CacheResource[V], monotonic: Callable[[], float], tracer: Tracer) -> None:
        self._resource = resource
        self._monotonic = monotonic
        self._tracer = tracer
        self._entries: OrderedDict[str, tuple[float, V]] = OrderedDict()
        self._loading: dict[str, asyncio.Future[V]] = {}

    async def get(self, key: str, load: Callable[[], Awaitable[V]], /) -> V:
        resource = self._resource
        with self._tracer.start_as_current_span(f"{resource.name} get", attributes={"basis.resource": resource.name, "basis.deadline_s": resource.deadline_s}) as span:
            deadline = asyncio.timeout(resource.deadline_s)
            try:
                async with deadline:
                    value, hit = await self._get(key, load)
            except TimeoutError as error:
                if deadline.expired():
                    raise DeadlineExceededError(resource.name, resource.deadline_s) from error
                raise
            span.set_attribute("basis.cache.hit", hit)
            return value

    async def _get(self, key: str, load: Callable[[], Awaitable[V]]) -> tuple[V, bool]:
        while True:
            if (entry := self._entries.get(key)) is not None:
                expires, value = entry
                if self._monotonic() < expires:
                    self._entries.move_to_end(key)
                    return value, True
                del self._entries[key]
            if (loading := self._loading.get(key)) is not None:
                try:
                    return await asyncio.shield(loading), False
                except _Abandoned:
                    continue  # the request that was loading it stopped: load it here
            loaded = asyncio.Future[V]()  # on the running loop
            self._loading[key] = loaded
            try:
                value = await load()
            except BaseException as error:
                del self._loading[key]
                loaded.set_exception(error if isinstance(error, Exception) else _Abandoned())
                loaded.exception()  # retrieved, whether or not another request waits on it
                raise
            del self._loading[key]
            self._entries[key] = (self._monotonic() + self._resource.ttl_s, value)
            if len(self._entries) > self._resource.max_entries:
                self._entries.popitem(last=False)
            loaded.set_result(value)
            return value, False


class SystemClock:
    def __init__(self, resource: ClockResource, now: Callable[[], datetime], tracer: Tracer) -> None:
        self._resource = resource
        self._now = now
        self._tracer = tracer

    def now(self) -> datetime:
        with self._tracer.start_as_current_span(f"{self._resource.name} now", attributes={"basis.resource": self._resource.name}) as span:
            value = self._now()
            span.set_attribute("basis.value", value.isoformat())
            return value


class NewIds:
    def __init__(self, resource: IdsResource, new: Callable[[], UUID], tracer: Tracer) -> None:
        self._resource = resource
        self._new = new
        self._tracer = tracer

    def new(self) -> UUID:
        with self._tracer.start_as_current_span(f"{self._resource.name} new", attributes={"basis.resource": self._resource.name}) as span:
            value = self._new()
            span.set_attribute("basis.value", str(value))
            return value


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _in_thread[T](work: Callable[[], T]) -> asyncio.Future[T]:
    """`work` in the loop's worker threads, with this context's log fields."""
    context = contextvars.copy_context()
    return asyncio.get_running_loop().run_in_executor(None, context.run, work)
