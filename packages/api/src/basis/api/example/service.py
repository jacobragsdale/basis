"""Accrued interest on a fixed-rate bond: the coupon interest earned between two dates under the bond's day count
convention, which a buyer pays the seller at settlement. Given a bond's terms, the service accrues over any period;
for a bond in its warehouse, it accrues since the last coupon, prices the bond with the pricing service's clean price,
solves for its yield to maturity in the service's process pool, lists its coupons, a dataset that a UI's grid can take
as Arrow, and records settlements, booking each with the ledger service through the warehouse's outbox.

This file is the service: its settings, its resources, the errors it declares, its handlers, and the declaration basis
builds its app from, in which each route declares its deadline, its response and its errors. The handlers compute and
return, and reach the warehouse, the pricing service, the cache, the clock and new ids only through what `api.given`
gives them, as they reach the yield solver in `compute.py`; the app around them, with its logs, traces, errors,
health, transactions, process pool and shutdown, is basis's. `dev.py` makes its fakes for `basis dev`."""

import calendar
import logging
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from http import HTTPStatus
from typing import Annotated, Literal, assert_never, get_args
from uuid import UUID

from basis import api
from fastapi import Path, Query
from pydantic import BaseModel, ConfigDict, Field

from basis.api.example import compute

log = logging.getLogger(__name__)

type Convention = Literal["30/360", "ACT/360", "ACT/365F"]
type Security = Annotated[str, Path(pattern=r"^[A-Z0-9]{12}$", description="The bond's ISIN.")]
"""A bond's ISIN, as a path parameter: twelve capital letters and digits, which is safe in the pricing service's path."""
CENT = Decimal("0.01")
MICRO = Decimal("0.000001")
HUNDRED = Decimal(100)


class Settings(api.Settings):
    max_days: Annotated[int, Field(ge=1)] = 3660
    """The longest period a request may accrue over, in calendar days: about ten years, past which a date is more
    likely a typo than a trade."""


class PeriodReversed(api.Error):
    status = HTTPStatus.UNPROCESSABLE_CONTENT
    title = "The period ends before it starts"


class PeriodTooLong(api.Error):
    status = HTTPStatus.UNPROCESSABLE_CONTENT
    title = "The period is longer than this service accrues over"


class UnknownBond(api.Error):
    status = HTTPStatus.NOT_FOUND
    title = "No bond has that ISIN"


class Unpriced(api.Error):
    status = HTTPStatus.NOT_FOUND
    title = "The pricing service has no price for the bond"


class Matured(api.Error):
    status = HTTPStatus.UNPROCESSABLE_CONTENT
    title = "The bond matures on or before the settlement date"


class NotIssued(api.Error):
    status = HTTPStatus.UNPROCESSABLE_CONTENT
    title = "The bond is issued after the settlement date"


class NoYield(api.Error):
    status = HTTPStatus.UNPROCESSABLE_CONTENT
    title = "No yield gives the bond's price"


class Period(BaseModel):
    """The query: a bond's face amount and coupon, and the period to accrue over. An unknown parameter is refused."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    face: Annotated[Decimal, Field(gt=0)]
    rate: Annotated[Decimal, Field(ge=0, lt=1)]
    """The annual coupon rate, as a fraction: 0.045 for 4.5%."""
    start: date
    end: date
    convention: Convention


class Accrual(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start: date
    end: date
    convention: Convention
    days: int
    """The days in the period, as the convention counts them."""
    year_days: int
    """The days in a year, as the convention counts them."""
    interest: Decimal
    """In the face amount's currency, rounded half to even to the cent."""


class DayCount(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    convention: Convention
    year_days: int


class Bond(BaseModel):
    """A bond's terms, as the warehouse's `bonds` table holds them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    security_id: str
    coupon_rate: Decimal
    """The annual coupon rate, as a fraction."""
    convention: Convention
    frequency: Literal[1, 2, 4, 12]
    """Coupons a year, the last of them paid at maturity."""
    issued: date
    """The date it was issued, from which its first coupon period accrues."""
    maturity: date


class Holding(BaseModel):
    """The query to price a holding: its face amount, and the date it settles."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    face: Annotated[Decimal, Field(gt=0)]
    settle: date


class CleanPrice(BaseModel):
    """The pricing service's quote. Fields it adds later are ignored, as basis-ui ignores an API's."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    security_id: str
    clean: Decimal
    """Per 100 of face."""


class DirtyPrice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    security_id: str
    settle: date
    face: Decimal
    clean: Decimal
    """Per 100 of face, as the pricing service quotes it."""
    accrued: Decimal
    """The interest accrued since the last coupon, on the face amount, to the cent."""
    dirty: Decimal
    """What the holding costs at settlement: its clean price on the face amount, and the interest accrued, to the cent."""


class Yield(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    security_id: str
    settle: date
    clean: Decimal
    """Per 100 of face, as the pricing service quotes it."""
    dirty: Decimal
    """Per 100 of face: the clean price and the interest accrued since the last coupon, to six places."""
    annual_yield: Decimal
    """The yield to maturity, compounded as often as the bond pays coupons, as a fraction: 0.0461 for 4.61%."""


class Coupon(BaseModel):
    """One of a bond's coupons, a row of its schedule."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    starts: date
    """The day its period starts accruing: the coupon date before, or the issue date for the first."""
    pays: date
    interest: Annotated[Decimal, Field(max_digits=12, decimal_places=2)]
    """Per 100 of face, as the bond's convention accrues it over the period, to the cent."""


class SettlementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    security_id: Annotated[str, Field(pattern=r"^[A-Z0-9]{12}$")]
    face: Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=2)]
    settle: date


class Settlement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    settlement_id: UUID
    security_id: str
    face: Decimal
    settle: date
    accrued: Decimal
    recorded_at: datetime


WAREHOUSE = api.database("warehouse", deadline_s=5, max_concurrent=8, outbox=api.outbox("accruals_outbox", max_age_s=86_400, backoff_s=60, max_backoff_s=3_600))
"""Where each bond's terms are, where settlements are recorded, and where their bookings wait for the ledger: a day, at
most, retried from a minute to an hour apart."""
PRICING = api.http("pricing", deadline_s=2)
"""The pricing service, which quotes each bond's clean price."""
LEDGER = api.http("ledger", deadline_s=2)
"""The ledger service, which books each settlement, and drops a booking whose `Idempotency-Key` it has seen."""
TERMS = api.cache("bond_terms", Bond, ttl_s=300, max_entries=10_000, deadline_s=5)
"""Bonds' terms, which change far less often than every five minutes."""
CLOCK = api.clock("clock")
IDS = api.ids("ids")
SOLVE = api.compute(compute.yield_to_maturity)
"""The yield solver, which runs in the service's process pool."""

BOND = "SELECT security_id, coupon_rate, convention, frequency, issued, maturity FROM bonds WHERE security_id = %(security)s"
RECORD = "INSERT INTO settlements (settlement_id, security_id, face, settle, accrued, recorded_at) VALUES (%(settlement_id)s, %(security)s, %(face)s, %(settle)s, %(accrued)s, %(recorded_at)s)"


def day_count(start: date, end: date, convention: Convention) -> tuple[int, int]:
    """The days from `start` to `end` under `convention`, and the days in its year. 30/360 counts every month as 30
    days, moving a 31st back to the 30th, and an end on the 31st too when the start is on the 30th or 31st."""
    match convention:
        case "30/360":
            first = min(start.day, 30)
            last = min(end.day, 30) if first == 30 else end.day  # noqa: PLR2004 -- the convention's month
            return 360 * (end.year - start.year) + 30 * (end.month - start.month) + last - first, 360
        case "ACT/360":
            return (end - start).days, 360
        case "ACT/365F":
            return (end - start).days, 365
        case unreachable:
            assert_never(unreachable)


def accrue(face: Decimal, rate: Decimal, start: date, end: date, convention: Convention) -> Accrual:
    days, year_days = day_count(start, end, convention)
    interest = (face * rate * days / year_days).quantize(CENT, rounding=ROUND_HALF_EVEN)
    return Accrual(start=start, end=end, convention=convention, days=days, year_days=year_days, interest=interest)


def last_coupon(bond: Bond, settle: date) -> date:
    """The date the interest accrued at `settle` runs from: the coupon date on or before it, counting back from maturity
    by the coupon's months, each on the maturity's day of the month, or the month's last day where it has fewer days;
    or the issue date, in the first period. A settlement outside the bond's life, before its issue or on or after its
    maturity, is refused."""
    return max(_period(bond, settle)[0], bond.issued)


def _period(bond: Bond, settle: date) -> tuple[date, date, int]:
    """The coupon period `settle` is in, as its regular start, on or before `settle`, its end, the next coupon date
    after `settle`, and the coupons still to pay, that one included."""
    if settle < bond.issued:
        msg = f"{bond.security_id} is issued on {bond.issued}, and the settlement date is {settle}"
        raise NotIssued(msg)
    if settle >= bond.maturity:
        msg = f"{bond.security_id} matures on {bond.maturity}, and the settlement date is {settle}"
        raise Matured(msg)
    step = 12 // bond.frequency
    months = 12 * (bond.maturity.year - settle.year) + bond.maturity.month - settle.month
    periods = max(1, months // step)  # the coupon in settle's month or after it, so at most one step more is needed
    while (coupon := _months_before(bond.maturity, step * periods)) > settle:
        periods += 1
    return coupon, _months_before(bond.maturity, step * (periods - 1)), periods


def _months_before(day: date, months: int) -> date:
    month = day.year * 12 + day.month - 1 - months
    year, month = divmod(month, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def accrued_interest(period: Annotated[Period, Query()], settings: Annotated[Settings, api.given(Settings)]) -> Accrual:
    start, end = period.start, period.end
    if end < start:
        msg = f"{end} is before {start}"
        raise PeriodReversed(msg, errors=[api.ParameterError(detail=f"is before the start, {start}", parameter="end")])
    if (end - start).days > settings.max_days:
        msg = f"{start} to {end} is {(end - start).days} days, and this service accrues over {settings.max_days} at most"
        raise PeriodTooLong(msg)
    accrual = accrue(period.face, period.rate, start, end, period.convention)
    log.info("accrued %s days under %s", accrual.days, period.convention, extra={"days": accrual.days, "convention": period.convention})
    return accrual


def conventions() -> list[DayCount]:
    return [DayCount(convention=convention, year_days=day_count(date(2026, 1, 1), date(2026, 1, 1), convention)[1]) for convention in get_args(Convention.__value__)]


async def bond(security: Security, warehouse: Annotated[api.Reader, api.given(WAREHOUSE)]) -> Bond:
    return await _bond(warehouse, security)


async def dirty_price(
    security: Security,
    holding: Annotated[Holding, Query()],
    warehouse: Annotated[api.Reader, api.given(WAREHOUSE)],
    terms: Annotated[api.Cache[Bond], api.given(TERMS)],
    pricing: Annotated[api.Client, api.given(PRICING)],
) -> DirtyPrice:
    found = await terms.get(security, lambda: _bond(warehouse, security))
    accrued = accrue(holding.face, found.coupon_rate, last_coupon(found, holding.settle), holding.settle, found.convention).interest
    price = await _clean_price(pricing, security)
    dirty = (holding.face * price.clean / HUNDRED + accrued).quantize(CENT, rounding=ROUND_HALF_EVEN)
    return DirtyPrice(security_id=security, settle=holding.settle, face=holding.face, clean=price.clean, accrued=accrued, dirty=dirty)


async def bond_yield(  # noqa: PLR0913, PLR0917 -- what the route reads and reaches, each a parameter FastAPI fills
    security: Security,
    settle: date,
    warehouse: Annotated[api.Reader, api.given(WAREHOUSE)],
    terms: Annotated[api.Cache[Bond], api.given(TERMS)],
    pricing: Annotated[api.Client, api.given(PRICING)],
    solve: Annotated[api.Compute[[Decimal, Decimal, int, int, Decimal], Decimal], api.given(SOLVE)],
) -> Yield:
    """The bond's yield to maturity at the pricing service's clean price, settling on `settle`."""
    found = await terms.get(security, lambda: _bond(warehouse, security))
    start, end, periods = _period(found, settle)
    days, year_days = day_count(last_coupon(found, settle), settle, found.convention)
    price = await _clean_price(pricing, security)
    dirty = price.clean + HUNDRED * found.coupon_rate * days / year_days
    first = Decimal(day_count(settle, end, found.convention)[0]) / day_count(start, end, found.convention)[0]  # of a period, to the next coupon
    try:
        annual = await solve(dirty, HUNDRED * found.coupon_rate / found.frequency, found.frequency, periods, first)
    except ValueError as unsolved:
        raise NoYield(str(unsolved)) from unsolved
    return Yield(security_id=security, settle=settle, clean=price.clean, dirty=dirty.quantize(MICRO, rounding=ROUND_HALF_EVEN), annual_yield=annual)


async def coupons(security: Security, warehouse: Annotated[api.Reader, api.given(WAREHOUSE)]) -> list[Coupon]:
    """The bond's coupons, from its first to its last, which is paid at maturity."""
    found = await _bond(warehouse, security)
    step = 12 // found.frequency
    dates = [found.maturity]
    while (earlier := _months_before(found.maturity, step * len(dates))) > found.issued:
        dates.append(earlier)
    ends = sorted(dates)
    starts = [found.issued, *ends[:-1]]
    return [Coupon(starts=start, pays=end, interest=accrue(HUNDRED, found.coupon_rate, start, end, found.convention).interest) for start, end in zip(starts, ends, strict=True)]


async def record_settlement(
    request: SettlementRequest,
    warehouse: Annotated[api.Writer, api.given(WAREHOUSE)],
    ledger: Annotated[api.Client, api.given(LEDGER)],
    clock: Annotated[api.Clock, api.given(CLOCK)],
    ids: Annotated[api.Ids, api.given(IDS)],
) -> Settlement:
    """Record a settlement, with the interest accrued to its date, and book it with the ledger: the row and the booking
    commit once the response validates, and the ledger is called after, by its id, as many times as it takes."""
    found = await _bond(warehouse, request.security_id)
    accrued = accrue(request.face, found.coupon_rate, last_coupon(found, request.settle), request.settle, found.convention).interest
    settlement = Settlement(settlement_id=ids.new(), security_id=found.security_id, face=request.face, settle=request.settle, accrued=accrued, recorded_at=clock.now())
    await warehouse.execute(
        RECORD,
        settlement_id=settlement.settlement_id, security=settlement.security_id, face=settlement.face,
        settle=settlement.settle, accrued=settlement.accrued, recorded_at=settlement.recorded_at,
    )  # fmt: skip
    await ledger.send("POST", "/bookings", settlement, key=str(settlement.settlement_id))
    log.info("recorded settlement %s", settlement.settlement_id, extra={"settlement_id": str(settlement.settlement_id), "security_id": settlement.security_id})
    return settlement


async def _clean_price(pricing: api.Client, security: str) -> CleanPrice:
    try:
        return await pricing.get(f"/prices/{security}", CleanPrice)
    except api.CallFailedError as failed:
        if failed.reply == HTTPStatus.NOT_FOUND:
            msg = f"the pricing service has no price for {security}"
            raise Unpriced(msg) from failed
        raise


async def _bond(warehouse: api.Reader, security: str) -> Bond:
    if (found := await warehouse.fetch_one(Bond, BOND, security=security)) is None:
        msg = f"the warehouse has no bond {security}"
        raise UnknownBond(msg)
    return found


OUTSIDE_ITS_LIFE = [Matured, NotIssued]
"""How a settlement date can fall outside a bond's life."""

service = api.Service(
    name="accruals",
    settings=Settings,
    resources=[WAREHOUSE, PRICING, LEDGER, TERMS, CLOCK, IDS],
    routes=[
        api.get("/accrued-interest", accrued_interest, deadline_s=1, response=Accrual, errors=[PeriodReversed, PeriodTooLong]),
        api.get("/conventions", conventions, deadline_s=1, response=list[DayCount], errors=[]),
        api.get("/bonds/{security}", bond, deadline_s=5, response=Bond, errors=[UnknownBond]),
        api.get("/bonds/{security}/dirty-price", dirty_price, deadline_s=5, response=DirtyPrice, errors=[UnknownBond, Unpriced, *OUTSIDE_ITS_LIFE]),
        api.get("/bonds/{security}/yield", bond_yield, deadline_s=5, response=Yield, errors=[UnknownBond, Unpriced, NoYield, *OUTSIDE_ITS_LIFE]),
        api.get("/bonds/{security}/coupons", coupons, deadline_s=5, response=list[Coupon], errors=[UnknownBond], dataset=True),
        api.post("/settlements", record_settlement, status=HTTPStatus.CREATED, deadline_s=5, response=Settlement, errors=[UnknownBond, *OUTSIDE_ITS_LIFE]),
    ],
    fakes="basis.api.example.dev:fakes",
)
