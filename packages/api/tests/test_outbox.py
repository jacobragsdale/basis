"""The outbox, on fakes: a call a route that writes sends is made only once its writes commit, with its key as its
`Idempotency-Key`, and its failure then changes nothing the request answers; `basis relay` makes again each call that
is due, with backoff, until its service accepts it or it ages out, which fails the run; and `basis ddl` prints the
outbox's table. The real databases' SQL is the integration tier's (`integration/test_outbox.py`)."""

import asyncio
import contextlib
import hashlib
import json
import os
import re
import sys
import textwrap
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http import HTTPStatus
from pathlib import Path
from typing import Annotated, override

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NoOpTracer, Tracer
from pydantic import BaseModel, ConfigDict
from starlette.types import Receive, Scope, Send

from basis import api
from basis.api import fakes, outboxes, relay
from basis.api.outboxes import Row
from basis.api.resources import DatabaseResource
from basis.core import command

type Records = Callable[[], list[dict[str, object]]]  # the `logged` fixture's

OUTBOX = api.outbox("prices_outbox", max_age_s=3600, backoff_s=60, max_backoff_s=600)
WAREHOUSE = api.database("warehouse", deadline_s=1, max_concurrent=4, outbox=OUTBOX)
PLAIN = api.database("plain", deadline_s=1, max_concurrent=4)
LEDGER = api.http("ledger", deadline_s=1)
INSERT = "INSERT INTO prices (security, price) VALUES (%(security)s, %(price)s)"
FAILS = "UPDATE prices SET price = price * 2"
SECURITY = "US912810TM64"
PRICE = {"security": SECURITY, "price": "101.25"}
AT = datetime(2026, 11, 6, 14, 30, tzinfo=UTC)


class Price(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    security: str
    price: Decimal


class Refused(api.Error):
    status = HTTPStatus.CONFLICT
    title = "The price is refused"


async def book(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)], ledger: Annotated[api.Client, api.given(LEDGER)]) -> Price:
    await warehouse.execute(INSERT, security=price.security, price=price.price)
    await ledger.send("POST", "/bookings", price, key=f"price-{price.security}")
    return price


async def book_then_refuse(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)], ledger: Annotated[api.Client, api.given(LEDGER)]) -> Price:
    await book(price, warehouse, ledger)
    msg = f"{price.security} is refused after all"
    raise Refused(msg)


async def book_then_misanswer(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)], ledger: Annotated[api.Client, api.given(LEDGER)]) -> Price:
    await book(price, warehouse, ledger)
    return {"security": price.security, "price": "not a number"}  # ty: ignore[invalid-return-type] -- a response that does not validate, on purpose


async def book_then_carry_on(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)], ledger: Annotated[api.Client, api.given(LEDGER)]) -> Price:
    await book(price, warehouse, ledger)
    with contextlib.suppress(LookupError):  # the fake has no answer for it, as a database's error would be
        await warehouse.execute(FAILS)
    return price


async def book_without_an_outbox(price: Price, plain: Annotated[api.Writer, api.given(PLAIN)], ledger: Annotated[api.Client, api.given(LEDGER)]) -> Price:
    await plain.execute(INSERT, security=price.security, price=price.price)
    await ledger.send("POST", "/bookings", price, key=f"price-{price.security}")
    return price


async def book_directly(price: Price, ledger: Annotated[api.Client, api.given(LEDGER)]) -> Price:
    await ledger.send("POST", "/bookings", price, key=f"price-{price.security}")
    return price


async def book_with_a_bad_key(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)], ledger: Annotated[api.Client, api.given(LEDGER)]) -> Price:
    await warehouse.execute(INSERT, security=price.security, price=price.price)
    await ledger.send("POST", "/bookings", price, key="two words")
    return price


async def book_twice(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)], ledger: Annotated[api.Client, api.given(LEDGER)]) -> Price:
    await warehouse.execute(INSERT, security=price.security, price=price.price)
    for key in ("first", "second"):
        await ledger.send("POST", "/bookings", price, key=key)
    return price


def _post(path: str, handler: Callable[..., object], *errors: type[api.Error], deadline_s: float = 10) -> api.Route:
    return api.post(path, handler, deadline_s=deadline_s, response=Price, errors=list(errors))  # ty: ignore[invalid-argument-type] -- each handler is a function


SERVICE = api.Service(
    name="prices",
    settings=api.Settings,
    resources=[WAREHOUSE, PLAIN, LEDGER],
    routes=[
        _post("/book", book),
        _post("/refused", book_then_refuse, Refused),
        _post("/misanswered", book_then_misanswer),
        _post("/carried-on", book_then_carry_on),
        _post("/no-outbox", book_without_an_outbox),
        _post("/direct", book_directly),
        _post("/bad-key", book_with_a_bad_key),
        _post("/twice", book_twice, deadline_s=0.7),
    ],
)


class Ledger:
    """The ledger service, as a fake: what it was sent, with the outbox's rows as they stood at each call, and the
    status it answers, after `wait_s`, which a test changes."""

    def __init__(self, warehouse: fakes.Database, *, status: int = HTTPStatus.CREATED, wait_s: float = 0) -> None:
        self.status, self.wait_s = status, wait_s
        self.seen: list[tuple[str | None, object, list[Row]]] = []
        self.app = FastAPI()

        @self.app.post("/bookings")
        async def bookings(request: Request) -> Response:
            self.seen.append((request.headers.get("idempotency-key"), json.loads(await request.body()), warehouse.outbox))
            await asyncio.sleep(self.wait_s)
            return Response(status_code=self.status)


def _app(warehouse: fakes.Database, ledger: Ledger, service: api.Service = SERVICE) -> api.App:
    return api.build(service, settings=api.Settings.given(), fakes={WAREHOUSE: warehouse, PLAIN: fakes.Database(answers={INSERT: 1}), LEDGER: fakes.Http(ledger.app)})


def _warehouse(*rows: Row) -> fakes.Database:
    return fakes.Database(answers={INSERT: 1}, outbox=rows)


# Declarations


@pytest.mark.parametrize(
    ("declare", "message"),
    [
        (lambda: api.outbox("prices outbox", max_age_s=1, backoff_s=1, max_backoff_s=1), "an outbox's table is a name, or schema.name"),
        (lambda: api.outbox("a.b.c", max_age_s=1, backoff_s=1, max_backoff_s=1), "an outbox's table is a name, or schema.name"),
        (lambda: api.outbox("ops." + "t" * 126, max_age_s=1, backoff_s=1, max_backoff_s=1), "name is 126 characters: give it 125 at most, after its schema"),
        (lambda: api.outbox("prices_outbox", max_age_s=0, backoff_s=1, max_backoff_s=1), "max_age_s is 0: give it the seconds, above 0"),
        (lambda: api.outbox("prices_outbox", max_age_s=1, backoff_s=float("inf"), max_backoff_s=1), "backoff_s is inf"),
        (lambda: api.outbox("prices_outbox", max_age_s=1, backoff_s=10, max_backoff_s=5), "give max_backoff_s at least backoff_s"),
        (lambda: api.database("warehouse", deadline_s=1, max_concurrent=1, outbox="prices_outbox"), r"declare it with api.outbox\(TABLE"),  # ty: ignore[invalid-argument-type] -- wrong on purpose
    ],
    ids=["a-space", "three-parts", "name-too-long", "no-age", "endless-backoff", "backoff-past-its-maximum", "a-name-for-an-outbox"],
)
def test_an_outbox_cannot_be_declared_wrong(declare: Callable[[], object], message: str) -> None:
    with pytest.raises(api.ServiceError, match=message):
        declare()


@pytest.mark.parametrize(("max_age_s", "declared"), [(70, "refused"), (70.5, "kept")], ids=["at-the-deadline-and-a-backoff", "past-them"])
def test_an_outbox_leaves_room_past_the_deadline_of_each_route_that_writes_its_database(max_age_s: float, declared: str) -> None:
    short = api.database("warehouse", deadline_s=1, max_concurrent=4, outbox=api.outbox("prices_outbox", max_age_s=max_age_s, backoff_s=60, max_backoff_s=600))

    async def write(price: Price, warehouse: Annotated[api.Writer, api.given(short)]) -> Price:
        del warehouse
        return price

    def declare() -> api.Service:
        return api.Service(name="prices", settings=api.Settings, resources=[short], routes=[_post("/w", write)])

    if declared == "kept":
        declare()
        return
    with pytest.raises(api.ServiceError, match=r"route write, which writes warehouse, has 10s to answer.* give the outbox a max_age_s above 70s"):
        declare()


@pytest.mark.parametrize(("attempts", "wait_s"), [(1, 60), (2, 120), (4, 480), (5, 600), (10_000, 600)])
def test_each_failure_doubles_the_wait_up_to_its_maximum(attempts: int, wait_s: float) -> None:
    assert outboxes.backoff(OUTBOX, attempts) == timedelta(seconds=wait_s)


def test_a_call_is_due_again_no_later_than_it_ages_out() -> None:
    row = _row(expires_at=AT + timedelta(seconds=90))
    assert (outboxes.again_at(OUTBOX, row, 1, AT), outboxes.again_at(OUTBOX, row, 2, AT)) == (AT + timedelta(seconds=60), AT + timedelta(seconds=90))


@pytest.mark.parametrize(("reply", "then"), [*((reply, "refused") for reply in (400, 404, 422)), *((reply, "retried") for reply in (408, 409, 425, 429, 500, 503, 302, None))])
def test_a_4xx_refuses_a_call_for_good_but_a_timeout_a_conflict_too_early_and_too_many(reply: int | None, then: str) -> None:
    assert outboxes.Failure("refused", reply).permanent is (then == "refused")


@pytest.mark.parametrize(
    ("path", "key", "message"),
    [
        ("/bookings", "", "is not one to 255 visible ASCII characters"),
        ("/bookings", "two words", "is not one to 255 visible ASCII characters"),
        ("/bookings", "k" * 256, "is not one to 255 visible ASCII characters"),
        ("/bookings", "clé", "is not one to 255 visible ASCII characters"),
        ("bookings", "k", "give a str that starts with /"),
        ("/" + "b" * 2000, "k", "of at most 2000 characters"),
        (None, "k", "the path None is not the service's own path"),
        (b"/bookings", "k", "the path b'/bookings' is not the service's own path"),
    ],
    ids=["empty", "a-space", "too-long", "not-ascii", "no-slash", "path-too-long", "no-path", "bytes"],
)
def test_a_call_the_outbox_cannot_hold_is_refused(path: object, key: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        outboxes.checked(path, key)


def test_the_longest_key_is_held() -> None:
    outboxes.checked("/bookings", "k" * 255)


@pytest.mark.parametrize(
    ("table", "dialect", "name"),
    [
        ("prices_outbox", "oracle", "ix_prices_outbox_due"),
        ("ops.prices_outbox", "oracle", "ops.ix_prices_outbox_due"),
        ("ops.prices_outbox", "mssql", "ix_prices_outbox_due"),
        ("OPS." + "T" * 125, "oracle", "ops.ix_" + "t" * 112 + "_" + hashlib.sha256(("ops." + "t" * 125).encode()).hexdigest()[:8] + "_due"),
    ],
    ids=["no-schema", "oracle-in-the-tables-schema", "sql-server-on-its-table", "too-long-for-a-name"],
)
def test_the_due_index_is_named_within_128_characters_in_the_tables_schema(table: str, dialect: str, name: str) -> None:
    found = outboxes.index(table, "mssql" if dialect == "mssql" else "oracle")
    assert (found, len(found.rpartition(".")[2]) <= outboxes.NAME_LENGTH) == (name, True)


# After the commit


def test_a_call_is_made_after_the_commit_with_its_key_and_its_row_is_done() -> None:
    warehouse = _warehouse()
    ledger = Ledger(warehouse)
    with TestClient(_app(warehouse, ledger).asgi) as client:
        response = client.post("/book", json=PRICE)
    assert (response.status_code, response.json()) == (HTTPStatus.OK, PRICE)
    [(key, body, rows_then)] = ledger.seen
    assert (key, body) == (f"price-{SECURITY}", PRICE)
    assert [(row.status, row.attempts) for row in rows_then] == [("pending", 0)]  # committed before the call was made
    [row] = warehouse.outbox
    assert (row.status, row.attempts, row.next_at, row.route, row.service, row.method, row.path) == ("done", 1, None, "book", "ledger", "POST", "/bookings")
    assert row.ended_at is not None


@pytest.mark.parametrize(
    ("path", "status"),
    [("/refused", HTTPStatus.CONFLICT), ("/misanswered", HTTPStatus.INTERNAL_SERVER_ERROR), ("/carried-on", HTTPStatus.INTERNAL_SERVER_ERROR)],
    ids=["declared-error", "response-does-not-validate", "a-statement-failed"],
)
def test_a_call_whose_writes_roll_back_is_never_made(path: str, status: int) -> None:
    warehouse = _warehouse()
    ledger = Ledger(warehouse)
    with TestClient(_app(warehouse, ledger).asgi) as client:
        assert client.post(path, json=PRICE).status_code == status
    assert (ledger.seen, warehouse.outbox, len(warehouse.rolled_back)) == ([], [], 1)


@pytest.mark.parametrize("status", [HTTPStatus.SERVICE_UNAVAILABLE, HTTPStatus.CONFLICT, HTTPStatus.TOO_MANY_REQUESTS])
def test_a_call_the_service_does_not_accept_yet_changes_nothing_the_request_answers(status: int, logged: Records) -> None:
    warehouse = _warehouse()
    ledger = Ledger(warehouse, status=status)
    with TestClient(_app(warehouse, ledger).asgi) as client:
        response = client.post("/book", json=PRICE)
    assert (response.status_code, response.json()) == (HTTPStatus.OK, PRICE)
    [row] = warehouse.outbox
    assert (row.status, row.attempts, row.last_error) == ("pending", 1, f"ledger answered {status} to POST /bookings")
    assert row.next_at is not None
    assert row.next_at - row.created_at > timedelta(seconds=59)  # its backoff, from the attempt
    [warned] = [record for record in logged() if str(record["message"]).startswith("ledger did not accept")]
    assert (warned["level"], warned["idempotency_key"], warned["reply"]) == ("warning", f"price-{SECURITY}", status)


def test_a_call_the_service_refuses_for_good_after_the_commit_is_refused_and_due_to_be_reported() -> None:
    warehouse = _warehouse()
    ledger = Ledger(warehouse, status=HTTPStatus.UNPROCESSABLE_CONTENT)
    with TestClient(_app(warehouse, ledger).asgi) as client:
        assert client.post("/book", json=PRICE).status_code == HTTPStatus.OK
    [row] = warehouse.outbox
    assert (row.status, row.attempts, row.last_error, row.next_at) == ("refused", 1, "ledger answered 422 to POST /bookings", row.ended_at)
    assert row.ended_at is not None


def test_a_requests_calls_after_its_commit_are_made_at_once_within_its_deadline() -> None:
    """Two calls of 0.4s each, on a route with 0.7s to answer: one after the other, the second would miss it."""
    warehouse = _warehouse()
    ledger = Ledger(warehouse, wait_s=0.4)
    with TestClient(_app(warehouse, ledger).asgi) as client:
        assert client.post("/twice", json=PRICE).status_code == HTTPStatus.OK
    assert sorted((row.idempotency_key, row.status) for row in warehouse.outbox) == [("first", "done"), ("second", "done")]


def test_a_call_the_route_has_no_time_left_to_make_is_due_when_its_deadline_passes() -> None:
    warehouse = _warehouse()
    ledger = Ledger(warehouse, wait_s=5)
    brief = api.Service(name="prices", settings=api.Settings, resources=[WAREHOUSE, PLAIN, LEDGER], routes=[_post("/book", book, deadline_s=0.3)])
    with TestClient(_app(warehouse, ledger, brief).asgi) as client:
        assert client.post("/book", json=PRICE).status_code == HTTPStatus.OK
    [row] = warehouse.outbox
    assert (row.status, row.attempts) == ("pending", 0)  # the call missed the deadline, and so did its record
    assert row.next_at is not None
    assert timedelta(seconds=0.2) < row.next_at - row.created_at <= timedelta(seconds=0.3)


def test_a_route_that_sends_through_a_database_without_an_outbox_is_a_defect(logged: Records) -> None:
    with TestClient(_app(_warehouse(), Ledger(_warehouse())).asgi) as client:
        assert client.post("/no-outbox", json=PRICE).status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    [failed] = [record for record in logged() if record["level"] == "error"]
    assert "book_without_an_outbox sends POST /bookings to ledger, and writes plain, which declares no outbox" in str(failed["error"])


def test_a_call_with_a_key_the_outbox_cannot_hold_is_a_defect_and_rolls_back() -> None:
    warehouse = _warehouse()
    ledger = Ledger(warehouse)
    with TestClient(_app(warehouse, ledger).asgi) as client:
        assert client.post("/bad-key", json=PRICE).status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert (ledger.seen, warehouse.outbox, len(warehouse.rolled_back)) == ([], [], 1)


@pytest.mark.parametrize(("status", "answered"), [(HTTPStatus.CREATED, HTTPStatus.OK), (HTTPStatus.SERVICE_UNAVAILABLE, HTTPStatus.BAD_GATEWAY)], ids=["accepted", "refused"])
def test_a_route_that_writes_no_database_makes_the_call_at_once(status: int, answered: int) -> None:
    warehouse = _warehouse()
    ledger = Ledger(warehouse, status=status)
    with TestClient(_app(warehouse, ledger).asgi) as client:
        assert client.post("/direct", json=PRICE).status_code == answered
    assert ([key for key, _, _ in ledger.seen], warehouse.outbox) == ([f"price-{SECURITY}"], [])


# One basis app reached from another in this process, as fakes.Http reaches it


NOTES_DB = api.database("notes_db", deadline_s=1, max_concurrent=4, outbox=api.outbox("notes_outbox", max_age_s=3600, backoff_s=60, max_backoff_s=600))
MAIL = api.http("mail", deadline_s=1)
NOTES = api.http("notes", deadline_s=5)
NOTE = "INSERT INTO notes (security) VALUES (%(security)s)"


async def remember(price: Price, notes_db: Annotated[api.Writer, api.given(NOTES_DB)], mail: Annotated[api.Client, api.given(MAIL)]) -> Price:
    await notes_db.execute(NOTE, security=price.security)
    await mail.send("POST", "/mail", price, key="remembered")
    return price


async def mention(price: Price, mail: Annotated[api.Client, api.given(MAIL)]) -> Price:
    await mail.send("POST", "/mail", price, key="mentioned")
    return price


async def book_and_note(
    price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)], notes: Annotated[api.Client, api.given(NOTES)], ledger: Annotated[api.Client, api.given(LEDGER)]
) -> Price:
    """Writes, has the notes service, another basis app, note the price by the route the request's query names, and
    then sends its own call."""
    await warehouse.execute(INSERT, security=price.security, price=price.price)
    await notes.post(f"/{price.security.lower()}", Price, body=price)
    await ledger.send("POST", "/bookings", price, key="booked")
    return price


def test_a_call_one_app_sends_never_goes_through_another_apps_transaction() -> None:
    """The notes app's routes run in the booking request's own context: one that writes nothing sends its call itself,
    not through the booking's transaction, and one that writes sends through its own, and the booking's call through
    the booking's once it returns."""
    mailed: list[str | None] = []
    mail = FastAPI()

    @mail.post("/mail")
    async def mailbox(request: Request) -> Response:
        mailed.append(request.headers.get("idempotency-key"))
        return Response(status_code=HTTPStatus.CREATED)

    notes_db = fakes.Database(answers={NOTE: 1})
    notes = api.Service(name="notes", settings=api.Settings, resources=[NOTES_DB, MAIL], routes=[_post("/remember", remember), _post("/mention", mention)])
    noting = api.build(notes, settings=api.Settings.given(), fakes={NOTES_DB: notes_db, MAIL: fakes.Http(mail)})
    booking = api.Service(name="booking", settings=api.Settings, resources=[WAREHOUSE, NOTES, LEDGER], routes=[_post("/book", book_and_note)])
    warehouse = _warehouse()
    with TestClient(noting.asgi) as started:  # its lifespan, whose state each request it serves carries, as a server's do

        async def notes_app(scope: Scope, receive: Receive, send: Send) -> None:
            await noting.asgi({**scope, "state": dict(started.app_state)}, receive, send)

        app = api.build(booking, settings=api.Settings.given(), fakes={WAREHOUSE: warehouse, NOTES: fakes.Http(notes_app), LEDGER: fakes.Http(Ledger(warehouse).app)})
        with TestClient(app.asgi) as client:
            assert client.post("/book", json={"security": "MENTION", "price": "101.25"}).status_code == HTTPStatus.OK
            assert client.post("/book", json={"security": "REMEMBER", "price": "101.25"}).status_code == HTTPStatus.OK
    assert mailed == ["mentioned", "remembered"]
    assert [(row.service, row.idempotency_key, row.status) for row in warehouse.outbox] == [("ledger", "booked", "done")] * 2
    assert [(row.service, row.idempotency_key, row.status) for row in notes_db.outbox] == [("mail", "remembered", "done")]


# The outbox's rows


def test_of_two_transactions_that_claim_one_row_the_second_changes_nothing() -> None:
    warehouse = _warehouse(_row())
    with warehouse.statements(writes=True) as first, warehouse.statements(writes=True) as second:
        assert isinstance(first, fakes.Transaction) and isinstance(second, fakes.Transaction)  # noqa: PT018 -- the fake's own
        [due], [also_due] = first.outbox().due(AT, 10, timeout_s=1), second.outbox().due(AT, 10, timeout_s=1)
        claimed = [outboxes.claim(first.outbox(), due, AT + timedelta(minutes=1), timeout_s=1), outboxes.claim(second.outbox(), also_due, AT + timedelta(minutes=1), timeout_s=1)]
        first.commit()
        second.commit()
    assert (claimed, [row.attempts for row in warehouse.outbox]) == ([True, False], [2])


def test_a_claim_that_rolls_back_leaves_the_row_to_the_next() -> None:
    warehouse = _warehouse(_row())
    with warehouse.statements(writes=True) as first:
        assert isinstance(first, fakes.Transaction)
        assert outboxes.claim(first.outbox(), _row(), AT + timedelta(minutes=1), timeout_s=1)
    with warehouse.statements(writes=True) as second:
        assert isinstance(second, fakes.Transaction)
        assert outboxes.claim(second.outbox(), _row(), AT + timedelta(minutes=1), timeout_s=1)
        second.commit()
    assert [row.attempts for row in warehouse.outbox] == [2]


def test_a_requests_late_record_never_overwrites_a_relays_claim() -> None:
    """A row a relay has claimed, as the request's record of its own attempt comes too late: the record changes nothing."""
    claimed = _row(attempts=1, next_at=AT + timedelta(minutes=1), last_error=None)
    warehouse = _warehouse(claimed)
    with warehouse.statements(writes=True) as late:
        assert isinstance(late, fakes.Transaction)
        recorded = outboxes.record(late.outbox(), OUTBOX, claimed, None, seen=0, attempts=1, at=AT, reported=False, timeout_s=1)
        late.commit()
    assert (recorded, warehouse.outbox) == (None, [claimed])


# The relay


def _row(**changed: object) -> Row:
    """A call in the outbox, due at `AT`, which its tests change only where they are about it."""
    row = Row(
        id="0190f1a2-0000-7000-8000-000000000001", service="ledger", method="POST", path="/bookings", body=json.dumps(PRICE), idempotency_key=f"price-{SECURITY}",
        route="book", request_id=None, created_at=AT - timedelta(minutes=5), expires_at=AT + timedelta(hours=1), next_at=AT, attempts=1, status="pending",
        last_error="ledger answered 503 to POST /bookings", ended_at=None,
    )  # fmt: skip
    return Row.model_validate(row.model_dump() | changed)


def _relay(warehouse: fakes.Database, ledger: Ledger | FastAPI, *, at: datetime = AT, tracer: Tracer | None = None, service: api.Service = SERVICE) -> list[relay.Relayed]:
    app = ledger.app if isinstance(ledger, Ledger) else ledger
    database = next(resource for resource in service.resources if isinstance(resource, DatabaseResource) and resource.outbox is not None)
    planned = relay.plan(service, api.Settings.given(), {database: warehouse, LEDGER: fakes.Http(app)})
    return relay.relay(planned, tracer or NoOpTracer(), now=lambda: at)


def test_the_relay_makes_a_due_call_with_its_key_and_records_it_done() -> None:
    warehouse = _warehouse(_row())
    ledger = Ledger(warehouse)
    [relayed] = _relay(warehouse, ledger)
    assert [(key, body) for key, body, _ in ledger.seen] == [(f"price-{SECURITY}", PRICE)]
    assert [(row.status, row.attempts, row.ended_at) for row in warehouse.outbox] == [("done", 2, AT)]
    assert (relayed.accepted, relayed.retried, relayed.refused, relayed.expired, relayed.failed) == (1, 0, [], [], [])
    assert relay.failure([relayed], "prices") is None


def test_the_relay_makes_a_call_not_accepted_yet_again_after_twice_the_last_wait() -> None:
    warehouse = _warehouse(_row())
    ledger = Ledger(warehouse, status=HTTPStatus.SERVICE_UNAVAILABLE)
    [relayed] = _relay(warehouse, ledger)
    assert [(row.status, row.attempts, row.next_at) for row in warehouse.outbox] == [("pending", 2, AT + timedelta(seconds=120))]
    assert (relayed.accepted, relayed.retried) == (0, 1)
    assert relay.failure([relayed], "prices") is None


def test_a_call_its_service_refuses_for_good_is_made_no_more_and_fails_the_run() -> None:
    warehouse = _warehouse(_row())
    ledger = Ledger(warehouse, status=HTTPStatus.NOT_FOUND)
    found = _relay(warehouse, ledger)
    assert [(row.status, row.attempts, row.next_at, row.ended_at, row.last_error) for row in warehouse.outbox] == [("refused", 2, None, AT, "ledger answered 404 to POST /bookings")]
    failed = relay.failure(found, "prices")
    assert failed is not None
    assert failed[0] == (
        f"service prices's outbox has a call refused for good by its service, and made no more: POST /bookings to ledger with Idempotency-Key price-{SECURITY}"
        " (route book, row 0190f1a2-0000-7000-8000-000000000001), in warehouse's prices_outbox, refused with: ledger answered 404 to POST /bookings"
    )


def test_the_relay_reports_a_call_refused_after_its_commit_once() -> None:
    refused = _row(status="refused", ended_at=AT, last_error="ledger answered 422 to POST /bookings")
    warehouse = _warehouse(refused)
    ledger = Ledger(warehouse)
    first, again = _relay(warehouse, ledger), _relay(warehouse, ledger)
    assert (ledger.seen, [row.next_at for row in warehouse.outbox]) == ([], [None])
    reported = relay.failure(first, "prices")
    assert reported is not None
    assert "refused with: ledger answered 422 to POST /bookings" in reported[0]
    assert relay.failure(again, "prices") is None


def test_the_relay_leaves_a_call_that_is_not_due() -> None:
    warehouse = _warehouse(_row(next_at=AT + timedelta(seconds=1)))
    ledger = Ledger(warehouse)
    [relayed] = _relay(warehouse, ledger)
    assert (ledger.seen, warehouse.outbox, relayed.accepted) == ([], [_row(next_at=AT + timedelta(seconds=1))], 0)


@pytest.mark.parametrize("status", ["done", "expired"])
def test_the_relay_never_makes_a_call_that_has_ended(status: str) -> None:
    ended = _row(status=status, next_at=None, ended_at=AT)
    ledger = Ledger(warehouse := _warehouse(ended))
    _relay(warehouse, ledger)
    assert (ledger.seen, warehouse.outbox) == ([], [ended])


def test_a_call_that_has_aged_out_is_made_no_more_and_fails_the_run(logged: Records) -> None:
    warehouse = _warehouse(_row(expires_at=AT))
    ledger = Ledger(warehouse)
    found = _relay(warehouse, ledger)
    assert ledger.seen == []
    assert [(row.status, row.next_at, row.ended_at) for row in warehouse.outbox] == [("expired", None, AT)]
    failed = relay.failure(found, "prices")
    assert failed is not None
    reason, fix = failed
    assert reason == (
        f"service prices's outbox has a call aged out, and made no more: POST /bookings to ledger with Idempotency-Key price-{SECURITY}"
        " (route book, row 0190f1a2-0000-7000-8000-000000000001), in warehouse's prices_outbox, aged out after 1 attempts, the last refused with: ledger answered 503 to POST /bookings"
    )
    assert "set its row's status to 'pending'" in fix
    assert [record["idempotency_key"] for record in logged() if record["level"] == "error"] == [f"price-{SECURITY}"]


def test_the_relay_takes_calls_in_batches_until_none_is_due() -> None:
    rows = [_row(id=f"0190f1a2-0000-7000-8000-{index:012d}", idempotency_key=f"price-{index}") for index in range(relay.BATCH + 5)]
    warehouse = _warehouse(*rows)
    ledger = Ledger(warehouse)
    [relayed] = _relay(warehouse, ledger)
    assert (relayed.accepted, len(ledger.seen), {row.status for row in warehouse.outbox}) == (relay.BATCH + 5, relay.BATCH + 5, {"done"})


def test_a_call_to_a_service_the_api_no_longer_declares_fails_its_attempt() -> None:
    warehouse = _warehouse(_row(service="payments"))
    [relayed] = _relay(warehouse, Ledger(warehouse))
    [row] = warehouse.outbox
    assert (relayed.retried, row.status, row.last_error) == (1, "pending", "the service declares no api.http('payments') to make it with")


def test_a_defect_in_one_call_stops_only_its_own_row(logged: Records) -> None:
    broken, fine = _row(idempotency_key="broken"), _row(id="0190f1a2-0000-7000-8000-000000000002", idempotency_key="fine")
    warehouse = _warehouse(broken, fine)
    ledger = FastAPI()

    @ledger.post("/bookings")
    async def bookings(request: Request) -> Response:
        if request.headers.get("idempotency-key") == "broken":
            msg = "the ledger broke"
            raise RuntimeError(msg)
        return Response(status_code=HTTPStatus.CREATED)

    [relayed] = _relay(warehouse, ledger)
    assert [(row.idempotency_key, row.status, row.attempts) for row in warehouse.outbox] == [("broken", "pending", 2), ("fine", "done", 2)]
    assert (relayed.accepted, relayed.retried, [(row.idempotency_key if row else None, error) for row, error in relayed.failed]) == (1, 1, [("broken", "RuntimeError: the ledger broke")])
    failed = relay.failure([relayed], "prices")
    assert failed is not None
    assert "a call stopped by a failure, which the log holds: POST /bookings to ledger with Idempotency-Key broken" in failed[0]
    [defect] = [record for record in logged() if record["level"] == "error"]
    assert (defect["idempotency_key"], defect["error"], "Traceback" in str(defect["traceback"])) == ("broken", "RuntimeError: the ledger broke", True)


class _Faltering(fakes.Database):
    """A fake database whose outbox fails each change of the row `failing`, as a statement past its deadline would."""

    def __init__(self, *rows: Row, failing: str) -> None:
        super().__init__(outbox=rows)
        self.failing = failing
        self.opened = 0

    @override
    def transaction(self, *, writes: bool) -> fakes.Transaction:
        self.opened += 1
        return _FalteringTransaction(self, writes=writes)


class _FalteringTransaction(fakes.Transaction):
    @override
    def outbox(self) -> fakes.Outbox:
        return _FalteringOutbox(self)


class _FalteringOutbox(fakes.Outbox):
    @override
    def update(self, row_id: str, *, was: str, seen: int, to: Mapping[str, object], timeout_s: float) -> bool:
        database = self.transaction.database
        if isinstance(database, _Faltering) and row_id == database.failing:
            msg = "the statement was cancelled"
            raise TimeoutError(msg)
        return super().update(row_id, was=was, seen=seen, to=to, timeout_s=timeout_s)


def test_a_statement_that_fails_on_one_row_stops_only_that_row_and_the_connection_is_opened_anew() -> None:
    failing, fine = _row(idempotency_key="failing"), _row(id="0190f1a2-0000-7000-8000-000000000002", idempotency_key="fine")
    warehouse = _Faltering(failing, fine, failing=failing.id)
    ledger = Ledger(warehouse)
    [relayed] = _relay(warehouse, ledger)
    assert [(row.idempotency_key, row.status) for row in warehouse.outbox] == [("failing", "pending"), ("fine", "done")]
    assert ([row.idempotency_key if row else None for row, _ in relayed.failed], relayed.accepted, warehouse.opened) == (["failing"], 1, 2)


SLOW = api.database("slow", deadline_s=0.2, max_concurrent=1, outbox=api.outbox("slow_outbox", max_age_s=3600, backoff_s=60, max_backoff_s=600))


class _Slow(fakes.Database):
    @override
    def transaction(self, *, writes: bool) -> fakes.Transaction:
        return _SlowTransaction(self, writes=writes)


class _SlowTransaction(fakes.Transaction):
    @override
    def outbox(self) -> fakes.Outbox:
        return _SlowOutbox(self)


class _SlowOutbox(fakes.Outbox):
    @override
    def due(self, now: datetime, limit: int, *, timeout_s: float) -> list[Row]:
        time.sleep(0.5)  # a query the database's deadline stops waiting for
        return super().due(now, limit, timeout_s=timeout_s)


def test_each_of_the_relays_statements_has_a_span_and_the_databases_deadline() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    warehouse = _warehouse(_row())
    _relay(warehouse, Ledger(warehouse), tracer=provider.get_tracer("test"))
    assert [span.name for span in exporter.get_finished_spans() if span.name.startswith("warehouse")] == [
        "warehouse open", "warehouse outbox due", "warehouse outbox claim", "warehouse commit", "warehouse outbox record", "warehouse commit", "warehouse outbox due",
    ]  # fmt: skip

    async def write(price: Price, slow: Annotated[api.Writer, api.given(SLOW)]) -> Price:
        del slow
        return price

    slowly = api.Service(name="prices", settings=api.Settings, resources=[SLOW, LEDGER], routes=[_post("/w", write)])
    [relayed] = _relay(_Slow(outbox=[_row()]), FastAPI(), service=slowly)  # the run waits for the query to end before it lets the connection go
    assert relayed.failed == [(None, "DeadlineExceededError: its outbox's due in database slow did not finish within its deadline of 0.2s")]


def test_the_relay_needs_an_outbox_and_the_settings_of_what_it_reaches() -> None:
    bare = api.Service(name="bare", settings=api.Settings, resources=[PLAIN, LEDGER], routes=[_post("/direct", book_directly)])
    with pytest.raises(api.ServiceError, match="service bare keeps no outbox, so basis relay has no calls to make"):
        relay.plan(bare, api.Settings.given(), {})
    with pytest.raises(api.SettingsError, match="set BASIS_DATABASES__WAREHOUSE__KIND and the rest of its block, for the database warehouse; BASIS_HTTP__LEDGER__URL"):
        relay.plan(SERVICE, api.Settings.given(), {})
    with pytest.raises(api.ServiceError, match="a fake is given for plain, which basis relay does not reach"):
        relay.plan(SERVICE, api.Settings.given(), {PLAIN: fakes.Database()})


# The commands


SERVICE_MODULE = """
from basis import api

WAREHOUSE = api.database("warehouse", deadline_s=1, max_concurrent=4, outbox=api.outbox("ops.prices_outbox", max_age_s=3600, backoff_s=60, max_backoff_s=600))
LEDGER = api.http("ledger", deadline_s=1)


def health() -> None: ...


service = api.Service(name="prices", settings=api.Settings, resources=[WAREHOUSE, LEDGER], routes=[api.get("/up", health, deadline_s=1, response=None, errors=[], status=204)])
"""


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logging_restored: None) -> Path:
    """An API repo whose service keeps an outbox, as the module `outboxed_service`, and no BASIS_ variable of the
    developer's; the logging `basis` configures is undone after."""
    del logging_restored
    for name in [name for name in os.environ if name.startswith("BASIS_")]:
        monkeypatch.delenv(name)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "prices-api"\nversion = "0.1.0"\n\n[tool.basis]\nparts = ["api"]\n', encoding="utf-8")
    (tmp_path / "outboxed_service.py").write_text(textwrap.dedent(SERVICE_MODULE), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "outboxed_service", raising=False)
    return tmp_path


def _basis(repo: Path, *argv: str) -> int:
    return command.run(argv, cwd=repo, installed=command.installed())


def test_ddl_prints_the_outbox_table_and_its_index(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _basis(repo, "ddl", "outboxed_service", "--dialect", "oracle") == 0
    printed = capsys.readouterr().out
    assert printed.startswith("CREATE TABLE ops.prices_outbox (\n    id VARCHAR2(36 CHAR) NOT NULL,\n")
    assert "    idempotency_key VARCHAR2(255 CHAR) NOT NULL,\n" in printed
    assert "    body CLOB,  -- no MaxLength in the row: unbounded\n" in printed
    assert "    next_at TIMESTAMP(6),  -- naive, in UTC\n" in printed
    assert printed.endswith("    CONSTRAINT pk_prices_outbox PRIMARY KEY (id)\n);\nCREATE INDEX ops.ix_prices_outbox_due ON ops.prices_outbox (next_at);\n")


@pytest.mark.parametrize("dialect", ["mssql", "oracle"])
def test_the_outbox_table_has_a_column_for_each_of_a_rows_fields_in_order(dialect: str) -> None:
    printed = outboxes.ddl("warehouse", "prices_outbox", "mssql" if dialect == "mssql" else "oracle")
    assert tuple(re.findall(r"^    ([a-z_]+) (?!KEY)", printed, re.MULTILINE)) == tuple(Row.model_fields)


UNBOXED = api.Service(name="unboxed", settings=api.Settings, resources=[PLAIN, LEDGER], routes=[_post("/direct", book_directly)])
"""A service whose one route calls another service directly, since it writes no database, so it keeps no outbox."""


def test_ddl_of_a_service_without_an_outbox_is_one_error_line(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _basis(repo, "ddl", f"{__name__}:UNBOXED", "--dialect", "mssql") == 1
    assert capsys.readouterr().err.splitlines()[-1].startswith("ERROR: service unboxed keeps no outbox, so it has no table to create: declare one where a route sends a call")


def test_ddl_prints_the_examples_outbox(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _basis(repo, "ddl", "basis.api.example.service", "--dialect", "mssql") == 0
    assert capsys.readouterr().out.startswith("CREATE TABLE accruals_outbox (\n")


def test_basis_lists_relay_and_ddl_in_a_repo_with_an_api(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _basis(repo, "--help") == 0
    out = capsys.readouterr().out
    assert re.search(r"^  relay +make the calls the API's outboxes still hold", out, re.MULTILINE)
    assert re.search(r"^  ddl +print the CREATE TABLE of each outbox", out, re.MULTILINE)


def test_relay_without_its_settings_is_one_error_line_after_its_record(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _basis(repo, "relay", "outboxed_service") == 1
    err = capsys.readouterr().err.splitlines()
    assert json.loads(err[-2])["message"] == "finished relay"
    assert err[-1].startswith("ERROR: service prices's outboxes and services are not all configured, so basis relay cannot run: set BASIS_DATABASES__WAREHOUSE__KIND")
