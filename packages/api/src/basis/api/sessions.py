"""A request's side of a database, as `basis.api.opened` describes what every call gets: the database as the app
opened it (`OpenDatabase`), its own thread pool and spans; one request's transaction in it (`DatabaseSession`), each
statement under its deadline, which commits only after the handler returns and the response validates; the calls a
handler sends in a route that writes, rows of the database's outbox in that transaction, made once it commits
(`basis.api.outboxes`); and another service's client (`HttpClient`), whose `send` writes them."""

import asyncio
import contextvars
import logging
import time
from collections.abc import Callable, Coroutine, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, ExitStack
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal, Protocol
from uuid import uuid7

import pyarrow as pa
from opentelemetry.trace import Span, SpanKind, Tracer
from pydantic import BaseModel, ValidationError

from basis.api import arrow, dataset, deadlines, fakes, idempotent, outboxes
from basis.api.errors import OutcomeUnknownError
from basis.api.resources import Bind, Body, DatabaseResource, HttpMethod, Query, Segment, SendMethod, filled
from basis.api.service import DeclarationError
from basis.core import logs, opened
from basis.core.databases import Statements
from basis.core.destination import Dialect
from basis.core.errors import ServiceError, Timeout
from basis.core.opened import adapter, content, utc_now

log = logging.getLogger(__name__)


class DatabasePort(Protocol):
    """What a database resource reaches: basis-core's database, or a fake."""

    def statements(self, *, writes: bool) -> AbstractContextManager[Statements]: ...


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

    def keys(self, statements: Statements) -> idempotent.Table:
        """The database's table of `Idempotency-Key`s, as the transaction `statements` belongs to reaches it."""
        declared, name = self.resource.idempotency, self.resource.name
        if declared is None:
            msg = f"database {name} declares no idempotency table"
            raise RuntimeError(msg)
        if isinstance(statements, fakes.Transaction):
            return statements.idempotency()
        if self.dialect is None:
            msg = f"database {name} is faked by statements that are not a fakes.Transaction, which keep no keys: fake it with fakes.Database"
            raise RuntimeError(msg)
        return idempotent.Sql(statements, declared.table, self.dialect)


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
                        raise DeclarationError(msg) from error

    resource = database.resource
    with database.span("open"):
        connecting = database.run(connect)
        deadline = asyncio.timeout(resource.deadline_s)
        try:
            async with deadline:
                await asyncio.shield(connecting)
        except TimeoutError as error:
            if deadline.expired():
                msg = f"{resource.name} did not answer within its deadline of {resource.deadline_s:g}s"
                raise Timeout(msg, resource=resource.name) from error
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

    async def fetch_table(self, row: type[BaseModel], sql: str, /, **binds: Bind) -> pa.Table:
        table = await self._call("fetch", sql, lambda statements, timeout_s: statements.query(sql, binds, timeout_s=timeout_s))
        found = dataset.rows(list[row], f"fetch_table of {row.__qualname__}", table=True)  # ty: ignore[invalid-type-form] -- the row model, a value
        return await asyncio.to_thread(arrow.conform, found, table, f"the rows database {self._database.resource.name} returned")

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
                f"{serving.name} sends {method} {path} to {client.name}, and writes {resource.name}, which declares no outbox for the call to wait in until its commit:"
                f" declare one, as api.database({resource.name!r}, ..., outbox=api.outbox(TABLE, ...)), and create its table with `basis ddl`"
            )
            raise RuntimeError(msg)
        now = utc_now()
        expires_at = now + timedelta(seconds=declared.max_age_s)
        due_at = now + timedelta(seconds=max(serving.at - time.monotonic(), 0.0))  # the route's deadline, which `max_age_s` outlasts
        request_id = logs.bound().get("request_id")
        row = outboxes.Row(
            id=str(uuid7()), service=client.name, method=method, path=path, body=body, idempotency_key=key, route=serving.name[: outboxes.ROUTE_LENGTH],
            request_id=request_id if isinstance(request_id, str) else None, created_at=now, expires_at=expires_at, next_at=due_at, attempts=0,
            status="pending", last_error=None, ended_at=None,
        )  # fmt: skip
        await self._call("outbox", f"INSERT INTO {declared.table}", lambda statements, timeout_s: database.table(statements).add(row, timeout_s=timeout_s))
        self._sent.append((row, client))

    async def claim(self, key_id: str, kept: Callable[[], idempotent.Kept]) -> idempotent.Kept | None:
        """The row of the key `key_id` that a request kept, in this transaction, or None once this request has added
        `kept()`, its own: a row kept longer than the table's `keep_s` is deleted first, as if never kept. Adding a key
        another transaction holds fails as its insert does, a `Conflict`."""
        database = self._database
        declared = database.resource.idempotency
        if declared is None:
            msg = f"database {database.resource.name} declares no idempotency table"
            raise RuntimeError(msg)
        found = await self._call("idempotency", f"find in {declared.table}", lambda statements, timeout_s: database.keys(statements).find(key_id, timeout_s=timeout_s))
        if found is not None and found.created_at <= utc_now() - timedelta(seconds=declared.keep_s):
            await self._call("idempotency", f"forget in {declared.table}", lambda statements, timeout_s: database.keys(statements).forget(key_id, timeout_s=timeout_s))
            found = None
        if found is not None:
            return found
        row = kept()
        await self._call("idempotency", f"add to {declared.table}", lambda statements, timeout_s: database.keys(statements).add(row, timeout_s=timeout_s))
        return None

    async def answered(self, key_id: str, status_code: int, body: str | None) -> None:
        """Write what the request answers into its key's row, which commits with its writes."""
        database = self._database
        table = database.resource.idempotency.table if database.resource.idempotency is not None else "?"
        await self._call("idempotency", f"answer in {table}", lambda statements, timeout_s: database.keys(statements).complete(key_id, status_code, body, timeout_s=timeout_s))

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
                        raised = deadlines.exceeded(limit)
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
        if isinstance(failure, Timeout | asyncio.CancelledError):
            detail = f"a statement in {resource.name} did not finish before a deadline, so the request's writes there are rolled back, never committed"
            return Timeout(detail, resource=resource.name)
        msg = f"a statement in {resource.name} failed and the handler carried on, so the request's writes there are rolled back, never committed: let the statement's error end the request"
        refused = RuntimeError(msg)
        refused.__cause__ = failure
        return refused

    async def _commit(self, stack: ExitStack) -> None:
        resource, statements = self._database.resource, self._statements
        if statements is None:
            return
        limit = deadlines.limit(resource.name, resource.deadline_s)
        if (route := limit.enclosing) is not None and limit.seconds <= 0:  # the route's deadline has passed, so its writes never commit
            await self._roll_back(stack)
            detail = f"{route.whose}'s deadline of {route.seconds:g}s passed before {resource.name}'s commit, so the request's writes there are rolled back, never committed"
            raise Timeout(detail, resource=resource.name)
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
                route = limit.enclosing
                whose = f"what was left of {route.whose}'s deadline of {route.seconds:g}s" if route is not None else f"its deadline of {resource.deadline_s:g}s"
                detail = f"{resource.name}'s commit did not finish within {whose}: whether the request's writes there committed, the log says when it ends"
                raise _unknown(resource.name, detail, error) from error
            except asyncio.CancelledError:
                self._database.outlive(_fate(committing, resource.name, "commit"))
                raise
            except ServiceError as error:  # the commit began, and its acknowledgement never came: it may have committed
                detail = f"{resource.name}'s commit failed, and may have committed the request's writes there or not: do not make the request again until you know which"
                raise _unknown(resource.name, detail, error) from error

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
        at, deadline_at = utc_now(), time.monotonic() + limit.seconds

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


def _unknown(name: str, detail: str, error: BaseException) -> OutcomeUnknownError:
    """A request's answer whose commit in database `name` began and did not end as it should, logged with why."""
    cause = error.__cause__ if error.__cause__ is not None else error
    log.error("%s: the outcome is unknown", detail, extra={"resource": name, "outcome": "unknown", "reason": f"{type(cause).__qualname__}: {cause}"})
    return OutcomeUnknownError(detail)


def _commit(statements: Statements, stack: ExitStack) -> None:
    with stack:
        statements.commit()


class HttpClient:
    """Another service, as `api.Client` gives it: basis-core's client, whose failures, its kinds of `ServiceError`, are
    the problems a route answers, and `send`."""

    def __init__(self, calls: opened.HttpClient) -> None:
        self._calls = calls

    @property
    def name(self) -> str:
        return self._calls.name

    @property
    def deadline_s(self) -> float:
        return self._calls.deadline_s

    async def get[T](self, path: str, reply: type[T], /, *, query: Query | None = None, segments: Mapping[str, Segment] | None = None) -> T:
        return await self.call("GET", path, reply, query=query, segments=segments)

    async def post[T](self, path: str, reply: type[T], /, *, query: Query | None = None, body: Body = None, segments: Mapping[str, Segment] | None = None) -> T:
        return await self.call("POST", path, reply, query=query, body=body, segments=segments)

    async def call[T](self, method: HttpMethod, path: str, reply: type[T], /, *, query: Query | None = None, body: Body = None, segments: Mapping[str, Segment] | None = None) -> T:
        return await self._calls.call(method, filled(path, segments), reply, query=query, body=body)

    async def send(self, method: SendMethod, path: str, body: Body = None, /, *, key: str, segments: Mapping[str, Segment] | None = None) -> None:
        path = filled(path, segments)
        outboxes.checked(path, key)
        sent = content(body)
        writing, serving = _WRITING.get(), deadlines.serving()
        if writing is not None and serving is not None and writing[0] is serving:  # the route serving this request writes
            await writing[1].enqueue(self, method, path, sent.decode() if sent is not None else None, key)
            return
        await self._calls.request(method, path, content=sent, headers={"idempotency-key": key}, attributes={"basis.idempotency_key": key})

    async def deliver(self, row: outboxes.Row) -> outboxes.Failure | None:
        """Make the call an outbox's row holds, once: None when the service accepts it, and otherwise why it did not,
        with the status it answered."""
        key, sent = row.idempotency_key, row.body.encode() if row.body is not None else None
        try:
            await self._calls.request(row.method, row.path, content=sent, headers={"idempotency-key": key}, attributes={"basis.idempotency_key": key, "basis.outbox.row": row.id})
        except ServiceError as failed:  # its span records it
            return outboxes.Failure.of(failed)
        return None
