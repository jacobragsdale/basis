"""Accrued interest on a fixed-rate bond: the coupon interest earned between two dates under the bond's day count
convention, which a buyer pays the seller at settlement.

This file is the service: its settings, the errors it declares, its handlers, and the declaration basis builds its app
from. The handlers compute and return; the app around them, with its logs, traces, errors, health and shutdown, is
basis's."""

import logging
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from http import HTTPStatus
from typing import Annotated, Literal, assert_never, get_args

from basis import api
from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)

type Convention = Literal["30/360", "ACT/360", "ACT/365F"]
CENT = Decimal("0.01")


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


def accrued_interest(period: Annotated[Period, Query()], settings: Annotated[Settings, api.given(Settings)]) -> Accrual:
    start, end = period.start, period.end
    if end < start:
        msg = f"{end} is before {start}"
        raise PeriodReversed(msg)
    if (end - start).days > settings.max_days:
        msg = f"{start} to {end} is {(end - start).days} days, and this service accrues over {settings.max_days} at most"
        raise PeriodTooLong(msg)
    days, year_days = day_count(start, end, period.convention)
    interest = (period.face * period.rate * days / year_days).quantize(CENT, rounding=ROUND_HALF_EVEN)
    log.info("accrued %s days under %s", days, period.convention, extra={"days": days, "convention": period.convention})
    return Accrual(start=start, end=end, convention=period.convention, days=days, year_days=year_days, interest=interest)


def conventions() -> list[DayCount]:
    return [DayCount(convention=convention, year_days=day_count(date(2026, 1, 1), date(2026, 1, 1), convention)[1]) for convention in get_args(Convention.__value__)]


service = api.Service(name="accruals", settings=Settings, routes=[api.get("/accrued-interest", accrued_interest), api.get("/conventions", conventions)])
