"""basis-core's service errors in an API, on fakes alone, with no mock: each kind a fake fails with reaches the route as
the problem the design gives it, unless the handler handles it; a route lists the kinds what it asks for can raise; a
kind nothing it asks for raises is a defect; and the outbox makes again, or refuses, a call by the kind it failed with,
as its statuses say."""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from basis import api
from basis.api import fakes
from basis.core import fakes as core_fakes
from basis.core.errors import KINDS

OUTBOX = api.outbox("prices_outbox", max_age_s=3600, backoff_s=60, max_backoff_s=600)
WAREHOUSE = api.database("warehouse", deadline_s=1, max_concurrent=4, outbox=OUTBOX)
PRICING = api.http("pricing", deadline_s=0.3)
LEDGER = api.http("ledger", deadline_s=1)
CLOCK = api.clock("clock")
SELECT = "SELECT security, price FROM prices WHERE security = %(security)s"
INSERT = "INSERT INTO prices (security, price) VALUES (%(security)s, %(price)s)"
SECURITY = "US912810TM64"
PRICE = {"security": SECURITY, "price": "101.25"}
STATUSES: dict[type[api.ServiceError], int] = {
    api.Timeout: HTTPStatus.GATEWAY_TIMEOUT, api.Unavailable: HTTPStatus.SERVICE_UNAVAILABLE, api.NotFound: HTTPStatus.BAD_GATEWAY, api.Conflict: HTTPStatus.BAD_GATEWAY,
    api.Rejected: HTTPStatus.BAD_GATEWAY, api.Denied: HTTPStatus.BAD_GATEWAY, api.BadReply: HTTPStatus.BAD_GATEWAY, api.Failed: HTTPStatus.BAD_GATEWAY,
}  # fmt: skip
"""The design's status for each kind a route does not handle."""
TYPES: dict[type[api.ServiceError], str] = {
    api.Timeout: "service-timeout", api.Unavailable: "service-unavailable", api.NotFound: "service-not-found", api.Conflict: "service-conflict",
    api.Rejected: "service-rejected", api.Denied: "service-denied", api.BadReply: "service-bad-reply", api.Failed: "service-failed",
}  # fmt: skip


class Price(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    security: str
    price: Decimal


class Unpriced(api.Error):
    status = HTTPStatus.NOT_FOUND
    title = "No price for the security"


class AlreadyRecorded(api.Error):
    status = HTTPStatus.CONFLICT
    title = "The price is recorded already"


async def quote(security: str, pricing: Annotated[api.Client, api.given(PRICING)]) -> Price:
    return await pricing.get(f"/prices/{security}", Price)


async def quote_or_unpriced(security: str, pricing: Annotated[api.Client, api.given(PRICING)]) -> Price:
    try:
        return await pricing.get(f"/prices/{security}", Price)
    except api.NotFound as missing:
        msg = f"pricing has no price for {security}"
        raise Unpriced(msg) from missing


async def stored(security: str, warehouse: Annotated[api.Reader, api.given(WAREHOUSE)]) -> list[Price]:
    return await warehouse.fetch(Price, SELECT, security=security)


async def record(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)]) -> Price:
    await warehouse.execute(INSERT, security=price.security, price=price.price)
    return price


async def record_once(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)]) -> Price:
    try:
        await warehouse.execute(INSERT, security=price.security, price=price.price)
    except api.Conflict as recorded:
        msg = f"{price.security} is recorded already"
        raise AlreadyRecorded(msg) from recorded
    return price


async def book(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)], ledger: Annotated[api.Client, api.given(LEDGER)]) -> Price:
    await warehouse.execute(INSERT, security=price.security, price=price.price)
    await ledger.send("POST", "/bookings", price, key=f"price-{price.security}")
    return price


def stray(clock: Annotated[api.Clock, api.given(CLOCK)]) -> Price:
    del clock
    msg = "pricing answered 404 to GET /prices/X"
    raise api.NotFound(msg, resource="pricing", status=HTTPStatus.NOT_FOUND)


SERVICE = api.Service(
    name="prices",
    settings=api.Settings,
    resources=[WAREHOUSE, PRICING, LEDGER, CLOCK],
    routes=[
        api.get("/quotes/{security}", quote, deadline_s=5, response=Price, errors=[]),
        api.get("/priced/{security}", quote_or_unpriced, deadline_s=5, response=Price, errors=[Unpriced]),
        api.get("/stored/{security}", stored, deadline_s=5, response=list[Price], errors=[]),
        api.post("/record", record, deadline_s=5, response=Price, errors=[]),
        api.post("/record-once", record_once, deadline_s=5, response=Price, errors=[AlreadyRecorded]),
        api.post("/book", book, deadline_s=5, response=Price, errors=[]),
        api.get("/stray", stray, deadline_s=5, response=Price, errors=[]),
    ],
)


def _pricing() -> fakes.Http:
    app = FastAPI()

    @app.get("/prices/{security}")
    def price(security: str) -> dict[str, str]:
        return {"security": security, "price": "99.50"}

    return fakes.Http(app)


def _ledger() -> fakes.Http:
    app = FastAPI()

    @app.post("/bookings", status_code=HTTPStatus.CREATED)
    def bookings() -> dict[str, str]:
        return {}

    return fakes.Http(app)


def _client(*, warehouse: fakes.Database | None = None, pricing: fakes.Http | None = None, ledger: fakes.Http | None = None) -> TestClient:
    given: fakes.Fakes = {
        WAREHOUSE: warehouse if warehouse is not None else fakes.Database(answers={SELECT: [PRICE], INSERT: 1}),
        PRICING: pricing if pricing is not None else _pricing(),
        LEDGER: ledger if ledger is not None else _ledger(),
        CLOCK: fakes.Clock(at=datetime(2026, 11, 7, tzinfo=UTC)),
    }
    return TestClient(api.build(SERVICE, settings=api.Settings.given(), fakes=given).asgi)


@pytest.mark.parametrize("kind", KINDS, ids=[kind.__name__ for kind in KINDS])
def test_each_kind_another_service_fails_with_answers_the_designs_status(kind: type[api.ServiceError]) -> None:
    pricing = _pricing()
    pricing.fails(f"/prices/{SECURITY}", kind)
    with _client(pricing=pricing) as client:
        response = client.get(f"/quotes/{SECURITY}")
    found = response.json()
    assert (response.status_code, found["type"], response.headers["content-type"]) == (STATUSES[kind], TYPES[kind], "application/problem+json")
    assert found["detail"].startswith(f"pricing answered {core_fakes.ANSWERS[kind]} to GET /prices/{SECURITY}" if kind is not api.BadReply else "pricing answered GET")


@pytest.mark.parametrize("kind", fakes.DATABASE_KINDS, ids=[kind.__name__ for kind in fakes.DATABASE_KINDS])
def test_each_kind_a_database_fails_with_answers_the_designs_status_and_never_its_drivers_words(kind: type[api.ServiceError]) -> None:
    warehouse = fakes.Database(answers={SELECT: [PRICE]})
    warehouse.fails(kind)
    with _client(warehouse=warehouse) as client:
        response = client.get(f"/stored/{SECURITY}")
    found = response.json()
    assert (response.status_code, found["type"]) == (STATUSES[kind], TYPES[kind])
    assert found["detail"].startswith("database warehouse")
    assert "the test made the fake database fail" not in found["detail"]  # the driver's words are the log's alone


@pytest.mark.parametrize("kind", fakes.DATABASE_KINDS, ids=[kind.__name__ for kind in fakes.DATABASE_KINDS])
def test_a_write_that_fails_with_any_kind_answers_it_and_commits_nothing(kind: type[api.ServiceError]) -> None:
    warehouse = fakes.Database(answers={INSERT: 1})
    warehouse.fails(kind, statement=INSERT)
    with _client(warehouse=warehouse) as client:
        response = client.post("/record", json=PRICE)
    assert (response.status_code, response.json()["type"], warehouse.committed) == (STATUSES[kind], TYPES[kind], [])


def test_a_kind_the_handler_handles_is_its_own_answer() -> None:
    pricing = _pricing()
    pricing.fails(f"/prices/{SECURITY}", api.NotFound)
    warehouse = fakes.Database(answers={INSERT: 1})
    warehouse.fails(api.Conflict, statement=INSERT)
    with _client(pricing=pricing, warehouse=warehouse) as client:
        priced = client.get(f"/priced/{SECURITY}")
        recorded = client.post("/record-once", json=PRICE)
    assert (priced.status_code, priced.json()["type"]) == (HTTPStatus.NOT_FOUND, "unpriced")
    assert (recorded.status_code, recorded.json()["type"], warehouse.committed) == (HTTPStatus.CONFLICT, "already-recorded", [])


def test_a_get_unavailable_once_is_made_again_and_the_route_answers() -> None:
    pricing = _pricing()
    pricing.fails(f"/prices/{SECURITY}", api.Unavailable, times=1)
    with _client(pricing=pricing) as client:
        response = client.get(f"/quotes/{SECURITY}")
    assert (response.status_code, response.json()) == (HTTPStatus.OK, {"security": SECURITY, "price": "99.50"})


def test_a_database_failure_once_fails_only_the_next_statement() -> None:
    warehouse = fakes.Database(answers={SELECT: [PRICE]})
    warehouse.fails(api.Unavailable, times=1)
    with _client(warehouse=warehouse) as client:
        first, second = client.get(f"/stored/{SECURITY}"), client.get(f"/stored/{SECURITY}")
    assert (first.status_code, second.status_code) == (HTTPStatus.SERVICE_UNAVAILABLE, HTTPStatus.OK)


def test_a_kind_nothing_the_route_asks_for_raises_is_a_defect() -> None:
    with _client() as client:
        response = client.get("/stray")
    assert (response.status_code, response.json()["type"], "detail" in response.json()) == (HTTPStatus.INTERNAL_SERVER_ERROR, "about:blank", False)


def test_each_route_lists_the_kinds_what_it_asks_for_can_raise() -> None:
    answers = {route.name: route.answers for route in SERVICE.routes}
    assert answers["quote"] == KINDS
    assert answers["record"] == (api.Timeout, api.Unavailable, api.Conflict, api.Rejected, api.Denied, api.Failed)
    assert answers["book"] == KINDS
    assert answers["stray"] == ()


@pytest.mark.parametrize(
    ("kind", "status", "attempts"),
    [
        (api.Timeout, "pending", 1), (api.Unavailable, "pending", 1), (api.Conflict, "pending", 1), (api.Failed, "pending", 1),
        (api.NotFound, "refused", 1), (api.Rejected, "refused", 1), (api.Denied, "refused", 1), (api.BadReply, "done", 1),
    ],
    ids=["timeout", "unavailable", "conflict", "failed", "not-found", "rejected", "denied", "bad-reply-unread"],
)  # fmt: skip
def test_a_call_after_its_commit_is_made_again_or_refused_by_the_kind_it_fails_with(kind: type[api.ServiceError], status: str, attempts: int) -> None:
    """A 504, a 503 and a 409 are made again, as a 500 is; a 404, a 422 and a 403 refuse the call for good; and the
    call's reply is never read, so a body that is not JSON is an accepted call."""
    ledger = _ledger()
    ledger.fails("/bookings", kind)
    warehouse = fakes.Database(answers={INSERT: 1})
    with _client(warehouse=warehouse, ledger=ledger) as client:
        response = client.post("/book", json=PRICE)
    [row] = warehouse.outbox
    assert (response.status_code, row.status, row.attempts) == (HTTPStatus.OK, status, attempts)


@pytest.mark.parametrize(
    ("fails", "message"),
    [
        (lambda database: database.fails(api.NotFound), "fails with one of the kinds of ServiceError a database raises, Timeout, Unavailable, Conflict, Rejected, Denied, Failed, not 'NotFound'"),
        (lambda database: database.fails(api.BadReply), "not 'BadReply'"),
        (lambda database: database.fails(ValueError), "not 'ValueError'"),
        (lambda database: database.fails(api.Unavailable, times=0), "a whole number of times, 1 or more"),
    ],
    ids=["not-found", "bad-reply", "not-a-kind", "no-times"],
)
def test_a_fake_database_fails_with_a_kind_a_database_raises(fails: Callable[[fakes.Database], None], message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        fails(fakes.Database())


def test_a_fake_database_fakes_one_database() -> None:
    database = fakes.Database()
    database.opened_as("warehouse")
    database.opened_as("warehouse")
    with pytest.raises(ValueError, match=r"given for both warehouse and books: give each database a fakes\.Database of its own"):
        database.opened_as("books")
