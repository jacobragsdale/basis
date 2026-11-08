"""The example's routes, on fakes: each answers what its handler makes of its request and of what its resources answer,
and each error a route declares, its own and the kinds of service error basis adds for what it reaches, answers its
problem. A resource's failure is made with its fake's `fails`, as a real one would fail."""

from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime
from http import HTTPStatus
from uuid import UUID

import httpx2  # the responses TestClient gives
import pyarrow as pa
import pytest
from basis import api
from basis.api import fakes
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from basis.api.example import dev, resources, service
from basis.api.example.repository import BOND, RECORD
from basis.api.example.resources import Settings

PERIOD = {"face": 1000000, "rate": 0.045, "convention": "30/360"}
ISIN = "US912810TM64"
UNPRICED = "US912810TN48"
UNSOLVABLE = "US912810TP95"
"""A bond the pricing service quotes at a price no yield from -99% to 1000% a year gives."""
TERMS = {"security_id": ISIN, "coupon_rate": 0.045, "convention": "30/360", "frequency": 2, "issued": date(2024, 11, 15), "maturity": date(2034, 11, 15)}
AT = datetime(2026, 11, 6, 14, 30, tzinfo=UTC)
ARROW = "application/vnd.apache.arrow.stream"
HOLDING = {"face": 100, "settle": "2026-11-06"}
QUERIES = {"dirty-price": HOLDING, "yield": {"settle": "2026-11-06"}}
"""What each route that prices a bond reads, by its path's end."""
SETTLEMENT = {"security_id": ISIN, "face": 1000000.0, "settle": "2026-11-06"}


def _bonds(binds: Mapping[str, api.Bind]) -> list[Mapping[str, object]]:
    return [TERMS | {"security_id": binds["security"]}] if binds["security"] in {ISIN, UNPRICED, UNSOLVABLE} else []


class Ledger:
    """The ledger service, which takes every booking, with its `Idempotency-Key`."""

    def __init__(self) -> None:
        self.booked: list[tuple[str | None, dict[str, object]]] = []
        self.app = FastAPI()

        @self.app.post("/bookings", status_code=HTTPStatus.CREATED)
        async def book(request: Request) -> None:
            body: object = await request.json()
            self.booked.append((request.headers.get("idempotency-key"), {str(key): value for key, value in body.items()} if isinstance(body, dict) else {}))


def _pricing() -> FastAPI:
    """The pricing service, as far as the accruals service calls it."""
    pricing = FastAPI()

    @pricing.get("/prices/{security}")
    def price(security: str) -> dict[str, object]:
        clean = {ISIN: 98.5, UNSOLVABLE: 100000000.0}.get(security)
        if clean is None:
            raise HTTPException(HTTPStatus.NOT_FOUND)
        return {"security_id": security, "clean": clean, "as_of": "2026-11-06"}

    return pricing


@pytest.fixture
def warehouse() -> fakes.Database:
    return fakes.Database(answers={BOND: fakes.Respond(_bonds), RECORD: 1})


@pytest.fixture
def pricing() -> fakes.Http:
    return fakes.Http(_pricing())


@pytest.fixture
def ledger() -> Ledger:
    return Ledger()


@pytest.fixture
def terms() -> fakes.Cache:
    return fakes.Cache()


@pytest.fixture
def app(warehouse: fakes.Database, pricing: fakes.Http, ledger: Ledger, terms: fakes.Cache) -> api.App:
    return built(warehouse, pricing, ledger, terms)


def built(warehouse: fakes.Database, pricing: fakes.Http, ledger: Ledger, terms: fakes.Cache, **settings: object) -> api.App:
    given: fakes.Fakes = {
        resources.WAREHOUSE: warehouse, resources.PRICING: pricing, resources.LEDGER: fakes.Http(ledger.app), resources.TERMS: terms, resources.CLOCK: fakes.Clock(at=AT),
        resources.IDS: fakes.Ids(), resources.SOLVE: fakes.Compute(),
    }  # fmt: skip
    return api.build(service.service, settings=Settings.given(**settings), fakes=given)


@pytest.fixture
def client(app: api.App) -> Iterator[TestClient]:
    with TestClient(app.asgi) as started:
        yield started


def kind(response: httpx2.Response) -> tuple[int, str]:
    """A problem's status and type."""
    return response.status_code, str(response.json()["type"])


# Accrued interest, and the conventions


def test_accrued_interest(client: TestClient) -> None:
    response = client.get("/accrued-interest", params=PERIOD | {"start": "2026-05-15", "end": "2026-11-15"})
    assert response.json() == pytest.approx({"start": "2026-05-15", "end": "2026-11-15", "convention": "30/360", "days": 180, "year_days": 360, "interest": 22500.0})


@pytest.mark.parametrize(
    ("query", "problem"),
    [
        ({"start": "2026-11-15", "end": "2026-05-15"}, "period-reversed"),
        ({"start": "2016-01-01", "end": "2026-11-15"}, "period-too-long"),
        ({"start": "2026-05-15", "end": "2026-11-15", "settle": "2026-11-17"}, "invalid-request"),
        ({"start": "2026-05-15", "end": "2026-11-15", "rate": 4.5}, "invalid-request"),
    ],
    ids=["reversed", "too-long", "unknown-parameter", "rate-as-a-percentage"],
)
def test_what_accrued_interest_refuses(client: TestClient, query: dict[str, str], problem: str) -> None:
    assert kind(client.get("/accrued-interest", params=PERIOD | query)) == (HTTPStatus.UNPROCESSABLE_CONTENT, problem)


def test_a_period_that_ends_before_it_starts_names_its_end(client: TestClient) -> None:
    response = client.get("/accrued-interest", params=PERIOD | {"start": "2026-11-15", "end": "2026-05-15"})
    assert response.json()["errors"] == [{"detail": "is before the start, 2026-11-15", "parameter": "end"}]


def test_the_longest_period_is_a_setting(warehouse: fakes.Database, pricing: fakes.Http, ledger: Ledger, terms: fakes.Cache) -> None:
    with TestClient(built(warehouse, pricing, ledger, terms, max_days=30).asgi) as client:
        response = client.get("/accrued-interest", params=PERIOD | {"start": "2026-05-15", "end": "2026-07-15"})
    assert response.json()["detail"] == "2026-05-15 to 2026-07-15 is 61 days, and this service accrues over 30 at most"


def test_conventions(client: TestClient) -> None:
    assert client.get("/conventions").json() == [{"convention": "30/360", "year_days": 360}, {"convention": "ACT/360", "year_days": 360}, {"convention": "ACT/365F", "year_days": 365}]


# A bond's terms


def test_a_bonds_terms_come_from_the_warehouse(client: TestClient) -> None:
    response = client.get(f"/bonds/{ISIN}")
    assert response.json() == {"security_id": ISIN, "coupon_rate": 0.045, "convention": "30/360", "frequency": 2, "issued": "2024-11-15", "maturity": "2034-11-15"}


@pytest.mark.parametrize(
    ("security", "problem"), [("US0000000000", (HTTPStatus.NOT_FOUND, "unknown-bond")), ("us912810tm64", (HTTPStatus.UNPROCESSABLE_CONTENT, "invalid-request"))], ids=["unknown", "not-an-isin"]
)
def test_a_bond_it_cannot_find(client: TestClient, security: str, problem: tuple[int, str]) -> None:
    assert kind(client.get(f"/bonds/{security}")) == problem


@pytest.mark.parametrize(
    ("failure", "problem"),
    [
        (api.Timeout, (HTTPStatus.GATEWAY_TIMEOUT, "service-timeout")),
        (api.Unavailable, (HTTPStatus.SERVICE_UNAVAILABLE, "service-unavailable")),
        (api.Denied, (HTTPStatus.BAD_GATEWAY, "service-denied")),
    ],
    ids=["timeout", "unavailable", "denied"],
)
def test_a_warehouse_that_fails_answers_its_kinds_problem(warehouse: fakes.Database, client: TestClient, failure: type[api.ServiceError], problem: tuple[int, str]) -> None:
    warehouse.fails(failure, statement=BOND)
    assert kind(client.get(f"/bonds/{ISIN}")) == problem
    assert kind(client.get(f"/bonds/{ISIN}/coupons")) == problem


# A holding's dirty price


def test_a_dirty_price_is_the_clean_price_and_the_interest_since_the_last_coupon(client: TestClient) -> None:
    response = client.get(f"/bonds/{ISIN}/dirty-price", params={"face": 1000000, "settle": "2026-11-06"})
    # 30/360 from the coupon of 2026-05-15 to 2026-11-06 is 171 days: 1,000,000 x 4.5% x 171/360 = 21,375.00
    assert response.json() == pytest.approx({"security_id": ISIN, "settle": "2026-11-06", "face": 1000000.0, "clean": 98.5, "accrued": 21375.0, "dirty": 1006375.0})


def test_a_bonds_terms_are_read_once_until_they_expire(warehouse: fakes.Database, app: api.App, terms: fakes.Cache) -> None:
    read: list[Mapping[str, api.Bind]] = []
    warehouse.answers[BOND] = fakes.Respond(lambda binds: read.append(binds) or _bonds(binds))
    with TestClient(app.asgi) as client:
        for _ in range(2):
            client.get(f"/bonds/{ISIN}/dirty-price", params=HOLDING).raise_for_status()
        assert len(read) == 1
        terms.advance(300)
        client.get(f"/bonds/{ISIN}/dirty-price", params=HOLDING).raise_for_status()
    assert len(read) == 2


@pytest.mark.parametrize("route", ["dirty-price", "yield"])
def test_a_bond_the_pricing_service_does_not_price_is_unpriced(pricing: fakes.Http, client: TestClient, route: str) -> None:
    assert kind(client.get(f"/bonds/{UNPRICED}/{route}", params=QUERIES[route])) == (HTTPStatus.NOT_FOUND, "unpriced")
    pricing.fails(f"/prices/{ISIN}", api.NotFound)
    assert kind(client.get(f"/bonds/{ISIN}/{route}", params=QUERIES[route])) == (HTTPStatus.NOT_FOUND, "unpriced")


@pytest.mark.parametrize(
    ("failure", "problem"),
    [
        (api.Timeout, (HTTPStatus.GATEWAY_TIMEOUT, "service-timeout")),
        (api.Unavailable, (HTTPStatus.SERVICE_UNAVAILABLE, "service-unavailable")),
        (api.Rejected, (HTTPStatus.BAD_GATEWAY, "service-rejected")),
        (api.BadReply, (HTTPStatus.BAD_GATEWAY, "service-bad-reply")),
        (api.Failed, (HTTPStatus.BAD_GATEWAY, "service-failed")),
    ],
    ids=["timeout", "unavailable", "rejected", "bad-reply", "failed"],
)
@pytest.mark.parametrize("route", ["dirty-price", "yield"])
def test_a_pricing_service_that_fails_answers_its_kinds_problem(pricing: fakes.Http, client: TestClient, route: str, failure: type[api.ServiceError], problem: tuple[int, str]) -> None:
    pricing.fails(f"/prices/{ISIN}", failure)
    assert kind(client.get(f"/bonds/{ISIN}/{route}", params=QUERIES[route])) == problem


@pytest.mark.parametrize(("settle", "problem"), [("2034-11-15", "matured"), ("2024-11-14", "not-issued")], ids=["matured", "not-issued"])
@pytest.mark.parametrize("route", ["dirty-price", "yield"])
def test_a_settlement_outside_the_bonds_life_is_refused_before_it_is_priced(pricing: fakes.Http, client: TestClient, route: str, settle: str, problem: str) -> None:
    pricing.fails(f"/prices/{ISIN}", api.Unavailable)  # never called
    assert kind(client.get(f"/bonds/{ISIN}/{route}", params=QUERIES[route] | {"settle": settle})) == (HTTPStatus.UNPROCESSABLE_CONTENT, problem)


# A bond's yield


def test_a_bonds_yield_is_solved_from_its_dirty_price(client: TestClient) -> None:
    response = client.get(f"/bonds/{ISIN}/yield", params={"settle": "2026-11-06"})
    # 98.50 clean, and 171 days of 30/360 interest at 4.5% on 100: 100.6375 dirty, nine days of a 180-day period before
    # the first of 17 coupons of 2.25
    found = response.json()
    assert (found.pop("security_id"), found.pop("settle")) == (ISIN, "2026-11-06")
    assert found == pytest.approx({"clean": 98.5, "dirty": 100.6375, "annual_yield": 0.0472658580}, rel=1e-9)


def test_a_price_no_yield_gives_is_refused(client: TestClient) -> None:
    response = client.get(f"/bonds/{UNSOLVABLE}/yield", params={"settle": "2026-11-06"})
    assert kind(response) == (HTTPStatus.UNPROCESSABLE_CONTENT, "no-yield")
    assert response.json()["detail"].startswith("no yield from -99% to 1000% a year gives a dirty price of ")


def test_an_unknown_bond_has_no_price_and_no_yield(client: TestClient) -> None:
    for route in ("dirty-price", "yield"):
        assert kind(client.get(f"/bonds/US0000000000/{route}", params=QUERIES[route])) == (HTTPStatus.NOT_FOUND, "unknown-bond")


# A bond's coupons


def test_a_bonds_coupons_are_its_schedule(client: TestClient) -> None:
    coupons = client.get(f"/bonds/{ISIN}/coupons").json()
    # semiannual from its issue to its maturity, each 30/360 period at 4.5% on 100: 2.25
    assert (len(coupons), coupons[0], coupons[-1]) == (
        20,
        pytest.approx({"starts": "2024-11-15", "pays": "2025-05-15", "interest": 2.25}),
        pytest.approx({"starts": "2034-05-15", "pays": "2034-11-15", "interest": 2.25}),
    )


def test_a_ui_takes_the_coupons_as_arrow(client: TestClient) -> None:
    response = client.get(f"/bonds/{ISIN}/coupons", headers={"accept": ARROW})
    assert (response.headers["content-type"], response.headers["vary"]) == (ARROW, "Accept")
    table = pa.ipc.open_stream(response.content).read_all()
    assert table.schema == pa.schema([pa.field("starts", pa.date32(), nullable=False), pa.field("pays", pa.date32(), nullable=False), pa.field("interest", pa.float64(), nullable=True)])
    as_json = client.get(f"/bonds/{ISIN}/coupons").json()
    assert table.to_pylist() == [{"starts": date.fromisoformat(row["starts"]), "pays": date.fromisoformat(row["pays"]), "interest": float(row["interest"])} for row in as_json]


def test_a_bond_it_has_no_coupons_for_is_a_problem_whatever_was_asked_for(client: TestClient) -> None:
    response = client.get("/bonds/US0000000000/coupons", headers={"accept": ARROW})
    assert (response.status_code, response.headers["content-type"], response.json()["type"]) == (HTTPStatus.NOT_FOUND, "application/problem+json", "unknown-bond")


# Settlements


def test_a_settlement_is_recorded_with_a_new_id_and_the_time(client: TestClient, warehouse: fakes.Database) -> None:
    response = client.post("/settlements", json=SETTLEMENT)
    settlement_id = "00000000-0000-7000-8000-000000000001"
    assert (response.status_code, response.json()) == (
        HTTPStatus.CREATED,
        pytest.approx({"settlement_id": settlement_id, "security_id": ISIN, "face": 1000000.0, "settle": "2026-11-06", "accrued": 21375.0, "recorded_at": "2026-11-06T14:30:00Z"}),
    )
    binds = {"settlement_id": UUID(settlement_id), "security": ISIN, "face": 1000000.00, "settle": date(2026, 11, 6), "accrued": 21375.00, "recorded_at": AT}
    assert warehouse.committed == [fakes.Statement(RECORD, binds)]


def test_a_settlement_is_booked_with_the_ledger_by_its_id_once_its_row_commits(client: TestClient, warehouse: fakes.Database, ledger: Ledger) -> None:
    settlement = client.post("/settlements", json=SETTLEMENT).json()
    [row] = warehouse.outbox
    assert (row.service, row.method, row.path, row.idempotency_key, row.status) == ("ledger", "POST", "/bookings", settlement["settlement_id"], "done")
    assert [(settlement["settlement_id"], settlement)] == ledger.booked


@pytest.mark.parametrize(
    ("settlement", "problem"),
    [
        (SETTLEMENT | {"settle": "2035-01-02"}, (HTTPStatus.UNPROCESSABLE_CONTENT, "matured")),
        (SETTLEMENT | {"settle": "1900-01-01"}, (HTTPStatus.UNPROCESSABLE_CONTENT, "not-issued")),
        (SETTLEMENT | {"security_id": "US0000000000"}, (HTTPStatus.NOT_FOUND, "unknown-bond")),
    ],
    ids=["matured", "not-issued", "unknown-bond"],
)
def test_a_settlement_it_refuses_records_nothing_and_books_nothing(client: TestClient, warehouse: fakes.Database, ledger: Ledger, settlement: dict[str, str], problem: tuple[int, str]) -> None:
    assert kind(client.post("/settlements", json=settlement)) == problem
    assert (warehouse.committed, warehouse.outbox, ledger.booked) == ([], [], [])


def test_a_settlement_the_warehouse_refuses_records_nothing_and_books_nothing(client: TestClient, warehouse: fakes.Database, ledger: Ledger) -> None:
    warehouse.fails(api.Conflict, statement=RECORD)
    assert kind(client.post("/settlements", json=SETTLEMENT)) == (HTTPStatus.BAD_GATEWAY, "service-conflict")
    assert (warehouse.committed, warehouse.outbox, ledger.booked) == ([], [], [])


def test_a_settlement_is_recorded_while_the_ledger_is_down_and_its_booking_waits(warehouse: fakes.Database, ledger: Ledger) -> None:
    ledger_down = fakes.Http(ledger.app)
    ledger_down.fails("/bookings", api.Unavailable)
    given: fakes.Fakes = {
        resources.WAREHOUSE: warehouse, resources.PRICING: fakes.Http(_pricing()), resources.LEDGER: ledger_down, resources.TERMS: fakes.Cache(), resources.CLOCK: fakes.Clock(at=AT),
        resources.IDS: fakes.Ids(), resources.SOLVE: fakes.Compute(),
    }  # fmt: skip
    with TestClient(api.build(service.service, settings=Settings.given(), fakes=given).asgi) as down:
        assert down.post("/settlements", json=SETTLEMENT).status_code == HTTPStatus.CREATED
    [row] = warehouse.outbox
    assert (row.status, ledger.booked) == ("pending", [])


# The service's own fakes


def test_the_services_own_fakes_run_it() -> None:
    """What `basis dev` and `basis call --fake` run it on, with the solver run here rather than in the pool."""
    app = api.build(service.service, settings=Settings.given(), fakes=dev.fakes() | {resources.SOLVE: fakes.Compute()})
    with TestClient(app.asgi) as client:
        assert client.get("/bonds/XS2010030836").json()["convention"] == "ACT/360"
        assert client.get("/bonds/XS2010030836/yield", params={"settle": "2026-11-06"}).status_code == HTTPStatus.OK
        assert kind(client.get("/bonds/DE0001102580/dirty-price", params=HOLDING)) == (HTTPStatus.NOT_FOUND, "unpriced")
        assert kind(client.get("/bonds/US0000000000")) == (HTTPStatus.NOT_FOUND, "unknown-bond")
        assert client.post("/settlements", json={"security_id": "US912810TM64", "face": 100.0, "settle": "2026-11-06"}).status_code == HTTPStatus.CREATED
