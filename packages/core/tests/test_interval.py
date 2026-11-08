import re
from datetime import UTC, datetime

import pytest

from basis.core.calendars import calendar
from basis.core.interval import Interval, IntervalError, Relative, given

SIFMA = calendar("sifma-us")
TUESDAY = datetime(2026, 11, 10, 15, tzinfo=UTC)
"""10 a.m. in New York, the day after a Monday that is a business day and the day before Veterans Day, a holiday."""


def resolved(text: str, now: datetime = TUESDAY) -> str:
    found = given(text)
    assert isinstance(found, Relative)
    return str(found.resolve(now, SIFMA, job="positions"))


@pytest.mark.parametrize(
    ("text", "interval"),
    [
        ("today", "2026-11-10..2026-11-11"),
        ("yesterday", "2026-11-09..2026-11-10"),
        ("previous-business-day", "2026-11-09..2026-11-10"),
        ("previous-month", "2026-10-01..2026-11-01"),
        ("today-7d..today", "2026-11-03..2026-11-10"),
        ("today-1bd..today", "2026-11-09..2026-11-10"),
        ("today-3bd..today-1bd", "2026-11-05..2026-11-09"),
        ("2026-11-01..today", "2026-11-01..2026-11-10"),
    ],
)
def test_a_relative_interval_resolves_to_the_dates_written_by_hand(text: str, interval: str) -> None:
    assert resolved(text) == interval


def test_the_previous_business_day_skips_a_weekend_and_a_holiday() -> None:
    assert resolved("previous-business-day", datetime(2026, 11, 9, 15, tzinfo=UTC)) == "2026-11-06..2026-11-07"  # Monday: the Friday
    assert resolved("previous-business-day", datetime(2026, 11, 27, 15, tzinfo=UTC)) == "2026-11-25..2026-11-26"  # the Friday after Thanksgiving


def test_today_is_the_calendar_s_day_not_utc_s() -> None:
    sunday_evening_in_new_york = datetime(2026, 11, 9, 2, tzinfo=UTC)  # Monday in UTC
    assert resolved("today", sunday_evening_in_new_york) == "2026-11-08..2026-11-09"
    assert resolved("previous-business-day", sunday_evening_in_new_york) == "2026-11-06..2026-11-07"


def test_dates_and_instants_stay_absolute() -> None:
    assert given("2026-11-05..2026-11-06") == Interval.parse("2026-11-05..2026-11-06")
    assert given(" 2026-11-05T06:00Z..2026-11-05T07:00Z ") == Interval.parse("2026-11-05T06:00Z..2026-11-05T07:00Z")


def test_a_relative_interval_needs_the_job_s_calendar() -> None:
    with pytest.raises(
        IntervalError, match=r'^positions declares no calendar, which --interval today is resolved in: declare one in the job\'s module, as CALENDAR = etl.calendar\("sifma-us"\), or give dates$'
    ):
        Relative("today").resolve(TUESDAY, None, job="positions")


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("tomorrow", "an interval is START..END, as 2026-11-05..2026-11-06 or today-7d..today, or one of today, yesterday, previous-business-day, previous-month, not 'tomorrow'"),
        ("today-1w..today", "'today-1w' is not a date, an instant with its offset, or today, today-Nd or today-Nbd (Invalid isoformat string: 'today-1w')"),
        ("2026-13-01..today", "'2026-13-01' is not a date, an instant with its offset, or today, today-Nd or today-Nbd (month must be in 1..12, not 13)"),
    ],
)
def test_an_interval_basis_cannot_read_is_refused_when_it_is_given(text: str, error: str) -> None:
    with pytest.raises(ValueError, match=f"^{re.escape(error)}$"):
        given(text)
