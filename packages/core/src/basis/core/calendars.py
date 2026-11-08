"""Business-day calendars, as data in basis itself: which days a market settles, and when one closes early. The firm has
no official calendar of its own, so basis ships two, and code names the one it means, as `calendar("sifma-us")`:

| Name | Is | Covers | Early closes |
|---|---|---|---|
| `sifma-us` | SIFMA's recommended closes for the US bond market | 1996-01-01 to 2027-12-31 | 2 p.m. New York time, and noon on a Good Friday with the employment report |
| `target2` | The closing days of TARGET2, now T2, for euro settlement | 2000-01-01 to 2099-12-31 | none |

Each is its publisher's rules and the dated exceptions to them, the same rules and exceptions as basis-ui's calendars
(`@basis/ui/dates`), so a day is a business day in a job, an API and the UI alike, and checked as basis-ui's are,
against the schedules SIFMA published for 1996 to 2027 and the ECB's decisions. A calendar answers only for the days it
covers: any other is a `CalendarError`, never a business day by default, since a year SIFMA has yet to publish may
close on a day no rule foresees. SIFMA publishes each year's recommendations about a year ahead, and a basis release
that adds them moves `sifma-us`'s last date. The ECB fixed TARGET2's closing days in 2000, from 2002 "until further
notice", so `target2` takes them to the end of the century.

Saturdays and Sundays are closed in both. A date is a `datetime.date`, never a `datetime`, whose day depends on its
zone: give `moment.astimezone(cal.zone).date()`. The module reads no file, clock or setting, so job code may call it."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import cache
from typing import override
from zoneinfo import ZoneInfo

from basis.core.errors import BasisError

MONDAY = 0
"""`date.weekday()`'s days."""
THURSDAY = 3
SATURDAY = 5
SUNDAY = 6
DAY = timedelta(days=1)
WEEK = 7


class CalendarError(BasisError, ValueError):
    """A calendar asked about a day it does not cover, or a name that is not a calendar's; the message gives the fix."""


@dataclass(frozen=True)
class Irregular:
    """A day a calendar's rules or exceptions close, or close early at `closes`, in the calendar's zone."""

    reason: str
    closes: time | None
    """None: closed all day."""


@dataclass(frozen=True)
class Departure:
    """A day the publisher's schedule departs from its rules, and the notice that says so."""

    on: date
    day: Irregular | None
    """Closed, or closing early; None restores the regular session on a day the rules close or close early."""
    source: str


@dataclass(frozen=True, eq=False, repr=False)
class Calendar:
    """A market's business days between `first` and `last`, which a calendar's rules and exceptions give."""

    name: str
    title: str
    zone: ZoneInfo
    """The zone its early closes are in, and the zone a moment's date is taken in."""
    first: date
    last: date
    sources: tuple[str, ...]
    """The publications its rules and exceptions come from."""
    days: Mapping[date, Irregular]
    """Each weekday it closes or closes early, from `first` to `last`."""

    def is_business_day(self, day: date) -> bool:
        """Whether the market is open on `day`, all day or until an early close."""
        found = self._irregular(day)
        return found is None or found.closes is not None

    def add(self, day: date, days: int) -> date:
        """The business day `days` business days after `day`, or before it for a negative `days`, counting only business
        days after (or before) `day` itself, which need not be one: a Saturday plus 1 is the Monday, where that is a
        business day. `days` of 0 gives `day` back, which must then be a business day."""
        if isinstance(days, bool) or not isinstance(days, int):
            msg = f"{self.name}.add counts whole business days, not {days!r}: give an int, as add(day, 2) or add(day, -1)"
            raise TypeError(msg)
        if days == 0:
            if not self.is_business_day(day):
                msg = f"{day} is not a business day in {self.name} ({self.reason(day)}), so it is no business day 0 days on: add 1 for the next, or -1 for the previous"
                raise CalendarError(msg)
            return day
        step = DAY if days > 0 else -DAY
        left = abs(days)
        self._covered(day)
        while left:
            day += step
            if self.is_business_day(day):
                left -= 1
        return day

    def previous(self, day: date) -> date:
        """The last business day before `day`, which need not be one itself."""
        return self.add(day, -1)

    def early_close(self, day: date) -> datetime | None:
        """When the market closes on `day`, if it closes early, in the calendar's zone; None on a day of its regular
        session. A day it is closed all day has no close, and is a `CalendarError`: ask `is_business_day` first."""
        found = self._irregular(day)
        if found is None:
            return None
        if found.closes is None:
            msg = f"{self.name} is closed all day on {day} ({found.reason}), so it has no early close: ask is_business_day first"
            raise CalendarError(msg)
        return datetime.combine(day, found.closes, self.zone)

    def reason(self, day: date) -> str | None:
        """Why `day` is closed or closes early, as `Thanksgiving Day` or `Weekend`; None on a day of its regular session."""
        found = self._irregular(day)
        return None if found is None else found.reason

    def _irregular(self, day: date) -> Irregular | None:
        self._covered(day)
        if day.weekday() >= SATURDAY:
            return _WEEKEND
        return self.days.get(day)

    def _covered(self, day: date) -> None:
        if isinstance(day, datetime) or not isinstance(day, date):
            msg = f"{self.name} answers for a date, not {day!r}: give a moment's date in its zone, as moment.astimezone(cal.zone).date()"
            raise TypeError(msg)
        if day < self.first:
            msg = f"{self.name} covers {self.first} to {self.last}, and {day} is before its first day: keep to the days it covers"
            raise CalendarError(msg)
        if day > self.last:
            msg = (
                f"{self.name} covers {self.first} to {self.last}, and {day} is after its last day, which its publisher has yet to confirm: "
                f"keep to the days it covers, or upgrade basis once a release covers {day.year}"
            )
            raise CalendarError(msg)

    @override
    def __repr__(self) -> str:
        return f"calendar({self.name!r})"


_WEEKEND = Irregular("Weekend", None)


def calendar(name: str) -> Calendar:
    """The calendar `name` names: `sifma-us` or `target2`."""
    if name not in _CALENDARS:
        msg = f"there is no calendar {name!r}: name one of {', '.join(_CALENDARS)}"
        raise CalendarError(msg)
    return _built(name)


@cache
def _built(name: str) -> Calendar:
    return _CALENDARS[name]()


def _with_exceptions(days: dict[date, Irregular], exceptions: tuple[Departure, ...]) -> dict[date, Irregular]:
    """`days` as the exceptions change them: each wins over the rules on its day, and changes no other day."""
    for exception in exceptions:
        if exception.day is None:
            days.pop(exception.on, None)
        else:
            days[exception.on] = exception.day
    return days


# The date arithmetic holiday rules share. A rule gives a holiday's date in a year.

type Rule = Callable[[int], date]


def fixed(month: int, day: int) -> Rule:
    return lambda year: date(year, month, day)


def nth(weekday: int, month: int, n: int) -> Rule:
    """The `n`th `weekday` of `month`, or for a negative `n`, counting from the month's end, so -1 is its last."""

    def rule(year: int) -> date:
        if n > 0:
            first = date(year, month, 1)
            return first + timedelta(days=(weekday - first.weekday()) % WEEK + (n - 1) * WEEK)
        last = date(year + month // 12, month % 12 + 1, 1) - DAY
        return last - timedelta(days=(last.weekday() - weekday) % WEEK + (-n - 1) * WEEK)

    return rule


def from_easter(days: int) -> Rule:
    return lambda year: easter(year) + timedelta(days=days)


def easter(year: int) -> date:
    """Easter Sunday in the Gregorian calendar, by the anonymous Gregorian algorithm (Meeus, Jones and Butcher), as
    basis-ui's and parallax's calendars compute it."""
    golden = year % 19
    century, of_century = divmod(year, 100)
    leap_centuries, century_rest = divmod(century, 4)
    moon = (century - (century + 8) // 25 + 1) // 3
    epact = (19 * golden + century - leap_centuries - moon + 15) % 30
    weekday = (32 + 2 * century_rest + 2 * (of_century // 4) - epact - of_century % 4) % 7
    shift = (golden + 11 * epact + 22 * weekday) // 451
    month, day = divmod(epact + weekday - 7 * shift + 114, 31)
    return date(year, month, day + 1)


def business_day_before(day: date, days: Mapping[date, Irregular]) -> date:
    """The last weekday before `day` that `days` do not close."""
    day -= DAY
    while day.weekday() >= SATURDAY or ((found := days.get(day)) is not None and found.closes is None):
        day -= DAY
    return day


# SIFMA's recommendations for the US bond market: the trading of US dollar government, agency, mortgage- and
# asset-backed, corporate and municipal bonds and money market instruments. SIFMA recommends; each firm decides.
#
# Full closes: New Year's Day, Martin Luther King Jr. Day, Presidents Day, Good Friday, Memorial Day, Juneteenth (from
# 2022), Independence Day, Labor Day, Columbus Day, Veterans Day, Thanksgiving Day and Christmas Day. A holiday on a
# Sunday closes the Monday after. A holiday on a Saturday closes the Friday before, except New Year's Day, since that
# Friday ends the year's books, and Veterans Day: neither gives a day off.
#
# Good Friday is a full close unless the Bureau of Labor Statistics publishes the employment report that day. It
# publishes March's on the first Friday of April, so a Good Friday on 1 to 7 April usually has it, and SIFMA then
# recommends a noon close instead.
#
# Early closes, at 2 p.m. New York time: the business day before Good Friday (when Good Friday is a full close),
# Memorial Day, Independence Day, Christmas Day and New Year's Day, and the day after Thanksgiving. Until 2009-04-03
# SIFMA recommended twelve a year, adding the business days before Martin Luther King Jr. Day, Presidents Day, Labor
# Day, Columbus Day, Veterans Day and Thanksgiving Day. Its 2009 policy dropped those and the one before Independence
# Day, which its schedules have restored every year since 2012 but 2015.

type OnWeekend = Callable[[date], date | None]


@dataclass(frozen=True)
class Holiday:
    name: str
    rule: Rule
    on_weekend: OnWeekend | None = None
    """The day off a weekend date gives, or None for a holiday that never falls on one."""
    since: int | None = None
    """The first year SIFMA closes for it."""

    def day_off(self, year: int) -> date | None:
        if self.since is not None and year < self.since:
            return None
        day = self.rule(year)
        return day if self.on_weekend is None else self.on_weekend(day)


def friday_or_monday(day: date) -> date | None:
    """The Friday before a Saturday, and the Monday after a Sunday."""
    if day.weekday() == SATURDAY:
        return day - DAY
    return day + DAY if day.weekday() == SUNDAY else day


def monday_only(day: date) -> date | None:
    """The Monday after a Sunday, and no day off for a Saturday."""
    if day.weekday() == SATURDAY:
        return None
    return day + DAY if day.weekday() == SUNDAY else day


NEW_YEAR = Holiday("New Year's Day", fixed(1, 1), monday_only)
KING = Holiday("Martin Luther King Jr. Day", nth(MONDAY, 1, 3))
PRESIDENTS = Holiday("Presidents Day", nth(MONDAY, 2, 3))
GOOD_FRIDAY = Holiday("Good Friday", from_easter(-2))
MEMORIAL = Holiday("Memorial Day", nth(MONDAY, 5, -1))
JUNETEENTH = Holiday("Juneteenth", fixed(6, 19), friday_or_monday, since=2022)
INDEPENDENCE = Holiday("Independence Day", fixed(7, 4), friday_or_monday)
LABOR = Holiday("Labor Day", nth(MONDAY, 9, 1))
COLUMBUS = Holiday("Columbus Day", nth(MONDAY, 10, 2))
VETERANS = Holiday("Veterans Day", fixed(11, 11), monday_only)
THANKSGIVING = Holiday("Thanksgiving Day", nth(THURSDAY, 11, 4))
CHRISTMAS = Holiday("Christmas Day", fixed(12, 25), friday_or_monday)
SIFMA_HOLIDAYS = (NEW_YEAR, KING, PRESIDENTS, GOOD_FRIDAY, MEMORIAL, JUNETEENTH, INDEPENDENCE, LABOR, COLUMBUS, VETERANS, THANKSGIVING, CHRISTMAS)
SIFMA_EVES = frozenset({MEMORIAL, CHRISTMAS})
"""The holidays whose business day before closes early under every policy SIFMA has had."""
SIFMA_FORMER_EVES = frozenset({KING, PRESIDENTS, INDEPENDENCE, LABOR, COLUMBUS, VETERANS, THANKSGIVING})
"""The holidays whose business day before closed early only until 2009-04-03."""
FIVE_EARLY_CLOSES_FROM = date(2009, 4, 3)
INDEPENDENCE_EVE_FROM = 2012
EARLY = time(14)
EMPLOYMENT_REPORT_CLOSE = time(12)


def employment_report_on_good_friday(year: int) -> bool:
    """Whether the employment report is due on Good Friday, so that SIFMA recommends a noon close, not a full one."""
    good_friday = GOOD_FRIDAY.rule(year)
    return good_friday.month == 4 and good_friday.day <= WEEK  # noqa: PLR2004 -- April; March's report is published on the first Friday of April


def sifma_eve_closes_early(holiday: Holiday, eve: date) -> bool:
    """Whether the business day `eve` before `holiday` closes early, by the policy of its time."""
    if holiday in SIFMA_EVES or (holiday is GOOD_FRIDAY and not employment_report_on_good_friday(eve.year)):
        return True
    if holiday is INDEPENDENCE and eve.year >= INDEPENDENCE_EVE_FROM:
        return True
    return holiday in SIFMA_FORMER_EVES and eve < FIVE_EARLY_CLOSES_FROM


def sifma_year(year: int) -> dict[date, Irregular]:
    """One year's closes and early closes by SIFMA's rules, before the exceptions."""
    days: dict[date, Irregular] = {}
    report = employment_report_on_good_friday(year)
    for holiday in SIFMA_HOLIDAYS:
        if (day := holiday.day_off(year)) is not None and not (holiday is GOOD_FRIDAY and report):
            days[day] = Irregular(holiday.name, None)
    for holiday in SIFMA_HOLIDAYS:
        if (day := holiday.day_off(year)) is not None and sifma_eve_closes_early(holiday, eve := business_day_before(day, days)):
            days[eve] = Irregular(f"Before {holiday.name}", EARLY)
    if report:
        days[GOOD_FRIDAY.rule(year)] = Irregular("Good Friday, with the employment report", EMPLOYMENT_REPORT_CLOSE)
    days[business_day_before(date(year + 1, 1, 1), days)] = Irregular(f"Before {NEW_YEAR.name}", EARLY)
    days[THANKSGIVING.rule(year) + DAY] = Irregular(f"After {THANKSGIVING.name}", EARLY)
    return days


SIFMA_SCHEDULE = "https://www.sifma.org/resources/guides-playbooks/holiday-schedule"
SIFMA_ARCHIVE = "https://www.sifma.org/resources/guides-playbooks/us-holiday-archive"
SIFMA_HISTORY = "https://www.sifma.org/wp-content/uploads/2017/08/Misc-US-Historical-Holiday-Market-Recommendations-SIFMA.pdf"
SIFMA_POLICY = "https://www.sifma.org/news/press-releases/sifma-changes-early-close-recommendation-policy"
SIFMA_2007_2009 = "https://web.archive.org/web/20111105105514/http://www.sifma.org/services/holiday-schedule-2007-2009-archive/"


def _closed(on: str, reason: str, source: str) -> Departure:
    return Departure(date.fromisoformat(on), Irregular(reason, None), source)


def _early(on: str, closes: time, reason: str, source: str) -> Departure:
    return Departure(date.fromisoformat(on), Irregular(reason, closes), source)


def _open(on: str, source: str) -> Departure:
    return Departure(date.fromisoformat(on), None, source)


SIFMA_EXCEPTIONS = (
    _closed("1996-04-05", "Good Friday: a full close, although the employment report was published that day", SIFMA_HISTORY),
    _early("1996-07-05", EARLY, "After Independence Day", SIFMA_HISTORY),
    _open("1997-11-10", SIFMA_HISTORY),  # no early close before Veterans Day
    _early("1997-12-26", EARLY, "After Christmas Day", SIFMA_HISTORY),
    _open("1998-11-10", SIFMA_HISTORY),  # no early close before Veterans Day
    _early("1999-04-01", EARLY, "Before Good Friday, which closed at noon", SIFMA_HISTORY),
    _open("1999-11-10", SIFMA_HISTORY),  # no early close before Veterans Day
    _early("1999-12-31", time(13), "Before New Year's Day 2000, at 1 p.m.", SIFMA_HISTORY),
    _closed("2001-09-11", "The September 11 attacks", "https://archives.starbulletin.com/2001/09/13/business/market.html"),
    _closed("2001-09-12", "After the September 11 attacks", "https://www.emta.org/media/k30hlfnq/fd6ed6ea-6fc2-4d8e-9e8e-ea69f71dc58b.pdf"),
    _early("2001-09-13", EARLY, "Reopening after the September 11 attacks", "https://www.emta.org/media/ejpcombt/238d6277-6b45-49fd-a9fa-937168053ae1.pdf"),
    _early("2001-09-14", EARLY, "After the September 11 attacks", "https://www.emta.org/media/03ch0pcp/f6d79bd7-1779-4d24-b2e0-6ad5cb2ceafc.pdf"),
    _early("2001-09-17", EARLY, "After the September 11 attacks", "https://www.emta.org/media/kqsbiibl/b3659e87-4624-41d0-8ce4-c00b218804ff.pdf"),
    _early("2004-01-02", EARLY, "After New Year's Day", SIFMA_HISTORY),
    _closed("2004-06-11", "National Day of Mourning for President Reagan", "https://web.archive.org/web/2004/http://money.cnn.com/2004/06/07/markets/nyse_reagan/index.htm"),
    _open("2004-11-10", SIFMA_HISTORY),  # no early close before Veterans Day
    _early(
        "2007-01-02", EARLY, "National Day of Mourning for President Ford", "https://mondovisione.com/news/sifma-recommends-early-market-close-on-tuesday-january-2-2007-for-trading-of-us--2011124"
    ),
    _early("2007-04-06", time(11), "Good Friday, with the employment report, at 11 a.m.", SIFMA_2007_2009),
    _early("2012-10-29", time(12), "Hurricane Sandy", SIFMA_HISTORY),
    _closed("2012-10-30", "Hurricane Sandy", SIFMA_HISTORY),
    _open("2015-07-02", SIFMA_ARCHIVE),  # no early close before Independence Day
    _closed("2018-12-05", "National Day of Mourning for President George H. W. Bush", SIFMA_HISTORY),
    _early("2025-01-09", EARLY, "National Day of Mourning for President Carter", "https://www.sifma.org/resources/guides-playbooks/unscheduled-close-market-matrix"),
)
"""The days SIFMA's published recommendations depart from its rules: unscheduled closes, and the irregular early closes
of its earlier years."""


def _sifma_us() -> Calendar:
    first, last = date(1996, 1, 1), date(2027, 12, 31)
    days: dict[date, Irregular] = {}
    for year in range(first.year, last.year + 1):
        days |= sifma_year(year)
    sources = (SIFMA_SCHEDULE, SIFMA_ARCHIVE, SIFMA_HISTORY, SIFMA_POLICY, *(exception.source for exception in SIFMA_EXCEPTIONS))
    return Calendar(
        name="sifma-us",
        title="SIFMA's recommendations for the US bond market",
        zone=ZoneInfo("America/New_York"),
        first=first,
        last=last,
        sources=tuple(dict.fromkeys(sources)),
        days=_with_exceptions(days, SIFMA_EXCEPTIONS),
    )


# TARGET2, now T2, the Eurosystem's settlement system, whose closing days are the euro's non-settlement days. The ECB
# fixed them in December 2000 for 2002 onward, and 2000 already had the same six: New Year's Day, Good Friday, Easter
# Monday, 1 May, Christmas Day and 26 December, besides Saturdays and Sundays. A closing day on a weekend gives no day
# off, and TARGET2 has no early closes. 2001 also closed on 31 December, for the changeover to euro cash.

TARGET2_DAYS: tuple[tuple[Rule, str], ...] = (
    (fixed(1, 1), "New Year's Day"),
    (from_easter(-2), "Good Friday"),
    (from_easter(1), "Easter Monday"),
    (fixed(5, 1), "Labour Day"),
    (fixed(12, 25), "Christmas Day"),
    (fixed(12, 26), "26 December"),
)
TARGET2_2001 = "https://www.ecb.europa.eu/press/pr/date/2000/html/pr000525_2.en.html"
TARGET2_SOURCES = ("https://www.ecb.europa.eu/press/pr/date/2000/html/pr001214_4.en.html", TARGET2_2001, "https://www.ecb.europa.eu/paym/target/t2/html/index.en.html")


def _target2() -> Calendar:
    first, last = date(2000, 1, 1), date(2099, 12, 31)
    days = {rule(year): Irregular(reason, None) for year in range(first.year, last.year + 1) for rule, reason in TARGET2_DAYS}
    return Calendar(
        name="target2",
        title="TARGET2's closing days, for euro settlement",
        zone=ZoneInfo("Europe/Berlin"),
        first=first,
        last=last,
        sources=TARGET2_SOURCES,
        days=_with_exceptions(days, (_closed("2001-12-31", "The changeover to euro cash", TARGET2_2001),)),
    )


_CALENDARS: dict[str, Callable[[], Calendar]] = {"sifma-us": _sifma_us, "target2": _target2}
