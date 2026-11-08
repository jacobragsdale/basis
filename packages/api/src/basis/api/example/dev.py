"""The example's fakes, which `basis dev` and `basis call --fake` run it on, as its service declares with
`fakes="basis.api.example.dev:fakes"`: a warehouse of three synthetic bonds, a pricing service that quotes two of
them, and a ledger that takes every booking. The cache, the clock, the ids and the yield solver are the real ones,
since they reach nothing outside the process; settlements recorded in dev mode, and their bookings, go into the fake
warehouse's memory, and end with the process."""

from collections.abc import Mapping
from datetime import date
from http import HTTPStatus

from basis import api
from basis.api.fakes import Database, Fake, Http, Respond
from fastapi import FastAPI, HTTPException

from basis.api.example import repository, resources

BONDS: Mapping[str, Mapping[str, object]] = {
    "US912810TM64": {"coupon_rate": 0.045, "convention": "30/360", "frequency": 2, "issued": date(2024, 11, 15), "maturity": date(2034, 11, 15)},
    "XS2010030836": {"coupon_rate": 0.0325, "convention": "ACT/360", "frequency": 4, "issued": date(2025, 3, 31), "maturity": date(2030, 3, 31)},
    "DE0001102580": {"coupon_rate": 0.02, "convention": "ACT/365F", "frequency": 1, "issued": date(2023, 2, 15), "maturity": date(2033, 2, 15)},
}
"""Each bond's terms, by its ISIN: made up, like every value here."""
PRICES = {"US912810TM64": 98.5, "XS2010030836": 101.125}
"""Each clean price the fake pricing service quotes, per 100 of face; it has none for DE0001102580."""


def fakes() -> dict[api.Resource | api.ComputeFunction[..., object], Fake]:
    return {resources.WAREHOUSE: Database(answers={repository.BOND: Respond(_bond), repository.RECORD: 1}), resources.PRICING: Http(pricing()), resources.LEDGER: Http(ledger())}


def _bond(binds: Mapping[str, api.Bind]) -> list[Mapping[str, object]]:
    security = binds["security"]
    terms = BONDS.get(security) if isinstance(security, str) else None
    return [{"security_id": security, **terms}] if terms is not None else []


def ledger() -> FastAPI:
    """The ledger service, as far as the example calls it: it takes every booking."""
    app = FastAPI()

    @app.post("/bookings", status_code=HTTPStatus.CREATED)
    def book() -> None:
        return None

    return app


def pricing() -> FastAPI:
    """The pricing service, as far as the example calls it."""
    app = FastAPI()

    @app.get("/prices/{security}")
    def price(security: str) -> dict[str, object]:
        if (clean := PRICES.get(security)) is None:
            raise HTTPException(HTTPStatus.NOT_FOUND)
        return {"security_id": security, "clean": clean}

    return app
