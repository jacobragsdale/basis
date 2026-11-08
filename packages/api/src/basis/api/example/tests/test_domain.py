"""The example's domain, unit by unit: the day count conventions, a bond's coupon dates and schedule, and what its dirty
price and its yield are made of. No app, no fakes: plain values in, plain values out."""

from datetime import date, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from basis.api.example import domain
from basis.api.example.domain import Bond, Convention, Matured, NotIssued, day_count, last_coupon

TERMS: dict[str, object] = {"security_id": "US912810TM64", "coupon_rate": 0.045, "convention": "30/360", "frequency": 2, "issued": date(2024, 11, 15), "maturity": date(2034, 11, 15)}
BOND = Bond.model_validate(TERMS)
SETTLE = date(2026, 11, 6)
PROPERTIES = settings(database=None, derandomize=True)
"""Each property's examples, the same on every run, and kept nowhere."""
DAYS = st.dates(min_value=date(1900, 1, 1), max_value=date(2200, 12, 31))
YEARS = st.integers(min_value=1900, max_value=2200)
THIRTIETHS = st.builds(date, YEARS, st.sampled_from([1, *range(3, 13)]), st.just(30))
THIRTY_FIRSTS = st.builds(date, YEARS, st.sampled_from([1, 3, 5, 7, 8, 10, 12]), st.just(31))


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


@PROPERTIES
@given(start=DAYS, end=DAYS, convention=st.sampled_from(["ACT/360", "ACT/365F"]))
def test_an_actual_convention_counts_the_calendars_days(start: date, end: date, convention: Convention) -> None:
    assert day_count(start, end, convention) == ((end - start).days, 360 if convention == "ACT/360" else 365)


@PROPERTIES
@given(start=DAYS, end=DAYS, day=st.integers(min_value=1, max_value=28))
def test_30_360_counts_thirty_days_a_month_between_days_every_month_has(start: date, end: date, day: int) -> None:
    months = 12 * (end.year - start.year) + end.month - start.month
    assert day_count(start.replace(day=day), end.replace(day=day), "30/360") == (30 * months, 360)


@PROPERTIES
@given(start=st.one_of(THIRTIETHS, THIRTY_FIRSTS), end=THIRTY_FIRSTS)
def test_30_360_counts_an_end_on_the_31st_as_the_30th_after_a_start_on_the_30th_or_31st(start: date, end: date) -> None:
    assert day_count(start, end, "30/360") == day_count(start, end - timedelta(days=1), "30/360")


@PROPERTIES
@given(start=DAYS, length=st.integers(min_value=0, max_value=3660), convention=st.sampled_from(["30/360", "ACT/360", "ACT/365F"]))
def test_a_conventions_days_never_fall_as_a_period_grows(start: date, length: int, convention: Convention) -> None:
    end = start + timedelta(days=length)
    days, year_days = day_count(start, end, convention)
    later, same_year = day_count(start, end + timedelta(days=1), convention)
    assert (later >= days >= 0, same_year) == (True, year_days)


def test_interest_is_unrounded_since_the_ui_rounds_it() -> None:
    half_a_cent = domain.accrue(1.8, 0.1, date(2026, 1, 1), date(2026, 1, 11), "30/360")
    assert half_a_cent.interest == pytest.approx(0.005)


def test_each_convention_has_its_years_days() -> None:
    assert [(found.convention, found.year_days) for found in domain.conventions()] == [("30/360", 360), ("ACT/360", 360), ("ACT/365F", 365)]


@pytest.mark.parametrize(
    ("maturity", "frequency", "settle", "expected"),
    [
        (date(2034, 11, 15), 2, date(2026, 11, 6), date(2026, 5, 15)),
        (date(2034, 11, 15), 2, date(2026, 11, 15), date(2026, 11, 15)),
        (date(2034, 11, 15), 2, date(2034, 11, 14), date(2034, 5, 15)),
        (date(2034, 8, 31), 2, date(2027, 3, 15), date(2027, 2, 28)),
        (date(2034, 8, 31), 2, date(2028, 3, 15), date(2028, 2, 29)),
        (date(2034, 11, 15), 12, date(2026, 11, 6), date(2026, 10, 15)),
        (date(2034, 11, 15), 1, date(2026, 11, 6), date(2025, 11, 15)),
        (date(2034, 11, 15), 2, date(2025, 3, 1), date(2025, 1, 10)),
    ],
    ids=["semiannual", "on-a-coupon-date", "last-period", "month-end", "month-end-in-a-leap-year", "monthly", "annual", "first-period-from-its-issue"],
)
def test_last_coupon(maturity: date, frequency: int, settle: date, expected: date) -> None:
    bond = Bond.model_validate(TERMS | {"issued": date(2025, 1, 10) if settle.year == 2025 else TERMS["issued"], "maturity": maturity, "frequency": frequency})
    assert last_coupon(bond, settle) == expected


@pytest.mark.parametrize(("settle", "refused"), [(date(2034, 11, 15), Matured), (date(2024, 11, 14), NotIssued), (date(1, 1, 1), NotIssued)], ids=["at-maturity", "before-issue", "the-first-day"])
def test_a_settlement_outside_the_bonds_life_has_no_last_coupon(settle: date, refused: type[Exception]) -> None:
    with pytest.raises(refused, match="US912810TM64"):
        last_coupon(BOND, settle)


def test_the_interest_accrued_at_a_settlement_runs_from_the_last_coupon() -> None:
    # 30/360 from the coupon of 2026-05-15 to 2026-11-06 is 171 days: 1,000,000 x 4.5% x 171/360 = 21,375.00
    assert domain.accrued_at(BOND, 1_000_000.0, SETTLE) == 21375.00


def test_a_dirty_price_is_the_clean_price_on_the_face_and_the_interest_accrued() -> None:
    price = domain.dirty_price(BOND, 1_000_000.0, SETTLE, 21375.00, 98.50)
    assert (price.security_id, price.dirty) == ("US912810TM64", 1006375.00)


def test_what_remains_of_a_bond_is_its_coupons_the_way_to_the_next_and_the_interest_since_the_last() -> None:
    left = domain.remaining(BOND, SETTLE)
    # nine days of a 180-day period before the first of 17 coupons, and 171 days of interest at 4.5% on 100
    assert (left.periods, left.first, left.accrued) == (17, 9.0 / 180, 2.1375)


def test_a_coupon_is_the_annual_rate_split_over_the_years_payments() -> None:
    assert domain.coupon(BOND) == 2.25


def test_a_bonds_schedule_runs_from_its_issue_to_its_maturity() -> None:
    coupons = domain.schedule(BOND)
    # semiannual from its issue to its maturity, each 30/360 period at 4.5% on 100: 2.25
    assert (len(coupons), coupons[0].starts, coupons[0].pays, coupons[-1].pays) == (20, date(2024, 11, 15), date(2025, 5, 15), date(2034, 11, 15))
    assert {coupon.interest for coupon in coupons} == {2.25}


def test_a_bond_issued_between_coupon_dates_has_a_short_first_coupon() -> None:
    coupons = domain.schedule(BOND.model_copy(update={"issued": date(2025, 3, 15)}))
    # 60 days of 30/360 at 4.5% on 100
    assert (coupons[0].starts, coupons[0].pays, coupons[0].interest) == (date(2025, 3, 15), date(2025, 5, 15), 0.75)
