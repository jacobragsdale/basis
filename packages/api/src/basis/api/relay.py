"""`basis relay [SERVICE]`: the run that makes again the calls a service's outboxes still hold (`basis.api.outboxes`).
The repo's scheduler runs it on a schedule, from the API's image and with the API's settings; it never runs inside
the API's replicas.

One run takes each database whose outbox the service declares, in turn, and each of its rows that is due, the longest
due first, once:

- a call still to make it claims, so that a relay running at the same time leaves it alone; makes, with its
  `Idempotency-Key`, within the other service's deadline; and records, as `outboxes.record` says: `done`, one more
  failed attempt, due again after the outbox's backoff, or `refused` for good by a 4xx;
- a call that has aged out it makes no more, and marks `expired`;
- a call the service refused after its request's commit it reports.

Each statement on the database has a span and the database's deadline: the login, each read of the rows due, each
change of a row, and each commit. A failure stops only its row: a statement that fails or misses its deadline, or a
defect, is logged with its traceback, the row's lease runs out on its own, so that a later run takes it again, the
connection is opened anew, and the run goes on to the next row.

The run prints what it did with each outbox as JSON, and fails, after its `finished relay` record, with one ERROR line
that names each call refused or expired in it, its key and how it ended, and each row a failure stopped, so that the
scheduler's alert for a failed run fires. A call its service has not accepted yet fails nothing: a later run makes it
again.

A run reads the settings the API reads, and needs, of them, each outbox's database with its write login, and the URL of
each other service the API declares; a test gives fakes for them, as `api.build` takes them."""

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx2
from opentelemetry.trace import Tracer

from basis.api import fakes, outboxes
from basis.api.opened import HttpClient, OpenDatabase, client, faked, open_one, unconfigured
from basis.api.outboxes import Failure, Row
from basis.api.resources import DatabaseResource, DeadlineExceededError, HttpResource
from basis.api.service import Service, ServiceError
from basis.api.settings import Settings
from basis.core import opened
from basis.core.databases import Statements
from basis.core.logs import utc_now
from basis.core.settings import SettingsError

log = logging.getLogger(__name__)
BATCH = 100
"""The rows read at a time, each batch's calls made before the next is read."""
SHOWN = 5
"""The calls the ERROR line names, of each kind; the log names every one."""


@dataclass
class Relayed:
    """What one run did with one database's outbox."""

    database: str
    table: str
    accepted: int = 0
    """Calls the service accepted."""
    retried: int = 0
    """Calls the service did not accept yet, or could not be reached for, which a later run makes again."""
    taken: int = 0
    """Calls another relay, or a late request, changed first."""
    refused: list[Row] = field(default_factory=list[Row])
    """Calls the service refused for good, reported in this run: each row as it was refused, with its error."""
    expired: list[Row] = field(default_factory=list[Row])
    """Calls that aged out in this run, and are made no more."""
    failed: list[tuple[Row | None, str]] = field(default_factory=list[tuple[Row | None, str]])
    """Each row a failure stopped, with the failure; None for one that stopped the run over the outbox itself."""

    def summary(self) -> dict[str, object]:
        counts = {"accepted": self.accepted, "retried": self.retried, "taken": self.taken, "refused": len(self.refused), "expired": len(self.expired), "failed": len(self.failed)}
        return {"database": self.database, "table": self.table} | counts


@dataclass(frozen=True)
class Plan:
    service: Service
    settings: Settings
    outboxed: tuple[DatabaseResource, ...]
    """Each database whose outbox the service declares."""
    fakes: Mapping[str, fakes.Fake]
    """Each faked resource's fake, by name."""


def plan(service: Service, settings: Settings, given: fakes.Fakes) -> Plan:
    """What a run relays, after checking that the service keeps an outbox, that each fake fakes one of its databases or
    other services, and that the settings configure each the run reaches that has no fake."""
    outboxed = tuple(resource for resource in service.resources if isinstance(resource, DatabaseResource) and resource.outbox is not None)
    if not outboxed:
        msg = (
            f"service {service.name} keeps no outbox, so basis relay has no calls to make: declare one where a route sends a call after its commit,"
            " as api.database(NAME, ..., outbox=api.outbox(TABLE, ...)), or schedule no relay"
        )
        raise ServiceError(msg)
    reached: list[DatabaseResource | HttpResource] = [*outboxed, *(resource for resource in service.resources if isinstance(resource, HttpResource))]
    by_name = faked(given, reached, "which basis relay does not reach: fake the databases whose outboxes it relays, and the services it calls")
    if missing := [found for resource in reached if resource.name not in by_name and (found := unconfigured(resource, settings, written=True)) is not None]:
        msg = f"service {service.name}'s outboxes and services are not all configured, so basis relay cannot run: set {'; '.join(missing)}"
        raise SettingsError(msg)
    return Plan(service, settings, outboxed, by_name)


def relay(planned: Plan, tracer: Tracer, *, now: Callable[[], datetime] = utc_now) -> list[Relayed]:
    """One run over every outbox `planned` names, in order, at the times `now` gives."""
    return asyncio.run(_relay_all(planned, tracer, now))


async def _relay_all(planned: Plan, tracer: Tracer, now: Callable[[], datetime]) -> list[Relayed]:
    clients: dict[str, HttpClient] = {}
    made: list[httpx2.AsyncClient] = []
    try:
        for resource in planned.service.resources:
            if isinstance(resource, HttpResource):
                fake = planned.fakes.get(resource.name)
                reaching = fake.client(resource.name) if isinstance(fake, fakes.Http) else client(resource.name, planned.settings.http[resource.name])
                made.append(reaching)
                clients[resource.name] = HttpClient(opened.HttpClient(resource, reaching, tracer))
        found: list[Relayed] = []
        for resource in planned.outboxed:
            database = open_one(resource, planned.settings, planned.fakes.get(resource.name), tracer, set())
            try:
                found.append(await _Run(database, clients, now).relay())
            finally:
                database.pool.shutdown(wait=False, cancel_futures=True)  # a statement past its deadline ends on the driver's timeout
        return found
    finally:
        for reaching in made:
            await reaching.aclose()


@dataclass
class _Run:
    """One run over one database's outbox, on one connection through its write login at a time, which commits after
    each change of a row, and is opened anew after a failure."""

    database: OpenDatabase
    clients: Mapping[str, HttpClient]
    now: Callable[[], datetime]
    _stack: ExitStack | None = None
    _statements: Statements | None = None
    _running: asyncio.Task[None] | None = None
    """Ends when a statement that missed its deadline, and still runs on the connection, ends: the connection waits for it."""

    async def relay(self) -> Relayed:
        resource = self.database.resource
        declared = resource.outbox
        if declared is None:  # only databases that declare one are relayed
            msg = f"database {resource.name} declares no outbox"
            raise RuntimeError(msg)
        relayed = Relayed(resource.name, declared.table)
        try:
            started, seen = self.now(), set[str]()  # each row this run has taken, which a batch read again skips
            while True:
                due = await self._step("due", lambda table, s: table.due(started, BATCH, timeout_s=s), commit=False)
                if not (fresh := [row for row in due if row.id not in seen]):
                    break
                for row in fresh:
                    seen.add(row.id)
                    try:
                        await self._one(row, relayed)
                    except Exception as error:  # the row's alone: logged with its traceback, and the run goes on
                        log.exception("relaying %s failed: a later run takes it again", row.named(), extra=_fields(row, self.database))
                        relayed.failed.append((row, _described(error)))
                        await self._drop()
        except Exception as error:  # the outbox could not be read: logged, and the run goes on to the next
            log.exception("relaying database %s's outbox %s failed", resource.name, declared.table, extra={"database": resource.name})
            relayed.failed.append((None, _described(error)))
        finally:
            await self._drop()
        summary = relayed.summary()
        log.info("relayed %s's outbox %s: %s", resource.name, declared.table, ", ".join(f"{count} {name}" for name, count in summary.items() if isinstance(count, int)), extra=summary)
        return relayed

    async def _one(self, row: Row, relayed: Relayed) -> None:
        """Take `row` once: report it, if its service refused it; mark it expired, if it has aged out; and otherwise,
        unless another relay has claimed it, make its call and record how it went."""
        declared, at, fields = self.database.resource.outbox, self.now(), _fields(row, self.database)
        if declared is None:
            return
        if row.status == "refused":
            if await self._step("report", lambda table, s: outboxes.reported(table, row, timeout_s=s)):
                relayed.refused.append(row)
                log.error("%s refused %s, which is made no more: %s", row.service, row.named(), row.last_error, extra=fields | {"last_error": row.last_error})
            return
        if row.expires_at <= at:
            if await self._step("expire", lambda table, s: outboxes.expire(table, row, at, timeout_s=s)):
                relayed.expired.append(row)
                log.error("%s aged out before %s accepted it, after %s attempts, and is made no more", row.named(), row.service, row.attempts, extra=fields | {"last_error": row.last_error})
            return
        client = self.clients.get(row.service)
        lease = timedelta(seconds=(client.deadline_s if client is not None else 0) + 3 * self.database.resource.deadline_s)  # the call, and its record's statement and commit
        if not await self._step("claim", lambda table, s: outboxes.claim(table, row, at + lease, timeout_s=s)):
            relayed.taken += 1
            return
        attempts = row.attempts + 1
        failure = await self._made(row, client, relayed)
        ended = self.now()
        recorded = await self._step("record", lambda table, s: outboxes.record(table, declared, row, failure, seen=attempts, attempts=attempts, at=ended, reported=True, timeout_s=s))
        self._tally(row, failure, attempts, recorded, relayed)

    async def _made(self, row: Row, client: HttpClient | None, relayed: Relayed) -> Failure | None:
        """Make `row`'s call once: None when its service accepted it, and otherwise why not. A defect is the call's
        failure, logged with its traceback, and counted among the run's failures."""
        if client is None:
            return Failure(f"the service declares no api.http({row.service!r}) to make it with", None)
        try:
            return await client.deliver(row)
        except Exception as error:  # logged with its traceback; the call is recorded as an attempt that failed, and made again
            log.exception("making %s failed with a defect: a later run makes it again", row.named(), extra=_fields(row, self.database))
            relayed.failed.append((row, _described(error)))
            return Failure(_described(error), None)

    def _tally(self, row: Row, failure: Failure | None, attempts: int, recorded: outboxes.Status | None, relayed: Relayed) -> None:
        fields = _fields(row, self.database) | {"attempts": attempts}
        if recorded is None:
            relayed.taken += 1
            log.warning("%s changed while it was made, so its attempt is not recorded", row.named(), extra=fields)
        elif failure is None:
            relayed.accepted += 1
            log.info("%s accepted %s, at attempt %s", row.service, row.named(), attempts, extra=fields)
        elif recorded == "refused":
            relayed.refused.append(Row.model_validate(row.model_dump() | {"status": "refused", "attempts": attempts, "last_error": failure.error}))
            log.error("%s refused %s, which is made no more: %s", row.service, row.named(), failure.error, extra=fields | {"error": failure.error, "reply": failure.reply})
        else:
            relayed.retried += 1
            log.warning("%s did not accept %s: a later run makes it again", row.service, row.named(), extra=fields | {"error": failure.error, "reply": failure.reply})

    async def _step[T](self, operation: str, work: Callable[[outboxes.Table, float], T], *, commit: bool = True) -> T:
        """`work` on the outbox, then the commit unless it only read, each a statement with a span of its own and the
        database's deadline, on one of the database's threads."""
        statements, deadline_s = await self._connected(), self.database.resource.deadline_s
        with self.database.span(f"outbox {operation}"):
            done = await self._bounded(lambda: work(self.database.table(statements), deadline_s), f"its outbox's {operation}")
        if commit:
            with self.database.span("commit"):
                await self._bounded(statements.commit, f"the commit of its outbox's {operation}")
        return done

    async def _connected(self) -> Statements:
        """The run's connection through the write login, opened within the database's deadline where there is none."""
        if self._statements is None:
            stack = ExitStack()
            self._stack = stack  # closed by `_drop`, should the login finish after its deadline
            with self.database.span("open"):
                self._statements = await self._bounded(lambda: stack.enter_context(self.database.port.statements(writes=True)), "its login")
        return self._statements

    async def _bounded[T](self, work: Callable[[], T], what: str) -> T:
        """`work` on one of the database's threads, within its deadline: one that misses it runs on, on the connection,
        which `_drop` then waits for."""
        resource = self.database.resource
        running = self.database.run(work)
        deadline = asyncio.timeout(resource.deadline_s)
        try:
            async with deadline:
                return await asyncio.shield(running)
        except TimeoutError as error:
            if not deadline.expired():
                raise
            self._running = asyncio.get_running_loop().create_task(_settled(running))
            detail = f"{what} in database {resource.name} did not finish within its deadline of {resource.deadline_s:g}s"
            raise DeadlineExceededError(resource.name, resource.deadline_s, detail=detail) from error
        except asyncio.CancelledError:
            self._running = asyncio.get_running_loop().create_task(_settled(running))
            raise

    async def _drop(self) -> None:
        """Give back the connection, once a statement that missed its deadline has ended on it: what it has not
        committed rolls back, and the next step opens another."""
        running, stack = self._running, self._stack
        self._running = self._statements = self._stack = None
        if running is not None:
            await running  # the driver's own timeout, the deadline, ends it
        if stack is not None:
            try:
                await self.database.run(stack.close)
            except Exception:  # logged: closing the connection ends its transaction anyway
                log.warning("could not close database %s's connection cleanly", self.database.resource.name, exc_info=True, extra={"database": self.database.resource.name})


async def _settled[T](running: asyncio.Future[T]) -> None:
    """Wait for a statement that missed its deadline to end, however it ends: its outcome no longer matters."""
    await asyncio.wait([running])
    if not running.cancelled():
        running.exception()  # retrieved, so the loop logs no warning of an error nobody read


def _described(error: BaseException) -> str:
    return f"{type(error).__name__}: {' '.join(str(error).split())}"


def _fields(row: Row, database: OpenDatabase) -> dict[str, object]:
    return {"database": database.resource.name, "outbox_row": row.id, "service": row.service, "method": row.method, "path": row.path, "idempotency_key": row.idempotency_key, "route": row.route}


def failure(found: Sequence[Relayed], service: str) -> tuple[str, str] | None:
    """The run's failure, the reason and the fix, when it reported a call refused or expired, or a failure stopped a row;
    None when none did."""
    refused = [f"{row.named()}, in {relayed.database}'s {relayed.table}, refused with: {row.last_error or 'nothing recorded'}" for relayed in found for row in relayed.refused]
    expired = [
        f"{row.named()}, in {relayed.database}'s {relayed.table}, aged out after {row.attempts} attempts, the last refused with: {row.last_error or 'nothing recorded'}"
        for relayed in found
        for row in relayed.expired
    ]
    failed = [f"{row.named() if row is not None else f'the outbox {relayed.table} itself'}, in {relayed.database}: {error}" for relayed in found for row, error in relayed.failed]
    parts = [
        f"{_calls(len(named))} {what}: {'; '.join(named[:SHOWN])}{f'; and {len(named) - SHOWN} more' if len(named) > SHOWN else ''}"
        for what, named in (("refused for good by its service, and made no more", refused), ("aged out, and made no more", expired), ("stopped by a failure, which the log holds", failed))
        if named
    ]
    if not parts:
        return None
    reason = f"service {service}'s outbox has {'; '.join(parts)}"
    fix = (
        "find out why each service refused or never accepted its call, or what failed; to make a refused or expired call again,"
        " set its row's status to 'pending', its next_at to now and its expires_at later, and the next basis relay makes it"
    )
    return reason, fix


def _calls(count: int) -> str:
    return "a call" if count == 1 else f"{count} calls"
