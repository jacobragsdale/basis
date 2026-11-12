"""The example's domain: accrued interest on a fixed-rate bond, its coupons, and what its dirty price and its yield are
made of. Pure types and rules, with no I/O, no FastAPI and nothing of basis, so a job could run them as they stand.
Its errors are plain exceptions, which `errors.py` gives their HTTP meaning."""

import calendar
from dataclasses import dataclass
from datetime import date
from typing import Literal, assert_never, get_args
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict

type Convention = Literal["30/360", "ACT/360", "ACT/365F"]
HUNDRED = 100.0


class UnknownBond(Exception):  # noqa: N818 -- the domain's name for it, which its problem's type is
    """No bond has the ISIN asked for."""


class Unpriced(Exception):  # noqa: N818 -- the domain's name for it, which its problem's type is
    """The pricing service has no price for the bond."""


class Matured(Exception):  # noqa: N818 -- the domain's name for it, which its problem's type is
    """The bond matures on or before the settlement date."""


class NotIssued(Exception):  # noqa: N818 -- the domain's name for it, which its problem's type is
    """The bond is issued after the settlement date."""


class NoYield(Exception):  # noqa: N818 -- the domain's name for it, which its problem's type is
    """No yield gives the bond's price."""


class Accrual(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start: date
    end: date
    convention: Convention
    days: int
    """The days in the period, as the convention counts them."""
    year_days: int
    """The days in a year, as the convention counts them."""
    interest: float
    """In the face amount's currency, unrounded: the UI rounds it, once, as it shows it."""


class DayCount(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    convention: Convention
    year_days: int


class Bond(BaseModel):
    """A bond's terms, as the warehouse's `bonds` table holds them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    security_id: str
    coupon_rate: float
    """The annual coupon rate, as a fraction."""
    convention: Convention
    frequency: Literal[1, 2, 4, 12]
    """Coupons a year, the last of them paid at maturity."""
    issued: date
    """The date it was issued, from which its first coupon period accrues."""
    maturity: date


class Quote(BaseModel):
    """A bond's latest clean price, which a UI that shows it live is sent each time it changes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    security_id: str
    clean: float
    """Per 100 of face, as the pricing service quotes it."""


class DirtyPrice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    security_id: str
    settle: date
    face: float
    clean: float
    """Per 100 of face, as the pricing service quotes it."""
    accrued: float
    """The interest accrued since the last coupon, on the face amount."""
    dirty: float
    """What the holding costs at settlement: its clean price on the face amount, and the interest accrued."""


class Yield(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    security_id: str
    settle: date
    clean: float
    """Per 100 of face, as the pricing service quotes it."""
    dirty: float
    """Per 100 of face: the clean price and the interest accrued since the last coupon."""
    annual_yield: float
    """The yield to maturity, compounded as often as the bond pays coupons, as a fraction: 0.0461 for 4.61%."""


class Coupon(BaseModel):
    """One of a bond's coupons, a row of its schedule."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    starts: date
    """The day its period starts accruing: the coupon date before, or the issue date for the first."""
    pays: date
    interest: float
    """Per 100 of face, as the bond's convention accrues it over the period."""


class Settlement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    settlement_id: UUID
    security_id: str
    face: float
    settle: date
    accrued: float
    recorded_at: AwareDatetime


@dataclass(frozen=True)
class Remaining:
    """What is left of a bond at a settlement date, as its yield is solved from it."""

    periods: int
    """The coupons still to pay, the next among them."""
    first: float
    """The fraction of a period until the next coupon."""
    accrued: float
    """The interest accrued since the last coupon, per 100 of face, unrounded."""


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


def conventions() -> list[DayCount]:
    """Each convention, with the days in its year."""
    return [DayCount(convention=convention, year_days=day_count(date(2026, 1, 1), date(2026, 1, 1), convention)[1]) for convention in get_args(Convention.__value__)]


def accrue(face: float, rate: float, start: date, end: date, convention: Convention) -> Accrual:
    days, year_days = day_count(start, end, convention)
    interest = face * rate * days / year_days
    return Accrual(start=start, end=end, convention=convention, days=days, year_days=year_days, interest=interest)


def last_coupon(bond: Bond, settle: date) -> date:
    """The date the interest accrued at `settle` runs from: the coupon date on or before it, counting back from maturity
    by the coupon's months, each on the maturity's day of the month, or the month's last day where it has fewer days;
    or the issue date, in the first period. A settlement outside the bond's life, before its issue or on or after its
    maturity, is refused."""
    return max(period(bond, settle)[0], bond.issued)


def accrued_at(bond: Bond, face: float, settle: date) -> float:
    """The interest accrued on `face` of the bond from its last coupon to `settle`."""
    return accrue(face, bond.coupon_rate, last_coupon(bond, settle), settle, bond.convention).interest


def period(bond: Bond, settle: date) -> tuple[date, date, int]:
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
    while (coupon := months_before(bond.maturity, step * periods)) > settle:
        periods += 1
    return coupon, months_before(bond.maturity, step * (periods - 1)), periods


def months_before(day: date, months: int) -> date:
    month = day.year * 12 + day.month - 1 - months
    year, month = divmod(month, 12)
    return date(year, month + 1, min(day.day, calendar.monthrange(year, month + 1)[1]))


def dirty_price(bond: Bond, face: float, settle: date, accrued: float, clean: float) -> DirtyPrice:
    """A holding's price at settlement: its clean price on `face`, and the interest `accrued`."""
    dirty = face * clean / HUNDRED + accrued
    return DirtyPrice(security_id=bond.security_id, settle=settle, face=face, clean=clean, accrued=accrued, dirty=dirty)


def remaining(bond: Bond, settle: date) -> Remaining:
    """What is left of the bond at `settle`: its coupons to pay, the fraction of a period to the next, and the interest
    accrued since the last, per 100 of face."""
    start, end, periods = period(bond, settle)
    days, year_days = day_count(last_coupon(bond, settle), settle, bond.convention)
    first = day_count(settle, end, bond.convention)[0] / day_count(start, end, bond.convention)[0]  # of a period, to the next coupon
    return Remaining(periods=periods, first=first, accrued=HUNDRED * bond.coupon_rate * days / year_days)


def coupon(bond: Bond) -> float:
    """Each coupon the bond pays, per 100 of face."""
    return HUNDRED * bond.coupon_rate / bond.frequency


def quoted(bond: Bond, settle: date, clean: float, dirty: float, annual: float) -> Yield:
    """The bond's yield at `settle`, as `annual`, solved from its `dirty` price per 100."""
    return Yield(security_id=bond.security_id, settle=settle, clean=clean, dirty=dirty, annual_yield=annual)


def schedule(bond: Bond) -> list[Coupon]:
    """The bond's coupons, from its first to its last, which is paid at maturity, each period's interest per 100."""
    step = 12 // bond.frequency
    dates = [bond.maturity]
    while (earlier := months_before(bond.maturity, step * len(dates))) > bond.issued:
        dates.append(earlier)
    ends = sorted(dates)
    starts = [bond.issued, *ends[:-1]]
    return [Coupon(starts=start, pays=end, interest=accrue(HUNDRED, bond.coupon_rate, start, end, bond.convention).interest) for start, end in zip(starts, ends, strict=True)]
