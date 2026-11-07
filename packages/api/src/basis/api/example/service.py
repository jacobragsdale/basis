"""Accrued interest on a fixed-rate bond: the coupon interest earned between two dates under the bond's day count
convention, which a buyer pays the seller at settlement. Given a bond's terms, the service accrues over any period;
for a bond in its warehouse, it accrues since the last coupon, prices the bond with the pricing service's clean price,
and records settlements.

This file is the service: its settings, its resources, the errors it declares, its handlers, and the declaration basis
builds its app from. The handlers compute and return, and reach the warehouse, the pricing service, the cache, the
clock and new ids only through what `api.given` gives them; the app around them, with its logs, traces, errors,
health, transactions and shutdown, is basis's."""

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

log = logging.getLogger(__name__)

type Convention = Literal["30/360", "ACT/360", "ACT/365F"]
type Security = Annotated[str, Path(pattern=r"^[A-Z0-9]{12}$", description="The bond's ISIN.")]
"""A bond's ISIN, as a path parameter: twelve capital letters and digits, which is safe in the pricing service's path."""
CENT = Decimal("0.01")
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


WAREHOUSE = api.database("warehouse", deadline_s=5)
"""Where each bond's terms are, and where settlements are recorded."""
PRICING = api.http("pricing", deadline_s=2)
"""The pricing service, which quotes each bond's clean price."""
TERMS = api.cache("bond_terms", Bond, ttl_s=300, max_entries=10_000, deadline_s=5)
"""Bonds' terms, which change far less often than every five minutes."""
CLOCK = api.clock("clock")
IDS = api.ids("ids")

BOND = "SELECT security_id, coupon_rate, convention, frequency, maturity FROM bonds WHERE security_id = %(security)s"
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
    """The coupon date on or before `settle`: counting back from maturity by the coupon's months, each on the
    maturity's day of the month, or the month's last day where it has fewer days."""
    if settle >= bond.maturity:
        msg = f"{bond.security_id} matures on {bond.maturity}, and the settlement date is {settle}"
        raise Matured(msg)
    step = 12 // bond.frequency
    periods = 1
    while (coupon := _months_before(bond.maturity, step * periods)) > settle:
        periods += 1
    return coupon


def _months_before(day: date, months: int) -> date:
    month = day.year * 12 + day.month - 1 - months
    year, month = divmod(month, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def accrued_interest(period: Annotated[Period, Query()], settings: Annotated[Settings, api.given(Settings)]) -> Accrual:
    start, end = period.start, period.end
    if end < start:
        msg = f"{end} is before {start}"
        raise PeriodReversed(msg)
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
    try:
        price = await pricing.get(f"/prices/{security}", CleanPrice)
    except api.CallFailedError as failed:
        if failed.reply == HTTPStatus.NOT_FOUND:
            msg = f"the pricing service has no price for {security}"
            raise Unpriced(msg) from failed
        raise
    dirty = (holding.face * price.clean / HUNDRED + accrued).quantize(CENT, rounding=ROUND_HALF_EVEN)
    return DirtyPrice(security_id=security, settle=holding.settle, face=holding.face, clean=price.clean, accrued=accrued, dirty=dirty)


async def record_settlement(
    request: SettlementRequest, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)], clock: Annotated[api.Clock, api.given(CLOCK)], ids: Annotated[api.Ids, api.given(IDS)]
) -> Settlement:
    """Record a settlement, with the interest accrued to its date: the row commits once the response validates."""
    found = await _bond(warehouse, request.security_id)
    accrued = accrue(request.face, found.coupon_rate, last_coupon(found, request.settle), request.settle, found.convention).interest
    settlement = Settlement(settlement_id=ids.new(), security_id=found.security_id, face=request.face, settle=request.settle, accrued=accrued, recorded_at=clock.now())
    await warehouse.execute(
        RECORD,
        settlement_id=settlement.settlement_id, security=settlement.security_id, face=settlement.face,
        settle=settlement.settle, accrued=settlement.accrued, recorded_at=settlement.recorded_at,
    )  # fmt: skip
    log.info("recorded settlement %s", settlement.settlement_id, extra={"settlement_id": str(settlement.settlement_id), "security_id": settlement.security_id})
    return settlement


async def _bond(warehouse: api.Reader, security: str) -> Bond:
    if (found := await warehouse.fetch_one(Bond, BOND, security=security)) is None:
        msg = f"the warehouse has no bond {security}"
        raise UnknownBond(msg)
    return found


service = api.Service(
    name="accruals",
    settings=Settings,
    resources=[WAREHOUSE, PRICING, TERMS, CLOCK, IDS],
    routes=[
        api.get("/accrued-interest", accrued_interest),
        api.get("/conventions", conventions),
        api.get("/bonds/{security}", bond),
        api.get("/bonds/{security}/dirty-price", dirty_price),
        api.post("/settlements", record_settlement, status=HTTPStatus.CREATED),
    ],
)
