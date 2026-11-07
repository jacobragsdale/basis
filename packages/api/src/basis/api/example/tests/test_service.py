from collections.abc import Iterator
from datetime import date
from http import HTTPStatus

import pytest
from basis import api
from fastapi.testclient import TestClient

from basis.api.example import service
from basis.api.example.service import Convention, Settings, day_count

BOND = {"face": "1000000", "rate": "0.045", "convention": "30/360"}


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(api.build(service.service, settings=Settings.given()).asgi) as started:
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


def test_accrued_interest(client: TestClient) -> None:
    response = client.get("/accrued-interest", params=BOND | {"start": "2026-05-15", "end": "2026-11-15"})
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
    response = client.get("/accrued-interest", params=BOND | query)
    assert (response.status_code, response.json()["type"]) == (HTTPStatus.UNPROCESSABLE_CONTENT, kind)


def test_the_longest_period_is_a_setting() -> None:
    with TestClient(api.build(service.service, settings=Settings.given(max_days=30)).asgi) as client:
        response = client.get("/accrued-interest", params=BOND | {"start": "2026-05-15", "end": "2026-07-15"})
    assert response.json()["detail"] == "2026-05-15 to 2026-07-15 is 61 days, and this service accrues over 30 at most"


def test_conventions(client: TestClient) -> None:
    assert client.get("/conventions").json() == [{"convention": "30/360", "year_days": 360}, {"convention": "ACT/360", "year_days": 360}, {"convention": "ACT/365F", "year_days": 365}]
