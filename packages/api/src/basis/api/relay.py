"""`basis relay [SERVICE]`: the run that makes again the calls a service's outboxes still hold (`basis.api.outboxes`).
The repo's scheduler runs it on a schedule, from the API's image and with the API's settings; it never runs inside
the API's replicas.

One run takes each database whose outbox the service declares, in turn, and each of its calls that is due, the longest
due first, once. It claims the call's row, so that a relay running at the same time leaves it alone; makes the call,
with its `Idempotency-Key`, within the other service's deadline; and records it `done`, or one more failed attempt,
due again after the outbox's backoff. A call that has aged out is made no more: its row is `expired`, it is logged as
an error with everything about it, and the run fails, its one ERROR line naming each such call and its key, so that
the scheduler's alert for a failed run fires. A call the service does not accept yet fails nothing: a later run makes
it again. The run prints what it did with each outbox as JSON.

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
from basis.api.opened import HttpClient, OpenDatabase, client, fits, open_one, unconfigured
from basis.api.outboxes import Row
from basis.api.resources import DatabaseResource, HttpResource
from basis.api.service import Service, ServiceError
from basis.api.settings import Settings
from basis.core.databases import Statements
from basis.core.logs import utc_now
from basis.core.settings import SettingsError

log = logging.getLogger(__name__)
BATCH = 100
"""The rows read at a time, each batch's calls made before the next is read."""


@dataclass
class Relayed:
    """What one run did with one database's outbox."""

    database: str
    table: str
    accepted: int = 0
    """Calls the service accepted."""
    refused: int = 0
    """Calls the service did not accept, or could not be reached for, which a later run makes again."""
    taken: int = 0
    """Calls another relay claimed first."""
    expired: list[Row] = field(default_factory=list[Row])
    """Calls that aged out in this run, and are made no more."""

    def summary(self) -> dict[str, object]:
        return {"database": self.database, "table": self.table, "accepted": self.accepted, "refused": self.refused, "taken": self.taken, "expired": len(self.expired)}


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
    by_name: dict[str, fakes.Fake] = {}
    for resource, fake in given.items():
        if not isinstance(resource, DatabaseResource | HttpResource) or resource not in reached:
            msg = f"a fake is given for {resource.name}, which basis relay does not reach: fake the databases whose outboxes it relays, and the services it calls"
            raise ServiceError(msg)
        if not fits(resource, fake):
            msg = f"{resource.name} is given a fakes.{type(fake).__name__}, which does not fake its kind of resource: give it the fake of its kind"
            raise ServiceError(msg)
        by_name[resource.name] = fake
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
                opened = fake.client(resource.name) if isinstance(fake, fakes.Http) else client(resource.name, planned.settings.http[resource.name])
                made.append(opened)
                clients[resource.name] = HttpClient(resource, opened, tracer)
        found: list[Relayed] = []
        for resource in planned.outboxed:
            database = open_one(resource, planned.settings, planned.fakes.get(resource.name), tracer, set())
            try:
                found.append(await _Run(database, clients, now).relay())
            finally:
                database.pool.shutdown(wait=True)
        return found
    finally:
        for opened in made:
            await opened.aclose()


@dataclass
class _Run:
    """One run over one database's outbox, on one connection through its write login, which commits after each change."""

    database: OpenDatabase
    clients: Mapping[str, HttpClient]
    now: Callable[[], datetime]

    async def relay(self) -> Relayed:
        resource = self.database.resource
        declared = resource.outbox
        if declared is None:  # only databases that declare one are relayed
            msg = f"database {resource.name} declares no outbox"
            raise RuntimeError(msg)
        relayed = Relayed(resource.name, declared.table)
        stack = ExitStack()
        statements = await self.database.run(lambda: stack.enter_context(self.database.port.statements(writes=True)))
        try:
            started, seen = self.now(), set[str]()  # each row this run has taken, which a batch read again skips
            while True:
                due = await self._step(statements, lambda table, s: table.due(started, BATCH, timeout_s=s), commit=False)
                if not (fresh := [row for row in due if row.id not in seen]):
                    break
                for row in fresh:
                    seen.add(row.id)
                    await self._one(statements, row, relayed)
        finally:
            await self.database.run(stack.close)
        log.info("relayed %s's outbox %s: %s accepted, %s refused, %s expired", resource.name, declared.table, relayed.accepted, relayed.refused, len(relayed.expired), extra=relayed.summary())
        return relayed

    async def _one(self, statements: Statements, row: Row, relayed: Relayed) -> None:
        """Make `row`'s call once, unless it has aged out or another relay has claimed it, and record how it went."""
        declared, at = self.database.resource.outbox, self.now()
        if declared is None:
            return
        fields = _fields(row, self.database)
        if row.expires_at <= at:
            if await self._step(statements, lambda table, s: table.expire(row.id, at=at, timeout_s=s)):
                relayed.expired.append(row)
                log.error("%s aged out before %s accepted it, after %s attempts, and is made no more", row.named(), row.service, row.attempts, extra=fields | {"last_error": row.last_error})
            return
        client = self.clients.get(row.service)
        lease = timedelta(seconds=self.database.resource.deadline_s + (client.deadline_s if client is not None else 0))
        if not await self._step(statements, lambda table, s: table.claim(row, at + lease, timeout_s=s)):
            relayed.taken += 1
            return
        attempts = row.attempts + 1
        failure = await client.deliver(row) if client is not None else f"the service declares no api.http({row.service!r}) to make it with"
        ended = self.now()
        if failure is None:
            await self._step(statements, lambda table, s: table.accepted(row.id, attempts=attempts, at=ended, timeout_s=s))
            relayed.accepted += 1
            log.info("%s accepted %s, at attempt %s", row.service, row.named(), attempts, extra=fields | {"attempts": attempts})
            return
        again = outboxes.again_at(declared, row, attempts, ended)
        await self._step(statements, lambda table, s: table.refused(row.id, attempts=attempts, next_at=again, error=failure, timeout_s=s))
        relayed.refused += 1
        retry = {"attempts": attempts, "error": failure, "next_at": again.isoformat()}
        log.warning("%s did not accept %s: it is made again from %s", row.service, row.named(), again.isoformat(), extra=fields | retry)

    async def _step[T](self, statements: Statements, work: Callable[[outboxes.Table, float], T], *, commit: bool = True) -> T:
        """`work` on the outbox, on one of the database's threads, each statement within the database's deadline; then
        commit, unless it only read."""
        database = self.database

        def step() -> T:
            done = work(database.table(statements), database.resource.deadline_s)
            if commit:
                statements.commit()
            return done

        return await database.run(step)


def _fields(row: Row, database: OpenDatabase) -> dict[str, object]:
    return {"database": database.resource.name, "outbox_row": row.id, "service": row.service, "method": row.method, "path": row.path, "idempotency_key": row.idempotency_key, "route": row.route}


def failure(found: Sequence[Relayed], service: str) -> tuple[str, str] | None:
    """The run's failure, the reason and the fix, when calls aged out in it; None when none did."""
    expired = [(relayed, row) for relayed in found for row in relayed.expired]
    if not expired:
        return None
    shown = 5
    named = "; ".join(
        f"{row.named()}, in {relayed.database}'s {relayed.table}, after {row.attempts} attempts, the last refused with: {row.last_error or 'nothing recorded'}" for relayed, row in expired[:shown]
    )
    more = f"; and {len(expired) - shown} more, each now a row whose status is 'expired'" if len(expired) > shown else ""
    if len(expired) == 1:
        aged = f"a call in service {service}'s outbox aged out before its service accepted it, and is made no more"
    else:
        aged = f"{len(expired)} calls in service {service}'s outbox aged out before their services accepted them, and are made no more"
    reason = f"{aged}: {named}{more}"
    fix = "find out why each service refused its call; to make one again, set its row's status to 'pending', its next_at to now and its expires_at later, and the next basis relay makes it"
    return reason, fix
