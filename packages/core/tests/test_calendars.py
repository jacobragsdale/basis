"""The calendars against what their publishers published, and what each of their functions gives.

`fixtures/calendars/sifma.json` is basis-ui's transcription of every schedule SIFMA published for 1996 to 2027, each
with its address and the day it was fetched (basis-ui's `packages/ui/tests/fixtures/calendars/sifma.json`, at
8f0d1e9), so basis and the UI are held to the same days. Every day a source lists must match the calendar, and in
the years it covers in full, every weekday the calendar closes, or closes early, must be listed. Where SIFMA's own
sources disagree, `DISPUTES` names the source the calendar follows, as basis-ui's tests do. TARGET2's publications are
rules, the ECB's six closing days, which the tests apply year by year."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from dateutil.easter import easter as published_easter
from pydantic import BaseModel, Field

from basis.core import calendars
from basis.core.calendars import Calendar, CalendarError, calendar
from basis.core.errors import BasisError

SIFMA = calendar("sifma-us")
TARGET2 = calendar("target2")
FIXTURES = Path(__file__).parent / "fixtures" / "calendars"
HISTORY_PDF = "https://www.sifma.org/wp-content/uploads/2017/08/Misc-US-Historical-Holiday-Market-Recommendations-SIFMA.pdf"
US_ARCHIVE = "https://www.sifma.org/resources/guides-playbooks/us-holiday-archive"


def archived(stamp: str, page: str) -> str:
    return f"https://web.archive.org/web/{stamp}/http://www.sifma.org/services/{page}/"


class Listed(BaseModel):
    date: str
    close: str | None
    """Its early close, as `14:00`, or None: closed."""


class Source(BaseModel):
    title: str
    url: str
    years: tuple[int, ...]
    """The years it lists in full."""
    listed: tuple[Listed, ...] = Field(alias="days")

    @property
    def days(self) -> dict[str, str]:
        """Each day it lists: `closed`, or its early close, as `14:00`."""
        return {day.date: day.close or "closed" for day in self.listed}


class Fixture(BaseModel):
    sources: tuple[Source, ...]


SOURCES = Fixture.model_validate_json((FIXTURES / "sifma.json").read_text(encoding="utf-8")).sources


@dataclass(frozen=True)
class Dispute:
    on: str
    source: str
    follows: str
    why: str


DISPUTES = (
    Dispute("2010-12-31", HISTORY_PDF, archived("20110823165211", "holiday-schedule-archive-2010"), "the 2010 archive page lists the early close before New Year's Day 2011, a Saturday"),
    Dispute("2012-07-03", HISTORY_PDF, archived("20130519101204", "holiday-schedule-archive-2012"), "the 2012 archive page lists an early close before Independence Day"),
    Dispute("2013-07-03", HISTORY_PDF, archived("20140404005123", "holiday-schedule-archive-2013"), "the 2013 archive page lists an early close before Independence Day"),
    Dispute("2015-04-02", HISTORY_PDF, US_ARCHIVE, "with the employment report due on Good Friday, SIFMA moved 2015's Good Friday to a noon close"),
    Dispute("2015-04-03", HISTORY_PDF, US_ARCHIVE, "with the employment report due on Good Friday, SIFMA moved 2015's Good Friday to a noon close"),
    Dispute("2015-07-02", HISTORY_PDF, US_ARCHIVE, "SIFMA's final 2015 schedule recommends no early close before Independence Day"),
    Dispute("2015-02-16", US_ARCHIVE, HISTORY_PDF, "the archive page leaves Presidents Day 2015 blank"),
    Dispute("2016-02-15", US_ARCHIVE, HISTORY_PDF, "the archive page leaves Presidents Day 2016 blank"),
)
"""Where one SIFMA source disagrees with the calendar, and the SIFMA source it follows instead. The 2018 compilation of
1996 to 2019 gives some years' schedules as first issued, before SIFMA revised them."""
UNSCHEDULED = frozenset({"2001-09-11", "2001-09-12", "2001-09-13", "2001-09-14", "2001-09-17", "2004-06-11", "2007-01-02", "2012-10-29", "2012-10-30", "2018-12-05", "2025-01-09"})
"""Unscheduled closes, which a published holiday schedule may leave out; each is listed by some source."""


def shown(cal: Calendar, on: str) -> str:
    """A day as the fixtures write it: `closed`, `open`, or its early close, as `14:00`."""
    day = date.fromisoformat(on)
    if not cal.is_business_day(day):
        return "closed"
    closes = cal.early_close(day)
    return "open" if closes is None else closes.strftime("%H:%M")


def disputed(source: Source, on: str) -> bool:
    return any(dispute.source == source.url and dispute.on == on for dispute in DISPUTES)


def weekdays(year: int) -> list[date]:
    first = date(year, 1, 1)
    days = (first + timedelta(days=offset) for offset in range(366))
    return [day for day in days if day.year == year and day.weekday() < calendars.SATURDAY]


@pytest.mark.parametrize("source", SOURCES, ids=lambda source: source.title)
def test_every_day_a_sifma_source_lists_matches_the_calendar(source: Source) -> None:
    differ = {on: (close, shown(SIFMA, on)) for on, close in source.days.items() if not disputed(source, on) and shown(SIFMA, on) != close}
    assert differ == {}


@pytest.mark.parametrize("source", [source for source in SOURCES if source.years], ids=lambda source: source.title)
def test_a_sifma_source_lists_every_irregular_weekday_of_the_years_it_covers(source: Source) -> None:
    irregular = [day.isoformat() for year in source.years for day in weekdays(year) if shown(SIFMA, day.isoformat()) != "open"]
    missing = [on for on in irregular if on not in source.days and on not in UNSCHEDULED and not disputed(source, on)]
    assert missing == []


@pytest.mark.parametrize("dispute", DISPUTES, ids=lambda dispute: f"{dispute.on}: {dispute.why}")
def test_where_sifma_sources_disagree_the_calendar_follows_the_one_named(dispute: Dispute) -> None:
    by_url = {source.url: source for source in SOURCES}
    followed = by_url[dispute.follows].days.get(dispute.on, "open")
    assert shown(SIFMA, dispute.on) == followed
    assert by_url[dispute.source].days.get(dispute.on, "open") != followed


def test_each_unscheduled_close_is_listed_by_a_source_and_sifma_covers_the_years_its_schedules_do() -> None:
    listed = {on for source in SOURCES for on in source.days}
    assert sorted(UNSCHEDULED - listed) == []
    years = {year for source in SOURCES for year in source.years}
    assert sorted(set(range(1996, 2028)) - years) == []
    assert (SIFMA.first, SIFMA.last) == (date(1996, 1, 1), date(2027, 12, 31))


def test_target2_closes_on_the_ecb_s_six_closing_days_alone_and_never_early() -> None:
    differ = {}
    for year in range(2000, 2100):
        good_friday = published_easter(year) - timedelta(days=2)
        named = {date(year, 1, 1), good_friday, good_friday + timedelta(days=3), date(year, 5, 1), date(year, 12, 25), date(year, 12, 26)}
        if year == 2001:
            named.add(date(2001, 12, 31))  # the changeover to euro cash
        expected = sorted(day for day in named if day.weekday() < calendars.SATURDAY)
        closed = [day for day in weekdays(year) if not TARGET2.is_business_day(day)]
        early = [day for day in weekdays(year) if TARGET2.is_business_day(day) and TARGET2.early_close(day) is not None]
        if closed != expected or early:
            differ[year] = (closed, expected, early)
    assert differ == {}
    assert (TARGET2.first, TARGET2.last) == (date(2000, 1, 1), date(2099, 12, 31))


def test_easter_is_the_gregorian_easter_of_every_year_either_calendar_covers() -> None:
    assert [year for year in range(1996, 2100) if calendars.easter(year) != published_easter(year)] == []


NEW_YORK = ZoneInfo("America/New_York")


@pytest.mark.parametrize(
    ("cal", "day", "business", "closes"),
    [
        (SIFMA, date(2026, 11, 25), True, None),  # a regular Wednesday
        (SIFMA, date(2026, 11, 26), False, None),  # Thanksgiving Day
        (SIFMA, date(2026, 11, 27), True, datetime(2026, 11, 27, 14, tzinfo=NEW_YORK)),  # the day after
        (SIFMA, date(2026, 4, 3), True, datetime(2026, 4, 3, 12, tzinfo=NEW_YORK)),  # Good Friday, with the employment report
        (SIFMA, date(2027, 3, 26), False, None),  # Good Friday, without it
        (SIFMA, date(2026, 11, 28), False, None),  # a Saturday
        (SIFMA, date(2022, 1, 1), False, None),  # New Year's Day on a Saturday, which gives no Friday off
        (SIFMA, date(2021, 12, 31), True, datetime(2021, 12, 31, 14, tzinfo=NEW_YORK)),  # but closes early
        (TARGET2, date(2026, 12, 24), True, None),
        (TARGET2, date(2027, 12, 27), True, None),  # Christmas on a Saturday and 26 December on a Sunday give no day off
        (TARGET2, date(2026, 4, 6), False, None),  # Easter Monday
    ],
)
def test_a_day_is_a_business_day_or_not_and_closes_early_or_not(cal: Calendar, day: date, business: bool, closes: datetime | None) -> None:  # noqa: FBT001 -- a parametrized case
    assert cal.is_business_day(day) is business
    if business:
        assert cal.early_close(day) == closes


@pytest.mark.parametrize(
    ("cal", "day", "days", "expected"),
    [
        (SIFMA, date(2026, 11, 25), 1, date(2026, 11, 27)),  # over Thanksgiving, to its early close
        (SIFMA, date(2026, 11, 26), 1, date(2026, 11, 27)),  # from the holiday itself
        (SIFMA, date(2026, 11, 28), 1, date(2026, 11, 30)),  # from a Saturday
        (SIFMA, date(2026, 12, 23), 3, date(2026, 12, 29)),  # over Christmas and a weekend
        (SIFMA, date(2026, 11, 30), -1, date(2026, 11, 27)),
        (SIFMA, date(2026, 11, 27), -2, date(2026, 11, 24)),
        (SIFMA, date(2026, 11, 25), 0, date(2026, 11, 25)),
        (SIFMA, date(2027, 12, 30), 1, date(2027, 12, 31)),  # its last day
        (TARGET2, date(2026, 12, 24), 1, date(2026, 12, 28)),
    ],
)
def test_adding_business_days_counts_only_business_days(cal: Calendar, day: date, days: int, expected: date) -> None:
    assert cal.add(day, days) == expected


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 12, 28), date(2026, 12, 24)),  # a Monday after Christmas on the Friday
        (date(2026, 11, 26), date(2026, 11, 25)),  # a holiday's
        (date(2026, 11, 25), date(2026, 11, 24)),
    ],
)
def test_the_previous_business_day_is_the_last_before_the_day(day: date, expected: date) -> None:
    assert SIFMA.previous(day) == expected


@pytest.mark.parametrize(
    ("call", "says"),
    [
        (lambda: SIFMA.is_business_day(date(2028, 1, 3)), "sifma-us covers 1996-01-01 to 2027-12-31, and 2028-01-03 is after its last day"),
        (lambda: SIFMA.add(date(2027, 12, 31), 1), "2028-01-01 is after its last day"),  # the walk past its last day
        (lambda: SIFMA.previous(date(1996, 1, 2)), "1995-12-31 is before its first day"),
        (lambda: SIFMA.early_close(date(1995, 12, 29)), "1995-12-29 is before its first day"),
        (lambda: TARGET2.is_business_day(date(2100, 1, 4)), "target2 covers 2000-01-01 to 2099-12-31"),
        (lambda: SIFMA.add(date(2026, 11, 26), 0), "2026-11-26 is not a business day in sifma-us (Thanksgiving Day)"),
        (lambda: SIFMA.early_close(date(2026, 11, 26)), "closed all day on 2026-11-26 (Thanksgiving Day), so it has no early close"),
        (lambda: calendar("nyse"), "there is no calendar 'nyse': name one of sifma-us, target2"),
    ],
)
def test_a_day_it_does_not_cover_or_cannot_answer_for_is_an_error_with_its_fix(call: Callable[[], object], says: str) -> None:
    with pytest.raises(CalendarError, match=r": [a-z]") as raised:
        call()
    assert says in str(raised.value)
    assert isinstance(raised.value, BasisError)


@pytest.mark.parametrize(
    "call",
    [
        lambda: SIFMA.is_business_day(datetime(2026, 11, 25, 12, tzinfo=UTC)),  # a moment, whose day depends on its zone
        lambda: SIFMA.add(date(2026, 11, 25), 1.0),  # ty: ignore[invalid-argument-type] -- the float it refuses
        lambda: SIFMA.add(date(2026, 11, 25), True),  # noqa: FBT003 -- the bool it refuses
    ],
)
def test_it_refuses_a_moment_for_a_date_and_anything_but_whole_days(call: Callable[[], object]) -> None:
    with pytest.raises(TypeError, match=": "):
        call()


def test_its_reason_names_why_a_day_is_irregular() -> None:
    assert [SIFMA.reason(day) for day in (date(2026, 11, 26), date(2026, 11, 27), date(2026, 11, 28), date(2026, 11, 25))] == ["Thanksgiving Day", "After Thanksgiving Day", "Weekend", None]
    assert SIFMA.early_close(date(1999, 12, 31)) == datetime.combine(date(1999, 12, 31), time(13), NEW_YORK)
