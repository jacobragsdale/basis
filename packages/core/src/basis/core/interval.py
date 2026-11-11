"""The run's interval: the span of time a run is for, half-open, in UTC. A job that has no notion of one runs without
it. A query binds it as `interval_start` and `interval_end`, naive UTC datetimes (Oracle reserves `START`, so the
names are long); a date compares with them as its midnight.

`basis run --interval` takes an interval as dates or instants, `2026-11-05..2026-11-06`, or relative to the moment
the run starts, for a scheduler whose command never changes (`given`). A relative interval resolves in the zone and
the business days of the calendar the job declares (`CALENDAR = etl.calendar("sifma-us")`), and to dates, as one
written by hand would be, so the run's manifest records the dates it ran for, and a replay or a regress of it is exact:

| Given | Resolves to, on Tuesday 2026-11-10 in the calendar's zone |
|---|---|
| `today` | `2026-11-10..2026-11-11` |
| `yesterday` | `2026-11-09..2026-11-10` |
| `previous-business-day` | the business day before today: `2026-11-09..2026-11-10`; on a Monday, the Friday before, unless it was a holiday |
| `previous-month` | `2026-10-01..2026-11-01` |
| `today-7d..today`: each end `today`, `today-Nd` (N days before) or `today-Nbd` (N business days before), or a date | `2026-11-03..2026-11-10` |
| `today-1bd..today` | `2026-11-09..2026-11-10` |

A business day before today is counted from today, which need not be one: on a Saturday, `today-1bd` is the Friday."""

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import override
from zoneinfo import ZoneInfo

from basis.core.calendars import Calendar
from basis.core.errors import BasisError


class IntervalError(BasisError, ValueError):
    """A relative interval a run cannot resolve; the message gives the fix."""


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime
    zone: str | None = field(default=None, compare=False)
    """The zone its binds are in, as naive times there (`in_zone`): None for naive UTC."""

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            msg = f"an interval's ends are instants: give {self.start} and {self.end} a zone"
            raise ValueError(msg)
        object.__setattr__(self, "start", self.start.astimezone(UTC))
        object.__setattr__(self, "end", self.end.astimezone(UTC))
        if self.start >= self.end:
            msg = f"an interval starts before it ends: {self}"
            raise ValueError(msg)

    @classmethod
    def parse(cls, text: str) -> Interval:
        """`START..END`, each a date, meaning its midnight UTC, or an ISO datetime with an offset, such as
        `2026-11-05..2026-11-06` or `2026-11-05T06:00Z..2026-11-05T07:00Z`."""
        start, separator, end = text.partition("..")
        if not separator:
            msg = f"an interval is START..END, as 2026-11-05..2026-11-06, not {text!r}"
            raise ValueError(msg)
        return cls(_instant(start), _instant(end))

    def binds(self) -> dict[str, datetime]:
        """The interval as query binds: naive UTC, which every driver binds without a zone conversion of its own, or,
        `in_zone`, naive times in that zone, as a column of them compares; an end whose time there names two instants
        is refused, since the query would select a different span than the run's."""
        if self.zone is None:
            return {"interval_start": self.start.replace(tzinfo=None), "interval_end": self.end.replace(tzinfo=None)}
        if (unsound := self.first_unsound()) is not None:
            raise IntervalError(unsound)
        zone = ZoneInfo(self.zone)
        return {"interval_start": self.start.astimezone(zone).replace(tzinfo=None), "interval_end": self.end.astimezone(zone).replace(tzinfo=None)}

    def in_zone(self, zone: str) -> Interval:
        """The same interval, binding its ends as naive times in `zone`."""
        return replace(self, zone=zone)

    def first_unsound(self) -> str | None:
        """Why the interval's ends, as naive times in its zone, would not mark out the same span, or None: an end whose
        naive time the zone shows twice, as its clocks go back, but for the instant the repeated hour begins."""
        if self.zone is None:
            return None
        for name, moment in (("start", self.start), ("end", self.end)):
            if (local := unsound_end(moment, self.zone)) is not None:
                return (
                    f"the interval's {name}, {moment}, is {local:%H:%M} in {self.zone}, which shows that time twice as its clocks go back:"
                    " start and end the interval outside that hour, or compare the column in UTC"
                )
        return None

    @override
    def __str__(self) -> str:
        return f"{_text(self.start)}..{_text(self.end)}"


NAMED = ("today", "yesterday", "previous-business-day", "previous-month")
"""The relative intervals that are one period: a day, or a month."""
_RELATIVE_END = re.compile(r"today(?:-(?P<count>\d+)(?P<unit>d|bd))?")
_DAY = timedelta(days=1)


@dataclass(frozen=True)
class Relative:
    """An interval relative to the moment a run starts, as `basis run --interval` is given it, such as
    `previous-business-day`, which a run resolves against its job's calendar."""

    text: str

    def resolve(self, now: datetime, calendar: Calendar | None, *, job: str) -> Interval:
        """The interval this names at `now`, in `calendar`'s zone and business days: `job`'s, which must declare one."""
        if calendar is None:
            msg = f'{job} declares no calendar, which --interval {self.text} is resolved in: declare one in the job\'s module, as CALENDAR = etl.calendar("sifma-us"), or give dates'
            raise IntervalError(msg)
        today = now.astimezone(calendar.zone).date()
        if self.text == "today":
            return _days(today, today + _DAY)
        if self.text == "yesterday":
            return _days(today - _DAY, today)
        if self.text == "previous-business-day":
            day = calendar.previous(today)
            return _days(day, day + _DAY)
        if self.text == "previous-month":
            first = today.replace(day=1)
            return _days((first - _DAY).replace(day=1), first)
        start, _, end = self.text.partition("..")
        first, last = _end(start, today, calendar), _end(end, today, calendar)
        if first >= last:  # business days and calendar days mixed, which only the day can judge
            msg = f"--interval {self.text} resolves to {_text(first)}..{_text(last)} on {today}, which does not start before it ends: give ends that do, as today-Nbd..today"
            raise IntervalError(msg)
        return Interval(first, last)

    @override
    def __str__(self) -> str:
        return self.text


def given(text: str) -> Interval | Relative:
    """`text` as `basis run --interval` takes it: dates or instants (`Interval.parse`), or relative to the run's start, a
    `Relative`, checked here and resolved when the run starts."""
    text = text.strip()
    if text in NAMED:
        return Relative(text)
    start, separator, end = (part.strip() for part in text.partition(".."))
    if not separator:
        msg = f"an interval is START..END, as 2026-11-05..2026-11-06 or today-7d..today, or one of {', '.join(NAMED)}, not {text!r}"
        raise ValueError(msg)
    if not (_RELATIVE_END.fullmatch(start) or _RELATIVE_END.fullmatch(end)):
        return Interval.parse(text)
    for found in (start, end):
        if not _RELATIVE_END.fullmatch(found):
            try:
                _instant(found)  # the other end, a date or an instant, checked now rather than when the run starts
            except ValueError as error:
                msg = f"{found!r} is not a date, an instant with its offset, or today, today-Nd or today-Nbd ({error})"
                raise ValueError(msg) from None
        elif (relative := _RELATIVE_END.fullmatch(found)) is not None and relative["count"] is not None and int(relative["count"]) == 0:
            msg = f"{found} is today: write today"
            raise IntervalError(msg)
    _refuse_backwards(start, end)
    return Relative(f"{start}..{end}")


def _refuse_backwards(start: str, end: str) -> None:
    """Refuse a relative interval that ends where it starts, or before, where the text alone says so: both ends in
    calendar days, or both in business days, counted back from today."""
    first, last = _RELATIVE_END.fullmatch(start), _RELATIVE_END.fullmatch(end)
    if first is None or last is None:
        return
    units = {first["unit"] or "", last["unit"] or ""} - {""}
    if len(units) > 1:
        return  # business days against calendar days, which only the day can judge
    back_start, back_end = int(first["count"] or 0), int(last["count"] or 0)
    if back_start == back_end:
        msg = (
            f"{start}..{end} ends where it starts, so it holds no time: for today, give today"
            if back_start == 0
            else f"{start}..{end} ends where it starts, so it holds no time: give an earlier start"
        )
        raise IntervalError(msg)
    if back_start < back_end:
        msg = f"{start}..{end} ends before it starts: give the earlier end first, as {end}..{start}"
        raise IntervalError(msg)


def _end(text: str, today: date, calendar: Calendar) -> datetime:
    found = _RELATIVE_END.fullmatch(text)
    if found is None:
        return _instant(text)
    count = int(found["count"] or 0)
    day = calendar.add(today, -count) if found["unit"] == "bd" and count else today - count * _DAY
    return datetime.combine(day, time(), UTC)


def _days(start: date, end: date) -> Interval:
    return Interval(datetime.combine(start, time(), UTC), datetime.combine(end, time(), UTC))


def _instant(text: str) -> datetime:
    text = text.strip()
    if len(text) == len("2026-11-05"):
        return datetime.combine(date.fromisoformat(text), time(), UTC)
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        msg = f"{text!r} has no offset: write it as {text}Z for UTC, or with its offset"
        raise ValueError(msg)
    return moment


def _text(moment: datetime) -> str:
    return moment.date().isoformat() if moment.time() == time() else moment.isoformat().replace("+00:00", "Z")


def names_two(naive: datetime, zone: ZoneInfo) -> bool:
    """Whether `zone` shows the naive time `naive` twice, as its clocks go back."""
    return naive.replace(tzinfo=zone, fold=0).utcoffset() != naive.replace(tzinfo=zone, fold=1).utcoffset()


def unsound_end(moment: datetime, zone: str) -> datetime | None:
    """`moment`'s naive time in `zone`, where a range of naive times ending or starting there would not mark out the
    same instants: a time the zone shows twice, unless `moment` is the first instant of that repeated hour, which a
    range of naive times starts or ends at exactly. None where it is sound."""
    found = ZoneInfo(zone)
    local = moment.astimezone(found).replace(tzinfo=None)
    if not names_two(local, found):
        return None
    first = local.replace(tzinfo=found, fold=0).astimezone(UTC) == moment.astimezone(UTC)
    return None if first and not names_two(local - timedelta(microseconds=1), found) else local
