"""`GET /bonds/{security}/next-coupon`: the coupon after the settlement date, its period's start and its interest per
100, and the problems of an unknown bond and of a date outside its life; and the route is in openapi.json."""

import json
from http import HTTPStatus
from pathlib import Path

import pytest

from quotes.tests.eval_app import ISIN, client


def test_the_next_coupon_after_a_settlement() -> None:
    with client() as started:
        response = started.get(f"/bonds/{ISIN}/next-coupon", params={"settle": "2026-11-06"})
    assert response.status_code == HTTPStatus.OK
    found = response.json()
    assert found["pays"] == "2026-11-15"
    assert found.get("starts", found.get("start")) == "2026-05-15"
    assert found["interest"] == pytest.approx(2.25)


def test_a_settlement_on_a_coupon_date_gets_the_one_after() -> None:
    with client() as started:
        found = started.get(f"/bonds/{ISIN}/next-coupon", params={"settle": "2026-11-15"}).json()
    assert found["pays"] == "2027-05-15"


@pytest.mark.parametrize(("security", "settle", "problem"), [("US0000000000", "2026-11-06", (404, "unknown-bond")), (ISIN, "2035-01-02", (422, "matured")), (ISIN, "2020-01-02", (422, "not-issued"))])
def test_its_problems(security: str, settle: str, problem: tuple[int, str]) -> None:
    with client() as started:
        response = started.get(f"/bonds/{security}/next-coupon", params={"settle": settle})
    assert (response.status_code, response.json()["type"].rsplit("/", 1)[-1]) == problem


def test_it_is_in_the_contract() -> None:
    document = json.loads((Path(__file__).parents[2] / "openapi.json").read_text(encoding="utf-8"))
    assert "/bonds/{security}/next-coupon" in document["paths"]
