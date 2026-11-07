from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from http import HTTPStatus
from uuid import UUID

import pytest
from basis import api
from basis.api import fakes
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from basis.api.example import service
from basis.api.example.service import BOND, RECORD, Bond, Convention, Matured, NotIssued, Settings, day_count, last_coupon

PERIOD = {"face": "1000000", "rate": "0.045", "convention": "30/360"}
ISIN = "US912810TM64"
UNPRICED = "US912810TN48"
TERMS = {"security_id": ISIN, "coupon_rate": Decimal("0.045"), "convention": "30/360", "frequency": 2, "issued": date(2024, 11, 15), "maturity": date(2034, 11, 15)}
AT = datetime(2026, 11, 6, 14, 30, tzinfo=UTC)


def _bonds(binds: Mapping[str, api.Bind]) -> list[Mapping[str, object]]:
    return [TERMS | {"security_id": binds["security"]}] if binds["security"] in {ISIN, UNPRICED} else []


def _pricing() -> FastAPI:
    """The pricing service, as far as the accruals service calls it."""
    pricing = FastAPI()

    @pricing.get("/prices/{security}")
    def price(security: str) -> dict[str, str]:
        if security != ISIN:
            raise HTTPException(HTTPStatus.NOT_FOUND)
        return {"security_id": security, "clean": "98.50", "as_of": "2026-11-06"}

    return pricing


@pytest.fixture
def warehouse() -> fakes.Database:
    return fakes.Database(answers={BOND: fakes.Respond(_bonds), RECORD: 1})


@pytest.fixture
def terms() -> fakes.Cache:
    return fakes.Cache()


def _app(warehouse: fakes.Database, terms: fakes.Cache, **settings: object) -> api.App:
    given: dict[api.Resource, fakes.Fake] = {service.WAREHOUSE: warehouse, service.PRICING: fakes.Http(_pricing()), service.TERMS: terms, service.CLOCK: fakes.Clock(at=AT), service.IDS: fakes.Ids()}
    return api.build(service.service, settings=Settings.given(**settings), fakes=given)


@pytest.fixture
def client(warehouse: fakes.Database, terms: fakes.Cache) -> Iterator[TestClient]:
    with TestClient(_app(warehouse, terms).asgi) as started:
        yield started


@pytest.mark.parametrize(
    ("start", "end", "convention", "expected"),
    [
        (date(2026, 1, 15), date(2026, 7, 15), "30/360", (180, 360)),
        (date(2026, 1, 31), date(2026, 2, 28), "30/360", (28, 360)),
        (date(2026, 1, 30), date(2026, 3, 31), "30/360", (60, 360)),
        (date(2026, 1, 15), date(2026, 3, 31), "30/360", (76, 360)),
        (date(2026, 1, 15), date(2026, 7, 15), "ACT/360", (181, 360)),
        (date(2026, 1, 15), date(2026, 7, 15), "ACT/365F", (181, 365)),
        (date(2026, 1, 15), date(2026, 1, 15), "ACT/365F", (0, 365)),
    ],
    ids=["30/360", "30/360-31st-start", "30/360-31st-end-after-30th", "30/360-31st-end-after-15th", "act/360", "act/365f", "empty"],
)
def test_day_count(start: date, end: date, convention: Convention, expected: tuple[int, int]) -> None:
    assert day_count(start, end, convention) == expected


@pytest.mark.parametrize(
    ("maturity", "frequency", "settle", "expected"),
    [
        (date(2034, 11, 15), 2, date(2026, 11, 6), date(2026, 5, 15)),
        (date(2034, 11, 15), 2, date(2026, 11, 15), date(2026, 11, 15)),
        (date(2034, 11, 15), 2, date(2034, 11, 14), date(2034, 5, 15)),
        (date(2034, 8, 31), 2, date(2027, 3, 15), date(2027, 2, 28)),
        (date(2034, 8, 31), 2, date(2028, 3, 15), date(2028, 2, 29)),
        (date(2034, 11, 15), 12, date(2026, 11, 6), date(2026, 10, 15)),
        (date(2034, 11, 15), 1, date(2026, 11, 6), date(2025, 11, 15)),
        (date(2034, 11, 15), 2, date(2025, 3, 1), date(2025, 1, 10)),
    ],
    ids=["semiannual", "on-a-coupon-date", "last-period", "month-end", "month-end-in-a-leap-year", "monthly", "annual", "first-period-from-its-issue"],
)
def test_last_coupon(maturity: date, frequency: int, settle: date, expected: date) -> None:
    bond = Bond.model_validate(TERMS | {"issued": date(2025, 1, 10) if settle.year == 2025 else TERMS["issued"], "maturity": maturity, "frequency": frequency})
    assert last_coupon(bond, settle) == expected


@pytest.mark.parametrize(("settle", "refused"), [(date(2034, 11, 15), Matured), (date(2024, 11, 14), NotIssued), (date(1, 1, 1), NotIssued)], ids=["at-maturity", "before-issue", "the-first-day"])
def test_a_settlement_outside_the_bonds_life_has_no_last_coupon(settle: date, refused: type[api.Error]) -> None:
    with pytest.raises(refused):
        last_coupon(Bond.model_validate(TERMS), settle)


@pytest.mark.parametrize(("path", "settle"), [(f"/bonds/{ISIN}/dirty-price", "0001-01-01"), ("/settlements", "1900-01-01")], ids=["price", "settlement"])
def test_a_settlement_before_the_bond_is_issued_is_refused(client: TestClient, warehouse: fakes.Database, path: str, settle: str) -> None:
    holding = {"face": "100.00", "settle": settle}
    response = client.get(path, params=holding) if path.startswith("/bonds") else client.post(path, json=holding | {"security_id": ISIN})
    assert (response.status_code, response.json()["type"]) == (HTTPStatus.UNPROCESSABLE_CONTENT, "not-issued")
    assert warehouse.committed == []


def test_accrued_interest(client: TestClient) -> None:
    response = client.get("/accrued-interest", params=PERIOD | {"start": "2026-05-15", "end": "2026-11-15"})
    assert response.json() == {"start": "2026-05-15", "end": "2026-11-15", "convention": "30/360", "days": 180, "year_days": 360, "interest": "22500.00"}


def test_interest_rounds_half_to_even(client: TestClient) -> None:
    exact_half_cent = {"face": "1.8", "rate": "0.1", "convention": "30/360", "start": "2026-01-01", "end": "2026-01-11"}  # 0.005
    assert client.get("/accrued-interest", params=exact_half_cent).json()["interest"] == "0.00"


@pytest.mark.parametrize(
    ("query", "kind"),
    [
        ({"start": "2026-11-15", "end": "2026-05-15"}, "period-reversed"),
        ({"start": "2016-01-01", "end": "2026-11-15"}, "period-too-long"),
        ({"start": "2026-05-15", "end": "2026-11-15", "settle": "2026-11-17"}, "invalid-request"),
        ({"start": "2026-05-15", "end": "2026-11-15", "rate": "4.5"}, "invalid-request"),
    ],
    ids=["reversed", "too-long", "unknown-parameter", "rate-as-a-percentage"],
)
def test_what_it_refuses(client: TestClient, query: dict[str, str], kind: str) -> None:
    response = client.get("/accrued-interest", params=PERIOD | query)
    assert (response.status_code, response.json()["type"]) == (HTTPStatus.UNPROCESSABLE_CONTENT, kind)


def test_the_longest_period_is_a_setting(warehouse: fakes.Database, terms: fakes.Cache) -> None:
    with TestClient(_app(warehouse, terms, max_days=30).asgi) as client:
        response = client.get("/accrued-interest", params=PERIOD | {"start": "2026-05-15", "end": "2026-07-15"})
    assert response.json()["detail"] == "2026-05-15 to 2026-07-15 is 61 days, and this service accrues over 30 at most"


def test_conventions(client: TestClient) -> None:
    assert client.get("/conventions").json() == [{"convention": "30/360", "year_days": 360}, {"convention": "ACT/360", "year_days": 360}, {"convention": "ACT/365F", "year_days": 365}]


def test_a_bonds_terms_come_from_the_warehouse(client: TestClient) -> None:
    response = client.get(f"/bonds/{ISIN}")
    assert response.json() == {"security_id": ISIN, "coupon_rate": "0.045", "convention": "30/360", "frequency": 2, "issued": "2024-11-15", "maturity": "2034-11-15"}


@pytest.mark.parametrize(
    ("security", "status", "kind"), [("US0000000000", HTTPStatus.NOT_FOUND, "unknown-bond"), ("us912810tm64", HTTPStatus.UNPROCESSABLE_CONTENT, "invalid-request")], ids=["unknown", "not-an-isin"]
)
def test_a_bond_it_cannot_find(client: TestClient, security: str, status: int, kind: str) -> None:
    response = client.get(f"/bonds/{security}")
    assert (response.status_code, response.json()["type"]) == (status, kind)


def test_a_dirty_price_is_the_clean_price_and_the_interest_since_the_last_coupon(client: TestClient) -> None:
    response = client.get(f"/bonds/{ISIN}/dirty-price", params={"face": "1000000", "settle": "2026-11-06"})
    # 30/360 from the coupon of 2026-05-15 to 2026-11-06 is 171 days: 1,000,000 x 4.5% x 171/360 = 21,375.00
    assert response.json() == {"security_id": ISIN, "settle": "2026-11-06", "face": "1000000", "clean": "98.50", "accrued": "21375.00", "dirty": "1006375.00"}


def test_a_bonds_terms_are_read_once_until_they_expire(warehouse: fakes.Database, terms: fakes.Cache) -> None:
    read: list[Mapping[str, api.Bind]] = []
    warehouse.answers[BOND] = fakes.Respond(lambda binds: read.append(binds) or _bonds(binds))
    with TestClient(_app(warehouse, terms).asgi) as client:
        for _ in range(2):
            client.get(f"/bonds/{ISIN}/dirty-price", params={"face": "100", "settle": "2026-11-06"}).raise_for_status()
        assert len(read) == 1
        terms.advance(300)
        client.get(f"/bonds/{ISIN}/dirty-price", params={"face": "100", "settle": "2026-11-06"}).raise_for_status()
    assert len(read) == 2


def test_a_bond_the_pricing_service_does_not_price(client: TestClient) -> None:
    response = client.get(f"/bonds/{UNPRICED}/dirty-price", params={"face": "100", "settle": "2026-11-06"})
    assert (response.status_code, response.json()["type"]) == (HTTPStatus.NOT_FOUND, "unpriced")


def test_a_settlement_is_recorded_with_a_new_id_and_the_time(client: TestClient, warehouse: fakes.Database) -> None:
    response = client.post("/settlements", json={"security_id": ISIN, "face": "1000000.00", "settle": "2026-11-06"})
    settlement_id = "00000000-0000-7000-8000-000000000001"
    assert (response.status_code, response.json()) == (
        HTTPStatus.CREATED,
        {"settlement_id": settlement_id, "security_id": ISIN, "face": "1000000.00", "settle": "2026-11-06", "accrued": "21375.00", "recorded_at": "2026-11-06T14:30:00Z"},
    )
    binds = {"settlement_id": UUID(settlement_id), "security": ISIN, "face": Decimal("1000000.00"), "settle": date(2026, 11, 6), "accrued": Decimal("21375.00"), "recorded_at": AT}
    assert warehouse.committed == [fakes.Statement(RECORD, binds)]


def test_a_settlement_on_a_bond_that_has_matured_records_nothing(client: TestClient, warehouse: fakes.Database) -> None:
    response = client.post("/settlements", json={"security_id": ISIN, "face": "100.00", "settle": "2035-01-02"})
    assert (response.status_code, response.json()["type"]) == (HTTPStatus.UNPROCESSABLE_CONTENT, "matured")
    assert (warehouse.committed, warehouse.rolled_back) == ([], [])
