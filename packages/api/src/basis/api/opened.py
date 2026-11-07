"""The resources an app opens when it starts, through basis-core's databases, an httpx2 client, the process's memory
and the system's clock, or through the fakes a test gives; and what every call through one gets, real or fake: a span
of its own on the app's tracer, and, for every call that can wait, a deadline.

A deadline is the declared `deadline_s`, from when the call is made: a call that has not finished by then raises
`DeadlineExceededError`, a 504 problem. The database drivers block, so a database's statements run on a thread pool of
the database's own, `max_concurrent` threads, one statement at a time on the request's one connection; waiting for a
thread counts against the statement's deadline, and the driver's own timeout, set to what is left of it, stops the
statement on the server too. The commit or rollback that ends the request's transaction has the same deadline.

A statement that fails, misses its deadline or is cancelled leaves the transaction unfit to commit: it rolls back,
once the statement has ended if it still runs, and a request that would have committed answers an error instead of a
2xx, even when the handler caught the statement's error and carried on. A commit that misses its deadline is a 504,
and whether it committed in the end is logged when it ends.

`plan` checks, when the app is built, that the settings configure every resource that has no fake, so that a missing
setting stops the app before it serves; `Resources.open`, in the app's lifespan, opens them, after the keys its users'
tokens verify with, checking that each database answers, through each login the routes use, within its deadline."""

import asyncio
import contextvars
import logging
import ssl
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal, Protocol, assert_never
from uuid import UUID, uuid7

import httpx2
import pyarrow as pa
from opentelemetry.trace import Span, SpanKind, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic_core import to_json

from basis.api import fakes
from basis.api.resources import Bind, Body, CacheResource, CallFailedError, ClockResource, DatabaseResource, DeadlineExceededError, HttpMethod, HttpResource, IdsResource, QueryValue, Resource
from basis.api.service import Service, ServiceError
from basis.api.settings import HttpService, Settings
from basis.core.databases import Statements, open_database
from basis.core.settings import MssqlSettings, SettingsError, variable

log = logging.getLogger(__name__)
_PROPAGATOR = TraceContextTextMapPropagator()
_SYSTEMS = {"mssql": "microsoft.sql_server", "oracle": "oracle.db"}
"""OpenTelemetry's `db.system.name` for each kind of database."""


class Opens(Protocol):
    """What the app opens before its resources and closes after them: the keys its users' tokens verify with."""

    async def open(self, tracer: Tracer) -> None: ...

    async def close(self) -> None: ...


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
    unset = [found for resource in service.resources if resource.name not in by_name and (found := _unset(resource, settings, written=resource.name in written)) is not None]
    if unset:
        msg = f"service {service.name}'s resources are not all configured, so the API cannot start: set {'; '.join(unset)}"
        raise SettingsError(msg)
    return Plan(service, settings, by_name)


def _unset(resource: Resource, settings: Settings, *, written: bool) -> str | None:
    """The settings `resource` needs that are not set, by their variables, or None."""
    match resource:
        case DatabaseResource():
            if (database := settings.databases.get(resource.name)) is None:
                return f"{variable(('databases', resource.name, 'kind'))} and the rest of its block, for the database {resource.name}"
            if written and database.write is None:
                logins = ("connection_string",) if isinstance(database, MssqlSettings) else ("user", "password")
                return f"{' and '.join(variable(('databases', resource.name, 'write', part)) for part in logins)}, the login through which routes write {resource.name}"
            return None
        case HttpResource():
            return None if resource.name in settings.http else f"{variable(('http', resource.name, 'url'))}, the base URL of the service {resource.name}"
        case CacheResource() | ClockResource() | IdsResource():
            return None
        case unreachable:
            assert_never(unreachable)


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


def client(name: str, service: HttpService) -> httpx2.AsyncClient:
    """The client of the service the settings name `name`: its certificate checked against the declared CA bundle or
    the operating system's trusted CAs, through the declared proxy or none, and nothing read from the environment."""
    verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=service.ca_bundle) if service.ca_bundle is not None else True
    proxy = str(service.proxy) if service.proxy is not None else None
    log.debug("calls %s at %s", name, service.url, extra={"resource": name, "ca_bundle": str(service.ca_bundle) if service.ca_bundle else None, "proxy": proxy})
    return httpx2.AsyncClient(base_url=str(service.url), verify=verify, proxy=proxy, trust_env=False)


@dataclass
class OpenDatabase:
    """A database the app opened: what it reaches, its kind for spans, and its own pool of threads."""

    resource: DatabaseResource
    port: DatabasePort
    system: str
    tracer: Tracer
    closing: set[asyncio.Task[None]]
    """Work that outlives its request, such as the rollback after a statement that missed its deadline, which shutdown
    waits for."""
    pool: ThreadPoolExecutor = field(init=False)

    def __post_init__(self) -> None:
        self.pool = ThreadPoolExecutor(max_workers=self.resource.max_concurrent, thread_name_prefix=f"basis-{self.resource.name}")

    def run[T](self, work: Callable[[], T]) -> asyncio.Future[T]:
        """`work` on one of the database's threads, once one is free, with this context's log fields."""
        context = contextvars.copy_context()
        return asyncio.get_running_loop().run_in_executor(self.pool, context.run, work)

    def outlive(self, work: Coroutine[object, object, None]) -> None:
        """Run `work` after the request that started it, and have shutdown wait for it."""
        task = asyncio.get_running_loop().create_task(work)
        self.closing.add(task)
        task.add_done_callback(self.closing.discard)

    def span(self, operation: str, **attributes: str | float) -> AbstractContextManager[Span]:
        resource = self.resource
        return self.tracer.start_as_current_span(
            f"{resource.name} {operation}", kind=SpanKind.CLIENT, attributes={"db.system.name": self.system, "basis.resource": resource.name, "basis.deadline_s": resource.deadline_s} | attributes
        )


class Resources:
    """The resources an app's lifespan opened, which `api.given` hands its requests."""

    def __init__(self, tracer: Tracer, *, linger_s: float | None) -> None:
        self._tracer = tracer
        self._databases: dict[str, OpenDatabase] = {}
        self._handles: dict[str, object] = {}
        self._clients: list[httpx2.AsyncClient] = []
        self._closing: set[asyncio.Task[None]] = set()
        self._first: Opens | None = None
        self._linger_s = linger_s
        """How long shutdown waits for work that outlived its request: a login and a statement, at the longest the
        settings allow, or, where every database is a fake, until it ends, which the fakes' answers decide."""

    @classmethod
    async def open(cls, planned: Plan, tracer: Tracer, first: Opens | None = None) -> Resources:
        """The resources `planned` opened, after `first`, which they close last."""
        real = [planned.settings.databases[resource.name] for resource in planned.service.resources if isinstance(resource, DatabaseResource) and resource.name not in planned.fakes]
        opened = cls(tracer, linger_s=2 * max(database.timeout_s for database in real) if real else None)
        written = {route.writes.name for route in planned.service.routes if route.writes is not None}
        try:
            if first is not None:
                opened._first = first  # closed whether or not it opened, which a failed fetch leaves half done
                await first.open(tracer)
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
                database = OpenDatabase(resource, port, system, tracer, self._closing)
                self._databases[resource.name] = database
                await _answers(database, writes=writes)
            case HttpResource():
                opened = fake.client(resource.name) if isinstance(fake, fakes.Http) else client(resource.name, planned.settings.http[resource.name])
                self._clients.append(opened)
                self._handles[resource.name] = HttpClient(resource, opened, tracer)
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
        return DatabaseSession(self._databases[resource.name], writes=writes)

    def handle(self, resource: HttpResource | CacheResource[object] | ClockResource | IdsResource) -> object:
        return self._handles[resource.name]

    async def close(self) -> None:
        """Close the HTTP clients, wait for the work that outlived its request, and then stop each database's threads."""
        for opened in self._clients:
            await opened.aclose()
        if self._closing:
            _, waiting = await asyncio.wait(self._closing, timeout=self._linger_s)
            if waiting:
                log.warning("%s database connections still run statements that missed their deadlines", len(waiting), extra={"connections": len(waiting)})
        for database in self._databases.values():
            database.pool.shutdown(wait=False, cancel_futures=True)
        if self._first is not None:
            await self._first.close()


async def _answers(database: OpenDatabase, *, writes: bool) -> None:
    """Check that the database answers within its deadline, through its read login, and its write login too where a
    route writes it: a wrong setting fails at startup, not at the first request."""

    def connect() -> None:
        for login in (False, True) if writes else (False,):
            with database.port.statements(writes=login):
                pass

    resource = database.resource
    with database.span("open"):
        connecting = database.run(connect)
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
    run one at a time, each on one of the database's threads, under its own deadline. `end` commits it, where the route
    writes and no statement failed, or rolls it back."""

    def __init__(self, database: OpenDatabase, *, writes: bool) -> None:
        self._database = database
        self._writes = writes
        self._turn = asyncio.Lock()
        self._stack: ExitStack | None = None
        self._statements: Statements | None = None
        self._failure: BaseException | None = None
        """What stopped the first statement that did not succeed: its error, its deadline, or a cancellation from
        outside it, such as another resource's deadline. The transaction then never commits."""
        self._running: asyncio.Task[None] | None = None
        """Ends when a statement that did not finish, and still runs on the connection, ends."""
        self._ended = False

    async def fetch[R](self, row: type[R], sql: str, /, **binds: Bind) -> list[R]:
        table = await self._call("fetch", sql, lambda statements, timeout_s: statements.query(sql, binds, timeout_s=timeout_s))
        validator = adapter(row)
        try:
            return [validator.validate_python(found) for found in table.to_pylist()]
        except ValidationError as error:
            error.add_note(f"the rows database {self._database.resource.name} returned are not {getattr(row, '__qualname__', row)}: match the query's columns, in lower case, to its fields")
            raise

    async def fetch_one[R](self, row: type[R], sql: str, /, **binds: Bind) -> R | None:
        rows = await self.fetch(row, sql, **binds)
        if len(rows) > 1:
            msg = f"a query of database {self._database.resource.name} returned {len(rows)} rows, and fetch_one takes one at most: filter it to one, or fetch them all"
            raise LookupError(msg)
        return rows[0] if rows else None

    async def execute(self, sql: str, /, **binds: Bind) -> int:
        if not self._writes:
            msg = f"database {self._database.resource.name}'s transaction only reads: a route writes it by asking for an api.Writer, and is not a GET"
            raise RuntimeError(msg)
        return await self._call("execute", sql, lambda statements, timeout_s: statements.execute(sql, binds, timeout_s=timeout_s))

    async def _call[T](self, operation: str, sql: str, work: Callable[[Statements, float], T]) -> T:
        resource = self._database.resource
        with self._database.span(operation, **{"db.query.text": sql}) as span:
            async with self._turn:
                self._check_usable()
                deadline_at = time.monotonic() + resource.deadline_s
                running = self._database.run(lambda: self._run(work, deadline_at))
                deadline = asyncio.timeout(resource.deadline_s)
                try:
                    async with deadline:
                        result = await asyncio.shield(running)
                except BaseException as error:
                    raised: BaseException = error
                    if isinstance(error, TimeoutError) or (isinstance(error, Exception) and time.monotonic() >= deadline_at):  # its deadline, or the driver's timeout at it
                        raised = DeadlineExceededError(resource.name, resource.deadline_s)
                    self._fail(raised, running)
                    if raised is error:
                        raise
                    raise raised from error
            if isinstance(result, pa.Table):
                span.set_attribute("db.response.returned_rows", result.num_rows)
            return result

    def _run[T](self, work: Callable[[Statements, float], T], deadline_at: float) -> T:
        """On one of the database's threads: open the connection on the first statement, and run `work` with what is
        left of the deadline, which the driver's timeout enforces. A statement whose deadline passed while it waited for
        the thread, or while it connected, never starts."""
        name = self._database.resource.name
        if deadline_at - time.monotonic() <= 0:
            msg = f"the deadline passed while the statement waited for one of database {name}'s threads"
            raise TimeoutError(msg)
        statements = self._statements
        if statements is None:
            stack = ExitStack()
            statements = stack.enter_context(self._database.port.statements(writes=self._writes))
            self._stack, self._statements = stack, statements
        if (left_s := deadline_at - time.monotonic()) <= 0:
            msg = f"the deadline passed while database {name} connected"
            raise TimeoutError(msg)
        return work(statements, left_s)

    def _fail[T](self, raised: BaseException, running: asyncio.Future[T]) -> None:
        if self._failure is None:
            self._failure = raised
        if not running.done():
            task = asyncio.get_running_loop().create_task(_ended(running))
            self._running = task
            task.add_done_callback(lambda _: self._settle(task))

    def _settle(self, task: asyncio.Task[None]) -> None:
        """The statement that did not finish has ended, so the connection takes statements again."""
        if self._running is task:
            self._running = None

    def _check_usable(self) -> None:
        name = self._database.resource.name
        if self._ended:
            msg = f"the request's transaction in database {name} has ended: a resource serves the request that was given it, and only while it runs"
            raise RuntimeError(msg)
        if self._running is not None:
            msg = f"database {name} is still running a statement that did not finish, on the request's one connection: wait for nothing more from it in this request"
            raise RuntimeError(msg)

    async def end(self, *, commit: bool) -> None:
        async with self._turn:
            if self._ended:
                return
            self._ended = True
            if self._running is not None:
                running = self._running
                self._database.outlive(self._close_when_done(running))
            elif self._stack is not None:
                if commit and self._writes and self._failure is None:
                    await self._commit(self._stack)
                else:
                    await self._roll_back(self._stack)
            if commit and self._writes and self._failure is not None:
                raise self._refused(self._failure)

    def _refused(self, failure: BaseException) -> BaseException:
        """What a request answers whose writes were rolled back because `failure` stopped one of its statements."""
        resource = self._database.resource
        if isinstance(failure, DeadlineExceededError | asyncio.CancelledError):
            detail = f"a statement in {resource.name} did not finish before a deadline, so the request's writes there are rolled back, never committed"
            return DeadlineExceededError(resource.name, resource.deadline_s, detail=detail)
        msg = f"a statement in {resource.name} failed and the handler carried on, so the request's writes there are rolled back, never committed: let the statement's error end the request"
        refused = RuntimeError(msg)
        refused.__cause__ = failure
        return refused

    async def _commit(self, stack: ExitStack) -> None:
        resource, statements = self._database.resource, self._statements
        if statements is None:
            return
        with self._database.span("commit"):
            committing = self._database.run(lambda: _commit(statements, stack))
            deadline = asyncio.timeout(resource.deadline_s)
            try:
                async with deadline:
                    await asyncio.shield(committing)
            except TimeoutError as error:
                if not deadline.expired():
                    raise
                self._database.outlive(_fate(committing, resource.name, "commit"))
                detail = f"{resource.name}'s commit did not finish within its deadline of {resource.deadline_s:g}s: whether the request's writes there committed, the log says when it ends"
                raise DeadlineExceededError(resource.name, resource.deadline_s, detail=detail) from error
            except asyncio.CancelledError:
                self._database.outlive(_fate(committing, resource.name, "commit"))
                raise

    async def _roll_back(self, stack: ExitStack) -> None:
        """End the transaction and give back the connection. It never raises but when cancelled: the request already
        has its answer, and a connection that cannot roll back is closed, which ends its transaction too."""
        resource = self._database.resource
        with self._database.span("rollback") as span:
            rolling = self._database.run(stack.close)
            deadline = asyncio.timeout(resource.deadline_s)
            try:
                async with deadline:
                    await asyncio.shield(rolling)
            except asyncio.CancelledError:
                self._database.outlive(_fate(rolling, resource.name, "rollback"))
                raise
            except TimeoutError as error:
                if not deadline.expired():
                    _could_not_roll_back(resource.name, span, error)
                    return
                span.record_exception(error)
                log.warning("%s's rollback did not finish within its deadline of %ss: it goes on, and the log says when it ends", resource.name, resource.deadline_s, extra={"resource": resource.name})
                self._database.outlive(_fate(rolling, resource.name, "rollback"))
            except Exception as error:  # noqa: BLE001 -- logged; the connection is closed, which ends the transaction
                _could_not_roll_back(resource.name, span, error)

    def abandon(self) -> None:
        if self._ended:
            return
        self._ended = True
        self._database.outlive(self._close_when_done(self._running))

    async def _close_when_done(self, running: asyncio.Task[None] | None) -> None:
        """Roll back and give back the connection once the statement still running on it ends."""
        if running is not None:
            await running
        if (stack := self._stack) is not None:
            await self._roll_back(stack)


def _could_not_roll_back(name: str, span: Span, error: BaseException) -> None:
    span.record_exception(error)
    log.warning("could not roll back database %s's transaction; its connection is closed, which ends it", name, exc_info=error, extra={"resource": name})


async def _ended[T](running: asyncio.Future[T]) -> None:
    """Wait for a statement that did not finish to end, however it ends: its outcome no longer matters."""
    await asyncio.wait([running])
    if not running.cancelled():
        running.exception()  # retrieved, so the loop logs no warning of an error nobody read


async def _fate(finishing: asyncio.Future[None], name: str, what: Literal["commit", "rollback"]) -> None:
    """Log how a commit or a rollback that missed its deadline ended, once it has."""
    await asyncio.wait([finishing])
    error = None if finishing.cancelled() else finishing.exception()
    extra = {"resource": name, "ended": what}
    if what == "commit" and error is None:
        log.warning("%s's commit finished after its deadline, and committed the request's writes", name, extra=extra | {"committed": True})
    elif what == "commit":
        log.error("%s's commit failed after its deadline, and committed nothing", name, exc_info=error, extra=extra | {"committed": False})
    elif error is None:
        log.info("%s's rollback finished after its deadline", name, extra=extra)
    else:
        log.warning("%s's rollback failed after its deadline; its connection is closed, which ends it", name, exc_info=error, extra=extra)


def _commit(statements: Statements, stack: ExitStack) -> None:
    with stack:
        statements.commit()


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
                return adapter(reply).validate_json(response.content)
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


_ADAPTERS: dict[object, object] = {}
"""Each type's pydantic adapter, made the first time a row or a reply is validated as it: building one costs a few
microseconds, which a route would otherwise pay on every call. A program validates a fixed set of types."""


def adapter[T](kind: type[T]) -> TypeAdapter[T]:
    if (made := _ADAPTERS.get(kind)) is None:
        made = TypeAdapter(kind)
        _ADAPTERS[kind] = made
    return made  # ty: ignore[invalid-return-type] -- each type's entry is the adapter made for it, as above
