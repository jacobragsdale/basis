"""The resources an app opens when it starts, through basis-core's databases, an httpx2 client, the process's memory
and the system's clock, or through the fakes a test gives; and what every call through one gets, real or fake: a span
of its own on the app's tracer, and, for every call that can wait, a deadline.

A deadline is the declared `deadline_s`, from when the call is made, or what is left of the route's deadline where that
is shorter (`deadlines.py`): a call that has not finished by then raises `DeadlineExceededError`, a 504 problem. The
database drivers block, so a database's statements run on a thread pool of the database's own, `max_concurrent`
threads, one statement at a time on the request's one connection; waiting for a thread counts against the statement's
deadline, and the driver's own timeout, set to what is left of it, stops the statement on the server too. The commit that ends the request's transaction has the same deadline, and the route's caps
it too; a rollback has the database's alone, so that the connection always goes back.

A statement that fails, misses its deadline or is cancelled leaves the transaction unfit to commit: it rolls back,
once the statement has ended if it still runs, and a request that would have committed answers an error instead of a
2xx, even when the handler caught the statement's error and carried on. A commit that misses its deadline is a 504,
and whether it committed in the end is logged when it ends.

A call a handler sends (`Client.send`) in a route that writes is a row of the written database's outbox, in the
request's transaction (`basis.api.outboxes`): once the transaction commits, the request makes each such call, within what
is left of the route's deadline, and records how it went in a transaction of its own. Neither the call nor the record
changes what the request answers. In a route that writes nothing, `send` makes the call at once.

`plan` checks, when the app is built, that the settings configure every resource that has no fake, and the process pool
where a compute function has none, so that a missing setting stops the app before it serves; `Resources.open`, in the
app's lifespan, opens them, after the keys its users' tokens verify with, checking that each database answers, through
each login the routes use, within its deadline, and starts the pool's processes."""

import asyncio
import contextvars
import logging
import ssl
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Collection, Coroutine, Generator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, ExitStack, contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal, Protocol, assert_never
from uuid import UUID, uuid7

import httpx2
import pyarrow as pa
from opentelemetry.trace import Span, SpanKind, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic_core import to_json

from basis.api import deadlines, fakes, outboxes
from basis.api.compute import ComputeFunction
from basis.api.pool import Call, InProcess, Pool, Runs
from basis.api.resources import (
    Bind,
    Body,
    CacheResource,
    CallFailedError,
    ClockResource,
    DatabaseResource,
    DeadlineExceededError,
    HttpMethod,
    HttpResource,
    IdsResource,
    QueryValue,
    Resource,
    SendMethod,
)
from basis.api.service import Service, ServiceError
from basis.api.settings import HttpService, Settings
from basis.core import logs
from basis.core.databases import Statements, open_database
from basis.core.destination import Dialect
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
    """How an app opens its resources: each name's fake, where a test gave one, and otherwise its settings; and the
    compute functions its routes ask for, each run in the pool unless a test fakes it."""

    service: Service
    settings: Settings
    fakes: Mapping[str, fakes.Fake]
    compute: tuple[ComputeFunction[..., object], ...]
    in_process: frozenset[ComputeFunction[..., object]]
    """The compute functions a test fakes, which run in this process."""

    @property
    def pooled(self) -> tuple[ComputeFunction[..., object], ...]:
        return tuple(declared for declared in self.compute if declared not in self.in_process)


def plan(service: Service, settings: Settings, given: fakes.Fakes) -> Plan:
    """The resources' plan, after checking that each fake fakes a resource of the service's, of its kind, or a compute
    function its routes ask for, and that the settings configure every resource that has none: a database, and its
    write login where a route writes it, another service's URL, and the pool for compute functions."""
    asked: dict[ComputeFunction[..., object], None] = {}
    for route in service.routes:
        for ask in route.asks:
            if isinstance(ask.given, ComputeFunction):
                asked[ask.given] = None
    compute = tuple(asked)
    in_process: set[ComputeFunction[..., object]] = set()
    for resource, fake in given.items():
        if isinstance(resource, ComputeFunction):
            if resource not in compute:
                msg = f"a fake is given for the compute function {resource.name}, which no route of service {service.name} asks for: fake the ones its routes run"
                raise ServiceError(msg)
            if not isinstance(fake, fakes.Compute):
                msg = f"the compute function {resource.name} is given a fakes.{type(fake).__name__}: give it fakes.Compute()"
                raise ServiceError(msg)
            in_process.add(resource)
    outside = f"which service {service.name} does not declare: fake the service's own resources"
    by_name = faked({resource: fake for resource, fake in given.items() if not isinstance(resource, ComputeFunction)}, service.resources, outside)
    written = {route.writes.name for route in service.routes if route.writes is not None}
    unset = [found for resource in service.resources if resource.name not in by_name and (found := unconfigured(resource, settings, written=resource.name in written)) is not None]
    if settings.compute is None and (pooled := [declared.name for declared in compute if declared not in in_process]):
        unset.append(f"{variable(('compute', 'workers'))}, the processes the compute functions {', '.join(pooled)} run in")
    if unset:
        msg = f"service {service.name}'s resources are not all configured, so the API cannot start: set {'; '.join(unset)}"
        raise SettingsError(msg)
    return Plan(service, settings, by_name, compute, frozenset(in_process))


def unconfigured(resource: Resource, settings: Settings, *, written: bool) -> str | None:
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


def faked(given: fakes.Fakes, reached: Collection[Resource], outside: str) -> dict[str, fakes.Fake]:
    """Each fake `given`, by its resource's name, after checking that it fakes one of the resources `reached`, of its
    kind; `outside` says why a fake of any other is refused, and the fix."""
    by_name: dict[str, fakes.Fake] = {}
    for resource, fake in given.items():
        if isinstance(resource, ComputeFunction) or resource not in reached:
            msg = f"a fake is given for {resource.name}, {outside}"
            raise ServiceError(msg)
        if not fits(resource, fake):
            msg = f"{resource.name} is given a fakes.{type(fake).__name__}, which does not fake its kind of resource: give it the fake of its kind"
            raise ServiceError(msg)
        by_name[resource.name] = fake
    return by_name


def fits(resource: Resource, fake: fakes.Fake) -> bool:
    """Whether `fake` fakes `resource`'s kind of resource."""
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
    dialect: Dialect | None
    """The SQL its outbox's statements are written in, or None for a fake, whose outbox is in memory."""
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

    def table(self, statements: Statements) -> outboxes.Table:
        """The database's outbox, as the transaction `statements` belongs to reaches it."""
        declared, name = self.resource.outbox, self.resource.name
        if declared is None:
            msg = f"database {name} declares no outbox"
            raise RuntimeError(msg)
        if isinstance(statements, fakes.Transaction):
            return statements.outbox()
        if self.dialect is None:
            msg = f"database {name} is faked by statements that are not a fakes.Transaction, which keep no outbox: fake it with fakes.Database"
            raise RuntimeError(msg)
        return outboxes.Sql(statements, declared.table, self.dialect)


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
        self._pool: Pool | None = None
        self._calls: dict[ComputeFunction[..., object], Call] = {}

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
            if (pooled := planned.pooled) and planned.settings.compute is not None:
                opened._pool = await Pool.open(planned.settings.compute.workers, pooled)
        except BaseException:
            await opened.close()
            raise
        in_process = InProcess()
        for declared in planned.compute:
            runs: Runs = in_process if declared in planned.in_process or opened._pool is None else opened._pool
            opened._calls[declared] = Call(declared, runs, tracer)
        return opened

    async def _open(self, resource: Resource, planned: Plan, *, writes: bool) -> None:
        fake = planned.fakes.get(resource.name)
        tracer = self._tracer
        match resource:
            case DatabaseResource():
                database = open_one(resource, planned.settings, fake, tracer, self._closing)
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
        """The request's transaction in `resource`, which, where the route writes it, the calls its handler sends go
        through until it ends."""
        session = DatabaseSession(self._databases[resource.name], writes=writes)
        if writes:
            session.take_sends()
        return session

    def handle(self, resource: HttpResource | CacheResource[object] | ClockResource | IdsResource) -> object:
        return self._handles[resource.name]

    def compute(self, declared: ComputeFunction[..., object]) -> Call:
        return self._calls[declared]

    async def close(self) -> None:
        """Stop the compute pool, close the HTTP clients, wait for the work that outlived its request, and then stop each
        database's threads."""
        if self._pool is not None:
            await self._pool.close()
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


def open_one(resource: DatabaseResource, settings: Settings, fake: fakes.Fake | None, tracer: Tracer, closing: set[asyncio.Task[None]]) -> OpenDatabase:
    """`resource`, opened as `fake`, or as the settings configure it: connecting to nothing yet."""
    if isinstance(fake, fakes.Database):
        return OpenDatabase(resource, fake, "fake", tracer, closing, None)
    kind = settings.database(resource.name).kind
    return OpenDatabase(resource, open_database(settings, resource.name), _SYSTEMS[kind], tracer, closing, kind)


_WRITING: ContextVar[tuple[deadlines.Deadline, DatabaseSession] | None] = ContextVar("basis.api.opened.writing", default=None)
"""The transaction of the database the route serving the request writes, which `Client.send` writes its calls in, with
that route's deadline: a route that writes none, as another app's that the request reaches in this process, finds a
deadline of its own, not this one, and sends its calls itself. None where no route writes."""


async def _answers(database: OpenDatabase, *, writes: bool) -> None:
    """Check that the database answers within its deadline, through its read login, and its write login too where a
    route writes it, which reaches its outbox's table where it declares one: a wrong setting, or a table not yet
    created, fails at startup, not at the first request."""

    def connect() -> None:
        for login in (False, True) if writes else (False,):
            with database.port.statements(writes=login) as statements:
                if login and (declared := database.resource.outbox) is not None:
                    try:
                        database.table(statements).probe(timeout_s=database.resource.deadline_s)
                    except Exception as error:
                        msg = (
                            f"database {database.resource.name}'s outbox {declared.table} cannot be read through its write login ({type(error).__name__}: {' '.join(str(error).split())}):"
                            " create it with `basis ddl SERVICE --dialect DIALECT`, and let that login select, insert and update its rows"
                        )
                        raise ServiceError(msg) from error

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
        self._sent: list[tuple[outboxes.Row, HttpClient]] = []
        """The calls the handler sent, each a row of the outbox in this transaction, with the client to make it with."""
        self._sending: Token[tuple[deadlines.Deadline, DatabaseSession] | None] | None = None
        """Set while the calls the route's handler sends go through this transaction."""

    def take_sends(self) -> None:
        """Take the calls the handler of the route serving the request sends, until the transaction ends."""
        if (serving := deadlines.serving()) is not None:
            self._sending = _WRITING.set((serving, self))

    def _stop_taking_sends(self) -> None:
        if (token := self._sending) is None:
            return
        self._sending = None
        try:
            _WRITING.reset(token)
        except ValueError:  # ended from another context, as a dependency closed without being finished is: its own copy ends with it
            log.debug("the transaction ended outside the context that took its sends", extra={"resource": self._database.resource.name})

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

    async def enqueue(self, client: HttpClient, method: SendMethod, path: str, body: str | None, key: str) -> None:
        """Write the call as a row of the database's outbox, in this transaction, to make once it commits: due to
        `basis relay` when the route's deadline has passed, so that it never makes a call its request still may."""
        database = self._database
        resource, serving = database.resource, deadlines.serving()
        if serving is None:
            msg = f"a call to {client.name} is sent through database {resource.name}'s outbox only while a route serves a request"
            raise RuntimeError(msg)
        declared = resource.outbox
        if declared is None:
            msg = (
                f"{serving.route} sends {method} {path} to {client.name}, and writes {resource.name}, which declares no outbox for the call to wait in until its commit:"
                f" declare one, as api.database({resource.name!r}, ..., outbox=api.outbox(TABLE, ...)), and create its table with `basis ddl`"
            )
            raise RuntimeError(msg)
        now = _utc_now()
        expires_at = now + timedelta(seconds=declared.max_age_s)
        due_at = now + timedelta(seconds=max(serving.at - time.monotonic(), 0.0))  # the route's deadline, which `max_age_s` outlasts
        request_id = logs.bound().get("request_id")
        row = outboxes.Row(
            id=str(uuid7()), service=client.name, method=method, path=path, body=body, idempotency_key=key, route=serving.route[: outboxes.ROUTE_LENGTH],
            request_id=request_id if isinstance(request_id, str) else None, created_at=now, expires_at=expires_at, next_at=due_at, attempts=0,
            status="pending", last_error=None, ended_at=None,
        )  # fmt: skip
        await self._call("outbox", f"INSERT INTO {declared.table}", lambda statements, timeout_s: database.table(statements).add(row, timeout_s=timeout_s))
        self._sent.append((row, client))

    async def _call[T](self, operation: str, sql: str, work: Callable[[Statements, float], T]) -> T:
        resource = self._database.resource
        with self._database.span(operation, **{"db.query.text": sql}) as span:
            async with self._turn:
                self._check_usable()
                limit = deadlines.limit(resource.name, resource.deadline_s)
                deadline_at = time.monotonic() + limit.seconds
                running = self._database.run(lambda: self._run(work, deadline_at))
                deadline = asyncio.timeout(limit.seconds)
                try:
                    async with deadline:
                        result = await asyncio.shield(running)
                except BaseException as error:
                    raised: BaseException = error
                    if isinstance(error, TimeoutError) or (isinstance(error, Exception) and time.monotonic() >= deadline_at):  # its deadline, or the driver's timeout at it
                        raised = limit.exceeded()
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
        self._stop_taking_sends()
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
                    await self._send_committed()
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
        limit = deadlines.limit(resource.name, resource.deadline_s)
        if limit.route is not None and limit.seconds <= 0:  # the route's deadline has passed, so its writes never commit
            await self._roll_back(stack)
            detail = f"route {limit.route.route}'s deadline of {limit.route.seconds:g}s passed before {resource.name}'s commit, so the request's writes there are rolled back, never committed"
            raise DeadlineExceededError(resource.name, limit.route.seconds, detail=detail)
        with self._database.span("commit"):
            committing = self._database.run(lambda: _commit(statements, stack))
            deadline = asyncio.timeout(limit.seconds)
            try:
                async with deadline:
                    await asyncio.shield(committing)
            except TimeoutError as error:
                if not deadline.expired():
                    raise
                self._database.outlive(_fate(committing, resource.name, "commit"))
                whose = f"what was left of route {limit.route.route}'s deadline of {limit.route.seconds:g}s" if limit.route is not None else f"its deadline of {resource.deadline_s:g}s"
                detail = f"{resource.name}'s commit did not finish within {whose}: whether the request's writes there committed, the log says when it ends"
                raise DeadlineExceededError(resource.name, limit.route.seconds if limit.route is not None else resource.deadline_s, detail=detail) from error
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

    async def _send_committed(self) -> None:
        """Make each call the handler sent, now that its row has committed, and record how each went. Whatever happens
        here, the request answers as it would have: its writes have committed, and `basis relay` makes again each call
        whose row is not recorded done."""
        if not self._sent:
            return
        async with asyncio.TaskGroup() as calls:  # each within what is left of the route's deadline, so a slow one leaves the others theirs
            made = [calls.create_task(_made(row, client)) for row, client in self._sent]
        await self._record([(row, call.result()) for (row, _), call in zip(self._sent, made, strict=True)])

    async def _record(self, outcomes: Sequence[tuple[outboxes.Row, outboxes.Failure | None]]) -> None:
        """Record each call the request made after its commit, `done` or one failed attempt, in a transaction of its
        own, within what is left of the route's deadline. A record that does not happen leaves the call to the relay."""
        database = self._database
        resource, declared = database.resource, database.resource.outbox
        limit = deadlines.limit(resource.name, resource.deadline_s)
        if declared is None or limit.seconds <= 0:
            log.info("no time was left to record the calls the request made after its commit: basis relay makes them again", extra={"resource": resource.name, "calls": len(outcomes)})
            return
        at, deadline_at = _utc_now(), time.monotonic() + limit.seconds

        def record() -> None:
            with database.port.statements(writes=True) as statements:
                table = database.table(statements)
                for row, failure in outcomes:  # each only while no relay has begun an attempt since the row's insert
                    left_s = _left(deadline_at, f"the calls' record in {resource.name}")
                    outboxes.record(table, declared, row, failure, seen=0, attempts=1, at=at, reported=False, timeout_s=left_s)
                statements.commit()

        with database.span("outbox record", **{"basis.outbox.calls": len(outcomes)}) as span:
            recording = database.run(record)
            try:
                async with asyncio.timeout(limit.seconds):
                    await asyncio.shield(recording)
            except asyncio.CancelledError:
                database.outlive(_ended(recording))
                raise
            except Exception as error:  # logged: the calls' rows stay pending, and the relay makes them again
                if not recording.done():
                    database.outlive(_ended(recording))
                span.record_exception(error)
                log.warning("could not record the calls the request made after its commit: basis relay makes them again", exc_info=error, extra={"resource": resource.name, "calls": len(outcomes)})

    def abandon(self) -> None:
        self._stop_taking_sends()
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


async def _made(row: outboxes.Row, client: HttpClient) -> outboxes.Failure | None:
    """Make the call `row` holds, after its commit, once: None when the service accepted it, and otherwise why not. It
    never raises but when cancelled, since the request's writes have committed."""
    try:
        failure = await client.deliver(row)
    except Exception as error:  # a defect, logged with its traceback: the writes have committed, so the request still succeeds
        log.exception("making %s after its commit failed with a defect: basis relay makes it again", row.named(), extra=_fields(row))
        return outboxes.Failure(f"{type(error).__name__}: {error}", None)
    if failure is not None:
        then = "it is made no more" if failure.permanent else "basis relay makes it again"
        log.warning("%s did not accept %s after its commit: %s", row.service, row.named(), then, extra=_fields(row) | {"error": failure.error, "reply": failure.reply})
    return failure


def _left(deadline_at: float, what: str) -> float:
    """The seconds left before `deadline_at`, on a database's thread, for the next statement of `what`."""
    if (left_s := deadline_at - time.monotonic()) <= 0:
        msg = f"the deadline passed before {what} finished"
        raise TimeoutError(msg)
    return left_s


def _fields(row: outboxes.Row) -> dict[str, object]:
    """A call's log fields."""
    return {"outbox_row": row.id, "service": row.service, "method": row.method, "path": row.path, "idempotency_key": row.idempotency_key, "attempts": row.attempts}


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

    @property
    def name(self) -> str:
        return self._resource.name

    @property
    def deadline_s(self) -> float:
        return self._resource.deadline_s

    async def get[T](self, path: str, reply: type[T], /, *, query: Mapping[str, QueryValue | Sequence[QueryValue]] | None = None) -> T:
        return await self.call("GET", path, reply, query=query)

    async def post[T](self, path: str, reply: type[T], /, *, query: Mapping[str, QueryValue | Sequence[QueryValue]] | None = None, body: Body = None) -> T:
        return await self.call("POST", path, reply, query=query, body=body)

    async def send(self, method: SendMethod, path: str, body: Body = None, /, *, key: str) -> None:
        outboxes.checked(path, key)
        content = _content(body)
        writing, serving = _WRITING.get(), deadlines.serving()
        if writing is not None and serving is not None and writing[0] is serving:  # the route serving this request writes
            await writing[1].enqueue(self, method, path, content.decode() if content is not None else None, key)
            return
        with self._span(method, path, key) as span:
            await self._exchange(span, method, path, content=content, key=key)

    async def deliver(self, row: outboxes.Row) -> outboxes.Failure | None:
        """Make the call an outbox's row holds, once: None when the service accepts it, and otherwise why it did not,
        with the status it answered."""
        with self._span(row.method, row.path, row.idempotency_key) as span:
            span.set_attribute("basis.outbox.row", row.id)
            try:
                await self._exchange(span, row.method, row.path, content=row.body.encode() if row.body is not None else None, key=row.idempotency_key)
            except (CallFailedError, DeadlineExceededError) as failed:
                span.record_exception(failed)
                span.set_status(StatusCode.ERROR, str(failed))
                return outboxes.Failure(str(failed), failed.reply if isinstance(failed, CallFailedError) else None)
        return None

    async def call[T](self, method: HttpMethod, path: str, reply: type[T], /, *, query: Mapping[str, QueryValue | Sequence[QueryValue]] | None = None, body: Body = None) -> T:
        resource = self._resource
        with self._span(method, path, None) as span:
            response = await self._exchange(span, method, path, query=query, content=_content(body))
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

    @contextmanager
    def _span(self, method: HttpMethod, path: str, key: str | None) -> Generator[Span]:
        resource = self._resource
        attributes = {"http.request.method": method, "url.path": path, "basis.resource": resource.name, "basis.deadline_s": resource.deadline_s}
        if key is not None:
            attributes["basis.idempotency_key"] = key
        with self._tracer.start_as_current_span(f"{resource.name} {method}", kind=SpanKind.CLIENT, attributes=attributes) as span:
            yield span

    async def _exchange(  # noqa: PLR0913 -- the request's parts, by keyword
        self, span: Span, method: HttpMethod, path: str, *, query: Mapping[str, QueryValue | Sequence[QueryValue]] | None = None, content: bytes | None = None, key: str | None = None
    ) -> httpx2.Response:
        """One request to the service, within its deadline, and its reply, which must be a 2xx."""
        resource = self._resource
        headers: dict[str, str] = {}
        _PROPAGATOR.inject(headers)  # this call's span, as the other service's parent
        if content is not None:
            headers["content-type"] = "application/json"
        if key is not None:
            headers["idempotency-key"] = key
        limit = deadlines.limit(resource.name, resource.deadline_s)
        if limit.seconds <= 0:
            raise limit.exceeded()
        deadline = asyncio.timeout(limit.seconds)
        try:
            async with deadline:
                response = await self._client.request(method, path, params=_query(query), content=content, headers=headers, timeout=limit.seconds)
        except TimeoutError as error:
            if deadline.expired():
                raise limit.exceeded() from error
            raise
        except httpx2.TimeoutException as error:  # one of the client's own timeouts, each the deadline
            raise limit.exceeded() from error
        except httpx2.RequestError as error:  # a transport's, or one such as a redirect loop or a body that does not decode
            msg = f"{resource.name} could not be reached for {method} {path}: {type(error).__name__}"
            raise CallFailedError(msg, resource=resource.name, reply=None) from error
        span.set_attribute("http.response.status_code", response.status_code)
        if not response.is_success:
            log.warning("%s answered %s to %s %s", resource.name, response.status_code, method, path, extra={"resource": resource.name, "reply": response.status_code, "body": response.text[:2000]})
            msg = f"{resource.name} answered {response.status_code} to {method} {path}"
            raise CallFailedError(msg, resource=resource.name, reply=response.status_code)
        return response


def _content(body: Body) -> bytes | None:
    """A call's body as JSON, or None for a call without one."""
    if body is None:
        return None
    return body.model_dump_json().encode() if isinstance(body, BaseModel) else to_json(body)


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
            limit = deadlines.limit(resource.name, resource.deadline_s)
            if limit.seconds <= 0:
                raise limit.exceeded()
            deadline = asyncio.timeout(limit.seconds)
            try:
                async with deadline:
                    value, hit = await self._get(key, load)
            except TimeoutError as error:
                if deadline.expired():
                    raise limit.exceeded() from error
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
