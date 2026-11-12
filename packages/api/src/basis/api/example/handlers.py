"""The example's handlers, one function per route: each reads its request, passes each resource it is given to the
repository function that reaches it, applies the domain's rules, and returns. A handler never calls a resource's
methods itself, so the SQL stays in `repository.py`; it awaits a compute function, which runs in the process pool."""

import logging
from datetime import date
from typing import Annotated

import pandas as pd
from basis import api
from fastapi import Path, Query
from pydantic import BaseModel, ConfigDict, Field

from basis.api.example import domain, repository
from basis.api.example.domain import Accrual, Bond, Convention, DayCount, DirtyPrice, Settlement, Yield
from basis.api.example.errors import PeriodReversed, PeriodTooLong
from basis.api.example.resources import CLOCK, IDS, LEDGER, PRICING, SOLVE, TERMS, WAREHOUSE, Settings

log = logging.getLogger(__name__)

type Security = Annotated[str, Path(pattern=r"^[A-Z0-9]{12}$", description="The bond's ISIN.")]
"""A bond's ISIN, as a path parameter: twelve capital letters and digits, which is safe in the pricing service's path."""


class Period(BaseModel):
    """The query: a bond's face amount and coupon, and the period to accrue over. An unknown parameter is refused."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    face: Annotated[float, Field(gt=0)]
    rate: Annotated[float, Field(ge=0, lt=1)]
    """The annual coupon rate, as a fraction: 0.045 for 4.5%."""
    start: date
    end: date
    convention: Convention


class Holding(BaseModel):
    """The query to price a holding: its face amount, and the date it settles."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    face: Annotated[float, Field(gt=0)]
    settle: date


class SettlementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    security_id: Annotated[str, Field(pattern=r"^[A-Z0-9]{12}$")]
    face: Annotated[float, Field(gt=0)]
    settle: date


def accrued_interest(period: Annotated[Period, Query()], settings: Annotated[Settings, api.given(Settings)]) -> Accrual:
    start, end = period.start, period.end
    if end < start:
        msg = f"{end} is before {start}"
        raise PeriodReversed(msg, errors=[api.ParameterError(detail=f"is before the start, {start}", parameter="end")])
    if (end - start).days > settings.max_days:
        msg = f"{start} to {end} is {(end - start).days} days, and this service accrues over {settings.max_days} at most"
        raise PeriodTooLong(msg)
    accrual = domain.accrue(period.face, period.rate, start, end, period.convention)
    log.info("accrued %s days under %s", accrual.days, period.convention, extra={"days": accrual.days, "convention": period.convention})
    return accrual


def conventions() -> list[DayCount]:
    return domain.conventions()


async def bond(security: Security, warehouse: Annotated[api.Reader, api.given(WAREHOUSE)]) -> Bond:
    return await repository.bond(warehouse, security)


async def dirty_price(
    security: Security,
    holding: Annotated[Holding, Query()],
    warehouse: Annotated[api.Reader, api.given(WAREHOUSE)],
    terms: Annotated[api.Cache[Bond], api.given(TERMS)],
    pricing: Annotated[api.Client, api.given(PRICING)],
) -> DirtyPrice:
    found = await repository.cached_bond(warehouse, terms, security)
    accrued = domain.accrued_at(found, holding.face, holding.settle)
    clean = await repository.clean_price(pricing, security)
    return domain.dirty_price(found, holding.face, holding.settle, accrued, clean)


async def bond_yield(  # noqa: PLR0917 -- what the route reads and reaches, each a parameter FastAPI fills
    security: Security,
    settle: date,
    warehouse: Annotated[api.Reader, api.given(WAREHOUSE)],
    terms: Annotated[api.Cache[Bond], api.given(TERMS)],
    pricing: Annotated[api.Client, api.given(PRICING)],
    solve: Annotated[api.Compute[[float, float, int, int, float], float], api.given(SOLVE)],
) -> Yield:
    """The bond's yield to maturity at the pricing service's clean price, settling on `settle`."""
    found = await repository.cached_bond(warehouse, terms, security)
    left = domain.remaining(found, settle)
    clean = await repository.clean_price(pricing, security)
    dirty = clean + left.accrued
    try:
        annual = await solve(dirty, domain.coupon(found), found.frequency, left.periods, left.first)
    except ValueError as unsolved:
        raise domain.NoYield(str(unsolved)) from unsolved
    return domain.quoted(found, settle, clean, dirty, annual)


class CouponRow(BaseModel):
    """A row of a bond's coupons as a UI's grid shows them: the domain's `Coupon`, and what it comes to, with each
    number's display format, which is how the API presents it, not a rule of the domain's. Between them the columns
    show every kind of display format, so the sample basis-ui pins holds each."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    starts: date
    """The day its period starts accruing: the coupon date before, or the issue date for the first."""
    pays: date
    interest: Annotated[float, api.format.price(4)]
    """Per 100 of face, as the bond's convention accrues it over the period."""
    interest_32nds: Annotated[float, api.format.price32()]
    """The same, as a Treasury desk reads a price, in 32nds."""
    rate: Annotated[float, api.format.pct(3)]
    """The bond's annual coupon rate, as a fraction: 0.045 is 4.5%."""
    period_rate: Annotated[float, api.format.bps(1)]
    """The period's interest as a fraction of face: 0.0225 is 225 bp."""
    per_million: Annotated[float, api.format.money("USD")]
    """The period's interest on 1,000,000 of face, in dollars."""
    year_fraction: Annotated[float, api.format.number(6)]
    """The share of a year the period accrues over, at the coupon rate; null for a bond that pays none."""


async def coupons(security: Security, warehouse: Annotated[api.Reader, api.given(WAREHOUSE)]) -> api.Table[CouponRow]:
    """The bond's coupons, from its first to its last, which is paid at maturity, as a table: a frame of them."""
    found = await repository.bond(warehouse, security)
    frame = pd.DataFrame([coupon.model_dump() for coupon in domain.schedule(found)], columns=["starts", "pays", "interest"])
    per_100 = frame["interest"]
    accrues = per_100 / (100 * found.coupon_rate) if found.coupon_rate > 0 else per_100 * float("nan")
    frame = frame.assign(interest_32nds=per_100, rate=found.coupon_rate, period_rate=per_100 / 100, per_million=per_100 * 10_000, year_fraction=accrues)
    return frame[list(CouponRow.model_fields)]


async def record_settlement(
    request: SettlementRequest,
    *,
    user: Annotated[api.User | None, api.given(api.User)],
    warehouse: Annotated[api.Writer, api.given(WAREHOUSE)],
    ledger: Annotated[api.Client, api.given(LEDGER)],
    clock: Annotated[api.Clock, api.given(CLOCK)],
    ids: Annotated[api.Ids, api.given(IDS)],
) -> Settlement:
    """Record a settlement, with the interest accrued to its date, and book it with the ledger: the row and the booking
    commit once the response validates, and the ledger is called after, by its id, as many times as it takes. Only a
    signed-in user records one, whom the request's log line names."""
    if user is None:
        msg = "sign in to record a settlement"
        raise api.SignInRequiredError(msg)
    found = await repository.bond(warehouse, request.security_id)
    accrued = domain.accrued_at(found, request.face, request.settle)
    settlement_id, recorded_at = repository.stamp(ids, clock)
    settlement = Settlement(settlement_id=settlement_id, security_id=found.security_id, face=request.face, settle=request.settle, accrued=accrued, recorded_at=recorded_at)
    await repository.record(warehouse, ledger, settlement)
    log.info("recorded settlement %s", settlement.settlement_id, extra={"settlement_id": str(settlement.settlement_id), "security_id": settlement.security_id})
    return settlement
