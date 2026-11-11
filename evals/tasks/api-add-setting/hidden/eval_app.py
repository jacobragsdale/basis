"""The quotes service built on fakes, as its own tests build it, for the eval's hidden tests: a warehouse that knows
one bond, a pricing service that quotes it, and a ledger that takes every booking."""

from collections.abc import Mapping
from datetime import UTC, date, datetime
from http import HTTPStatus

from basis import api
from basis.api import fakes
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from quotes import resources, service
from quotes.repository import BOND, RECORD
from quotes.resources import Settings

ISIN = "US912810TM64"
TERMS = {"security_id": ISIN, "coupon_rate": 0.045, "convention": "30/360", "frequency": 2, "issued": date(2024, 11, 15), "maturity": date(2034, 11, 15)}
IDENTITY = {"issuer": "https://sign-in.example", "audience": "accruals", "algorithms": ["ES256"], "leeway_s": 0, "keys": {"kind": "development"}}


def _bonds(binds: Mapping[str, api.Bind]) -> list[Mapping[str, object]]:
    return [TERMS] if binds["security"] == ISIN else []


def _pricing() -> FastAPI:
    pricing = FastAPI()

    @pricing.get("/prices/{security}")
    def price(security: str) -> dict[str, object]:
        if security != ISIN:
            raise HTTPException(HTTPStatus.NOT_FOUND)
        return {"security_id": security, "clean": 98.5}

    return pricing


def _ledger() -> FastAPI:
    ledger = FastAPI()

    @ledger.post("/bookings", status_code=HTTPStatus.CREATED)
    def book() -> None:
        return None

    return ledger


def client(**settings: object) -> TestClient:
    given: fakes.Fakes = {
        resources.WAREHOUSE: fakes.Database(answers={BOND: fakes.Respond(_bonds), RECORD: 1}),
        resources.PRICING: fakes.Http(_pricing()),
        resources.LEDGER: fakes.Http(_ledger()),
        resources.TERMS: fakes.Cache(),
        resources.CLOCK: fakes.Clock(at=datetime(2026, 11, 6, 14, 30, tzinfo=UTC)),
        resources.IDS: fakes.Ids(),
        resources.SOLVE: fakes.Compute(),
    }
    return TestClient(api.build(service.service, settings=Settings.given(identity=IDENTITY, **settings), fakes=given).asgi)
