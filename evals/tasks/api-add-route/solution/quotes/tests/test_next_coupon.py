"""The next coupon, in the domain and through its route."""

from datetime import date
from http import HTTPStatus

import pytest
from basis.api import fakes
from fastapi import FastAPI
from fastapi.testclient import TestClient

from quotes import domain
from quotes.domain import Bond
from quotes.repository import BOND as SELECT_BOND
from quotes.tests.test_handlers import ISIN, TERMS, Ledger, built

BOND = Bond.model_validate(TERMS)


def test_the_next_coupon_is_the_end_of_the_settlements_period() -> None:
    found = domain.next_coupon(BOND, date(2026, 11, 6))
    assert (found.starts, found.pays) == (date(2026, 5, 15), date(2026, 11, 15))
    assert found.interest == pytest.approx(2.25)


def test_the_first_coupon_accrues_from_the_issue() -> None:
    assert domain.next_coupon(BOND.model_copy(update={"issued": date(2026, 6, 1)}), date(2026, 7, 1)).starts == date(2026, 6, 1)


def test_the_route() -> None:
    warehouse = fakes.Database(answers={SELECT_BOND: fakes.Respond(lambda _: [TERMS])})
    with TestClient(built(warehouse, fakes.Http(FastAPI()), Ledger(), fakes.Cache()).asgi) as client:
        response = client.get(f"/bonds/{ISIN}/next-coupon", params={"settle": "2026-11-06"})
    assert response.status_code == HTTPStatus.OK
    assert response.json()["pays"] == "2026-11-15"
