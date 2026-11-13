"""Filters, whose state is the URL's: a page's filters are read from its query string when the page is built, and
written back to it in the browser as each changes, with no request of their own, so a link reproduces a view.

    page = bd.page("/", "Positions", filters=[bd.as_of("as-of", calendar="sifma-us"), bd.select("book", "Book", options=BOOKS)], body=[...])

    @callback(Output("positions", "rowData"), Input("as-of", "value"), Input("book", "value"))
    async def show(as_of: str, book: str | None) -> bd.Rows:
        frame = await bd.load(POSITIONS, as_of=bd.day(as_of))
        ...

- `bd.as_of(id, calendar=)`: one business day of the calendar, starting on the previous one, today's excluded, every
  other day disabled in the picker; `bd.day(value)` reads it.
- `bd.date_range(id, presets=)`: a first and last day, with presets for the month, quarter and year to date;
  `bd.days(value)` reads it.
- `bd.select(id, label, options=, multiple=)`: one value of a list, or several; `bd.options(frame, column)` gives a
  column's values as its options, for a callback to set.
- `bd.search(id)`: text, which the page's callbacks match as they see fit.

A filter's key in the query string is its id: `?as-of=2026-11-12&book=Rates`. A value the URL gives that the filter
cannot take, such as a closed day, is dropped for the filter's default, never shown as if it were valid."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Final, Literal, Protocol

import dash_mantine_components as dmc
import pandas as pd

from basis.core.calendars import Calendar, CalendarError
from basis.core.calendars import calendar as named_calendar
from basis.dash.errors import DeclarationError
from basis.dash.icons import icon
from dash.development.base_component import Component

type Preset = Literal["mtd", "qtd", "ytd", "1m", "3m", "1y"]
type Query = Mapping[str, object]
"""A page's query string, as Dash Pages gives a layout: each key's text, or a list of them where it repeats."""

RANGE: Final = ".."
"""What joins a range's first and last day in the query string: `2026-10-01..2026-11-12`."""
LISTED: Final = ","
"""What joins a multiple select's values in the query string."""
HISTORY_YEARS: Final = 2
"""How far back an as-of picker offers days, in years."""
PRESETS: Final[dict[Preset, str]] = {"mtd": "Month to date", "qtd": "Quarter to date", "ytd": "Year to date", "1m": "One month", "3m": "Three months", "1y": "One year"}
SIZE: Final = "xs"
"""Every filter's size, as dense as the grid."""


class Filter(Protocol):
    """A filter of a page: its component's id and the property it holds, and the component for a query string."""

    @property
    def id(self) -> str: ...

    @property
    def prop(self) -> str: ...

    def component(self, query: Query) -> Component: ...


@dataclass(frozen=True)
class AsOf:
    id: str
    label: str
    calendar: str | None
    prop: str = field(default="value", init=False)

    def component(self, query: Query) -> Component:
        known = named_calendar(self.calendar) if self.calendar is not None else None
        latest = _previous_day(known, _today(known))
        earliest = _earliest(known, latest)
        given = _one(query.get(self.id))
        value = given if given is not None and _open(known, given, earliest, latest) else latest.isoformat()
        closed = [day.isoformat() for day in _span(earliest, latest) if not _is_open(known, day)]
        return dmc.DatePickerInput(
            id=self.id,
            label=self.label,
            value=value,
            minDate=earliest.isoformat(),
            maxDate=latest.isoformat(),
            disabledDates=closed,
            valueFormat="YYYY-MM-DD",
            firstDayOfWeek=1,
            highlightToday=True,
            size=SIZE,
            w=138,
            leftSection=icon("calendar"),
            className="basis-filter",
        )


@dataclass(frozen=True)
class DateRange:
    id: str
    label: str
    presets: tuple[Preset, ...]
    calendar: str | None
    default: Preset
    prop: str = field(default="value", init=False)

    def component(self, query: Query) -> Component:
        known = named_calendar(self.calendar) if self.calendar is not None else None
        latest = _previous_day(known, _today(known))
        offered = {name: preset(name, latest, known) for name in self.presets}
        earliest = _earliest(known, latest)
        given = _range(_one(query.get(self.id)))
        first, last = given if given is not None and earliest <= given[0] <= given[1] <= latest else preset(self.default, latest, known)
        closed = [day.isoformat() for day in _span(earliest, latest) if not _is_open(known, day)] if known is not None else []
        return dmc.DatePickerInput(
            id=self.id,
            label=self.label,
            type="range",
            value=[first.isoformat(), last.isoformat()],
            minDate=earliest.isoformat(),
            maxDate=latest.isoformat(),
            disabledDates=closed,
            presets=[{"value": [start.isoformat(), end.isoformat()], "label": PRESETS[name]} for name, (start, end) in offered.items()],
            valueFormat="YYYY-MM-DD",
            firstDayOfWeek=1,
            allowSingleDateInRange=True,
            size=SIZE,
            w=228,
            leftSection=icon("calendar"),
            className="basis-filter",
        )


@dataclass(frozen=True)
class Select:
    id: str
    label: str
    options: tuple[str, ...]
    multiple: bool
    placeholder: str
    default: str | tuple[str, ...] | None
    prop: str = field(default="value", init=False)

    def component(self, query: Query) -> Component:
        given = _one(query.get(self.id))
        shared = {"id": self.id, "label": self.label, "data": list(self.options), "placeholder": self.placeholder, "searchable": True, "clearable": True, "size": SIZE, "className": "basis-filter"}
        if self.multiple:
            chosen = [value for value in (given.split(LISTED) if given else []) if value in self.options] if given is not None else list(self.default or ())
            return dmc.MultiSelect(value=chosen, w=220, **shared)  # ty: ignore[invalid-argument-type] -- the props both kinds share
        value = given if given is not None and given in self.options else (self.default if isinstance(self.default, str) else None)
        return dmc.Select(value=value, w=168, **shared)  # ty: ignore[invalid-argument-type] -- likewise


@dataclass(frozen=True)
class Search:
    id: str
    label: str
    placeholder: str
    prop: str = field(default="value", init=False)

    def component(self, query: Query) -> Component:
        given = _one(query.get(self.id))
        return dmc.TextInput(id=self.id, label=self.label, value=given or "", placeholder=self.placeholder, debounce=300, size=SIZE, w=220, leftSection=icon("search"), className="basis-filter")


def as_of(id: str, label: str = "As of", *, calendar: str | None = None) -> AsOf:  # noqa: A002 -- a component's id, as Dash names it
    """One business day of `calendar`, by default the previous one; every day, weekends aside, where it names none."""
    _named(id, label)
    if calendar is not None:
        named_calendar(calendar)  # a name that is no calendar's fails here, as the page is declared
    return AsOf(id, label, calendar)


def date_range(id: str, label: str = "Dates", *, presets: Sequence[Preset] = ("mtd", "qtd", "ytd"), default: Preset = "mtd", calendar: str | None = None) -> DateRange:  # noqa: A002 -- likewise
    """A first and a last day, at most the previous business day, with `presets` to pick from, starting on `default`."""
    _named(id, label)
    if unknown := [name for name in (*presets, default) if name not in PRESETS]:
        msg = f"a date range's presets are {', '.join(PRESETS)}, not {', '.join(map(repr, unknown))}: give some of them"
        raise DeclarationError(msg)
    if calendar is not None:
        named_calendar(calendar)
    return DateRange(id, label, tuple(presets), calendar, default)


def select(
    id: str,  # noqa: A002 -- likewise
    label: str,
    *,
    options: Sequence[str] = (),
    multiple: bool = False,
    placeholder: str = "All",
    default: str | Sequence[str] | None = None,
) -> Select:
    """A choice of `options`, one or, where `multiple`, several, none meaning all; a callback may set its `data` from a
    dataset, by `bd.options(frame, column)`."""
    _named(id, label)
    shown = tuple(str(option) for option in options)
    if multiple and isinstance(default, str):
        msg = f"filter {id} takes several values, so its default is a list of them, not {default!r}"
        raise DeclarationError(msg)
    chosen = default if default is None or isinstance(default, str) else tuple(default)
    return Select(id, label, shown, multiple, placeholder, chosen)


def search(id: str, label: str = "Search", *, placeholder: str = "CUSIP, issuer or name") -> Search:  # noqa: A002 -- likewise
    """A text to match, sent once typing pauses."""
    _named(id, label)
    return Search(id, label, placeholder)


def options(frame: pd.DataFrame, column: str) -> list[str]:
    """The distinct values of `frame`'s `column`, as text, sorted, missing ones left out: a select's options."""
    if column not in frame:
        msg = f"the frame has no column {column!r} to take options from: name one of {', '.join(map(str, frame.columns))}"
        raise DeclarationError(msg)
    return sorted({str(value) for value in frame[column].dropna()})


def day(value: object) -> date:
    """An as-of filter's value as a date."""
    if not isinstance(value, str):
        msg = f"an as-of filter's value is a date's ISO text, not {value!r}"
        raise TypeError(msg)
    return date.fromisoformat(value[:10])


def days(value: object) -> tuple[date, date]:
    """A date range's value as its first and last day."""
    if not (isinstance(value, list | tuple) and len(value) == 2 and all(isinstance(part, str) and part for part in value)):  # noqa: PLR2004 -- a first and a last
        msg = f"a date range's value is its first and last days' ISO text, not {value!r}"
        raise TypeError(msg)
    return day(value[0]), day(value[1])


def preset(name: Preset, latest: date, known: Calendar | None) -> tuple[date, date]:
    """Preset `name`'s first and last day, ending on `latest`: its first business day of the month, quarter or year,
    or of the month, three months or year before."""
    match name:
        case "mtd":
            start = latest.replace(day=1)
        case "qtd":
            start = latest.replace(month=(latest.month - 1) // 3 * 3 + 1, day=1)
        case "ytd":
            start = latest.replace(month=1, day=1)
        case "1m":
            start = _months_back(latest, 1) + timedelta(days=1)
        case "3m":
            start = _months_back(latest, 3) + timedelta(days=1)
        case "1y":
            start = _months_back(latest, 12) + timedelta(days=1)
    while not _is_open(known, start) and start < latest:
        start += timedelta(days=1)
    return start, latest


def filter_of(given: object) -> Filter | None:
    """`given` as a filter, where it is one of these."""
    return given if isinstance(given, AsOf | DateRange | Select | Search) else None


def _named(id: str, label: str) -> None:  # noqa: A002 -- the filter's id
    if not id or any(letter in id for letter in "&=?#,. "):
        msg = f"a filter's id is its key in the URL, so it holds no space and none of & = ? # , . : give one, as as-of, not {id!r}"
        raise DeclarationError(msg)
    if not label.strip():
        msg = f"filter {id} has no label: give it the words a person reads above it"
        raise DeclarationError(msg)


def _today(known: Calendar | None) -> date:
    return datetime.now(known.zone).date() if known is not None else datetime.now().astimezone().date()


def _is_open(known: Calendar | None, day_: date) -> bool:
    if known is None:
        return day_.weekday() < 5  # noqa: PLR2004 -- Monday to Friday
    try:
        return known.is_business_day(day_)
    except CalendarError:
        return False


def _previous_day(known: Calendar | None, today: date) -> date:
    found = today - timedelta(days=1)
    while not _is_open(known, found):
        found -= timedelta(days=1)
    return found


def _earliest(known: Calendar | None, latest: date) -> date:
    earliest = latest.replace(year=latest.year - HISTORY_YEARS) if not (latest.month == 2 and latest.day == 29) else latest.replace(year=latest.year - HISTORY_YEARS, day=28)  # noqa: PLR2004 -- February's leap day
    while not _is_open(known, earliest) and earliest < latest:
        earliest += timedelta(days=1)
    return earliest


def _open(known: Calendar | None, given: str, earliest: date, latest: date) -> bool:
    try:
        found = date.fromisoformat(given)
    except ValueError:
        return False
    return earliest <= found <= latest and _is_open(known, found)


def _range(given: str | None) -> tuple[date, date] | None:
    if given is None or RANGE not in given:
        return None
    first, _, last = given.partition(RANGE)
    try:
        return date.fromisoformat(first), date.fromisoformat(last)
    except ValueError:
        return None


def _one(given: object) -> str | None:
    if isinstance(given, list):
        given = given[-1] if given else None
    return given if isinstance(given, str) and given else None


def _span(first: date, last: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def _months_back(day_: date, months: int) -> date:
    month = day_.month - months
    year = day_.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    for last in (31, 30, 29, 28):
        try:
            return day_.replace(year=year, month=month, day=min(day_.day, last))
        except ValueError:
            continue
    return day_.replace(year=year, month=month, day=28)
