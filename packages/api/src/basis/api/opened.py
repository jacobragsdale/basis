"""The resources an app opens when it starts, through basis-core's databases, basis-core's own resources (another
service's client, the system's clock and new ids, `basis.core.opened`) and the process's memory, or through the fakes a
test gives; and what every call through one gets, real or fake: a span of its own on the app's tracer, and, for every
call that can wait, a deadline.

A deadline is the declared `deadline_s`, from when the call is made, or what is left of the route's deadline where that
is shorter (`deadlines.py`): a call that has not finished by then raises `Timeout`, a 504 problem. Every other failure
of a resource's is the kind of `ServiceError` basis-core makes of it, real or fake, which a route answers as its problem
(`errors.problem`) unless its handler handles it. The database drivers block, so a database's statements run on a thread pool of the database's own, `max_concurrent`
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
import logging
import time
from typing import Protocol, assert_never
from uuid import uuid7

import httpx2
from opentelemetry.trace import Tracer

from basis.api import fakes
from basis.api.caches import MemoryCache
from basis.api.compute import ComputeFunction
from basis.api.planning import Plan
from basis.api.pool import Call, InProcess, Pool, Runs
from basis.api.resources import CacheResource, ClockResource, DatabaseResource, HttpResource, IdsResource, Resource
from basis.api.sessions import DatabaseSession, HttpClient, OpenDatabase, _answers
from basis.api.settings import Settings
from basis.core import opened
from basis.core.databases import open_database
from basis.core.databases.base import SYSTEMS
from basis.core.opened import SYSTEM_WAITS, NewIds, SystemClock, client, utc_now

log = logging.getLogger(__name__)


class Opens(Protocol):
    """What the app opens before its resources and closes after them: the keys its users' tokens verify with."""

    async def open(self, tracer: Tracer) -> None: ...

    async def close(self) -> None: ...


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
                reaching = fake.client(resource.name) if isinstance(fake, fakes.Http) else client(resource.name, planned.settings.http[resource.name])
                self._clients.append(reaching)
                waits = fake.waits if isinstance(fake, fakes.Http) else SYSTEM_WAITS
                self._handles[resource.name] = HttpClient(opened.HttpClient(resource, reaching, tracer, waits=waits))
            case CacheResource():
                monotonic = fake.monotonic if isinstance(fake, fakes.Cache) else time.monotonic
                self._handles[resource.name] = MemoryCache(resource, monotonic, tracer)
            case ClockResource():
                self._handles[resource.name] = SystemClock(resource, fake.now if isinstance(fake, fakes.Clock) else utc_now, tracer)
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
        for reaching in self._clients:
            await reaching.aclose()
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
        fake.opened_as(resource.name)
        return OpenDatabase(resource, fake, "fake", tracer, closing, None)
    kind = settings.database(resource.name).kind
    return OpenDatabase(resource, open_database(settings, resource.name), SYSTEMS[kind], tracer, closing, kind)
