"""Filters and the URL: each filter built from a page's query string, a value it cannot take dropped for its default,
the as-of picker's closed days, the range's presets, the values a callback reads, the refusals, and the browser's
writing of each value back to the query string."""

import json
import os
import shutil
import subprocess
from datetime import date, timedelta
from typing import Any

import pandas as pd
import pytest
from dash import Input, Output, callback, html

import basis.dash as bd
from basis.core.calendars import calendar
from basis.dash import filters, shell, testing
from basis.dash.errors import DeclarationError
from basis.dash.theme import STATIC

SIFMA = calendar("sifma-us")
AS_OF = bd.as_of("filter-as-of", calendar="sifma-us")
DATES = bd.date_range("filter-dates", calendar="sifma-us")
BOOK = bd.select("filter-book", "Book", options=["Rates", "Credit"], multiple=True)
SECTOR = bd.select("filter-sector", "Sector", options=["Treasury", "Agency"], default="Treasury")
SEARCH = bd.search("filter-q")


class Settings(bd.Settings):
    pass


@callback(Output("filter-echo", "children"), Input("filter-as-of", "value"), Input("filter-book", "value"))
async def echo(as_of: str, book: list[str]) -> str:
    return f"{bd.day(as_of)} {book}"


PAGE = bd.page("/", "Positions", filters=[AS_OF, DATES, BOOK, SECTOR, SEARCH], body=[html.Div(id="filter-echo")])
OTHER = bd.page("/other", "Other", filters=[AS_OF], body=[html.Div()])
APP = bd.App(name="filtered", title="Filtered", settings=Settings, pages=[PAGE, OTHER])


def _props(made: Any) -> Any:  # noqa: ANN401 -- a component's props, JSON of any shape
    return json.loads(json.dumps(made.to_plotly_json(), default=lambda found: found.to_plotly_json()))["props"]


def _json(found: object) -> Any:  # noqa: ANN401 -- JSON of any shape
    return json.loads(json.dumps(found))


def _latest() -> date:
    found = filters._today(SIFMA) - timedelta(days=1)  # noqa: SLF001 -- the day the filter starts from
    while not SIFMA.is_business_day(found):
        found -= timedelta(days=1)
    return found


def test_as_of_starts_on_the_previous_business_day_and_closes_every_other() -> None:
    shown = _props(AS_OF.component({}))
    latest = _latest()
    assert shown["value"] == shown["maxDate"] == latest.isoformat()
    closed = {date.fromisoformat(day) for day in shown["disabledDates"]}
    window = [date.fromisoformat(shown["minDate"]) + timedelta(days=offset) for offset in range((latest - date.fromisoformat(shown["minDate"])).days + 1)]
    assert closed == {day for day in window if not SIFMA.is_business_day(day)}
    assert any(day.weekday() < 5 for day in closed)  # a weekday holiday, such as Christmas, among the weekends
    assert (shown["valueFormat"], shown["label"]) == ("YYYY-MM-DD", "As of")


def _open_before(days: int) -> date:
    return SIFMA.add(_latest(), -days)


@pytest.mark.parametrize(("given", "kept"), [("open", True), ("closed", False), ("future", False), ("1999-01-04", False), ("soon", False), ("listed", True)])
def test_as_of_takes_an_open_day_from_the_url_and_drops_any_other(given: str, kept: bool) -> None:  # noqa: FBT001 -- a parameter of the case
    recent = _open_before(2)
    weekend = next(recent - timedelta(days=offset) for offset in range(1, 8) if (recent - timedelta(days=offset)).weekday() == 6)
    value = {"open": recent.isoformat(), "closed": weekend.isoformat(), "future": (_latest() + timedelta(days=7)).isoformat(), "listed": [weekend.isoformat(), recent.isoformat()]}.get(given, given)
    shown = _props(AS_OF.component({"filter-as-of": value}))
    assert shown["value"] == (recent.isoformat() if kept else _latest().isoformat())


def test_a_range_takes_its_days_from_the_url_and_offers_its_presets() -> None:
    first, last = _open_before(30), _open_before(2)
    shown = _props(DATES.component({"filter-dates": f"{first}..{last}"}))
    assert shown["value"] == [first.isoformat(), last.isoformat()]
    assert [preset["label"] for preset in shown["presets"]] == ["Month to date", "Quarter to date", "Year to date"]
    latest = _latest()
    assert all(preset["value"][1] == latest.isoformat() for preset in shown["presets"])
    assert all(not SIFMA.is_business_day(date.fromisoformat(day)) for day in shown["disabledDates"])  # a range starts and ends on open days
    backwards = _props(DATES.component({"filter-dates": f"{last}..{first}"}))
    assert backwards["value"] == [part.isoformat() for part in filters.preset("mtd", latest, SIFMA)]


@pytest.mark.parametrize(
    ("name", "latest", "first"),
    [("mtd", date(2026, 11, 12), date(2026, 11, 2)), ("qtd", date(2026, 11, 12), date(2026, 10, 1)), ("ytd", date(2026, 11, 12), date(2026, 1, 2)), ("1m", date(2026, 3, 31), date(2026, 3, 2))],
)
def test_a_preset_starts_on_its_first_business_day(name: filters.Preset, latest: date, first: date) -> None:
    assert filters.preset(name, latest, SIFMA) == (first, latest)


def test_a_select_takes_known_values_from_the_url() -> None:
    assert _props(BOOK.component({"filter-book": "Rates,Bogus,Credit"}))["value"] == ["Rates", "Credit"]
    assert _props(BOOK.component({}))["value"] == []
    assert _props(SECTOR.component({"filter-sector": "Agency"}))["value"] == "Agency"
    assert _props(SECTOR.component({"filter-sector": "Bogus"}))["value"] == "Treasury"  # the default, never a value it does not offer
    assert _props(SEARCH.component({"filter-q": "912"}))["value"] == "912"


def test_a_callback_reads_a_filter_s_value() -> None:
    assert bd.day("2026-11-12") == date(2026, 11, 12)
    assert bd.days(["2026-10-01", "2026-11-12"]) == (date(2026, 10, 1), date(2026, 11, 12))
    with pytest.raises(TypeError, match="first and last days"):
        bd.days(["2026-10-01", None])
    assert bd.options(pd.DataFrame({"book": ["Rates", None, "Credit", "Rates"]}), "book") == ["Credit", "Rates"]


@pytest.mark.parametrize(
    ("made", "refused"),
    [
        (lambda: bd.as_of("as of"), "holds no space"),
        (lambda: bd.as_of("as-of", calendar="nyse"), "nyse"),
        (lambda: bd.select("book", " "), "has no label"),
        (lambda: bd.select("book", "Book", multiple=True, default="Rates"), "its default is a list"),
        (lambda: bd.date_range("dates", presets=["wtd"]), "'wtd'"),  # ty: ignore[invalid-argument-type] -- the refused preset
    ],
)
def test_a_filter_is_refused_where_the_url_or_the_page_could_not_hold_it(made: Any, refused: str) -> None:  # noqa: ANN401 -- each case's declaration
    with pytest.raises((DeclarationError, ValueError), match=refused):
        made()


def test_a_page_builds_its_filters_from_its_query_string() -> None:
    with testing.client(APP) as client:
        shown = client.page("/")
        router = next(entry for entry in _json(client.dependencies()) if testing.ROUTER in str(entry["output"]))
        inputs = {f"{given['id']}.{given['property']}": {"pathname": "/", "search": "?filter-book=Credit"}.get(given["property"]) for given in router["inputs"]}
        queried = client.fire(str(router["output"]), inputs, state=dict.fromkeys(f"{given['id']}.{given['property']}" for given in router["state"]))
    sent = json.dumps(shown)
    for name in ("filter-as-of", "filter-dates", "filter-book", "filter-sector", "filter-q"):
        assert f'"id": "{name}"' in sent
    assert '"value": ["Credit"]' in json.dumps(queried)


def test_each_filter_writes_the_url_in_the_browser_once() -> None:
    assert [found.id for found in PAGE.filters] == ["filter-as-of", "filter-dates", "filter-book", "filter-sector", "filter-q"]
    with testing.client(APP) as client:
        written = [entry for entry in _json(client.dependencies()) if shell.URL in str(entry["output"])]
    assert sorted(entry["inputs"][0]["id"] for entry in written) == ["filter-as-of", "filter-book", "filter-dates", "filter-q", "filter-sector"]  # the as-of both pages share, once
    assert {json.dumps(entry["clientside_function"]) for entry in written} == {json.dumps({"namespace": "basis", "function_name": "remember"})}
    assert all(entry["prevent_initial_call"] for entry in written)


@pytest.mark.skipif(shutil.which("node") is None and "CI" not in os.environ, reason="the browser script runs under node, which this machine lacks; CI has it")
def test_the_browser_writes_each_value_to_the_query_string_in_place() -> None:
    probe = """
    const SCRIPT = require("fs").readFileSync(process.argv[1], "utf8");
    const written = [];
    globalThis.window = {
      location: {href: "http://desk/apps/pos/?filter-sector=Agency&keep=1"},
      history: {state: {s: 1}, replaceState: (state, title, href) => { written.push(href); window.location.href = href; }},
      dash_clientside: {no_update: "NO"},
    };
    eval(SCRIPT);
    const set = (prop_id, value) => { window.dash_clientside.callback_context = {triggered: [{prop_id, value}]}; return window.dash_clientside.basis.remember(); };
    const answers = [
      set("filter-as-of.value", "2026-11-10"),
      set("filter-book.value", ["Rates", "Credit"]),
      set("filter-dates.value", ["2026-10-01", null]),
      set("filter-dates.value", ["2026-10-01", "2026-11-10"]),
      set("filter-sector.value", null),
      set("filter-q.value", ""),
    ];
    console.log(JSON.stringify({answers, written}));
    """
    ran = subprocess.run(["node", "-e", probe, str(STATIC / "basis.js")], capture_output=True, text=True, encoding="utf-8", check=True, timeout=30)  # noqa: S607 -- node, on the path
    found = json.loads(ran.stdout)
    assert set(found["answers"]) == {"NO"}  # no request of its own
    assert found["written"] == [
        "http://desk/apps/pos/?filter-sector=Agency&keep=1&filter-as-of=2026-11-10",
        "http://desk/apps/pos/?filter-sector=Agency&keep=1&filter-as-of=2026-11-10&filter-book=Rates,Credit",
        "http://desk/apps/pos/?filter-sector=Agency&keep=1&filter-as-of=2026-11-10&filter-book=Rates,Credit&filter-dates=2026-10-01..2026-11-10",
        "http://desk/apps/pos/?keep=1&filter-as-of=2026-11-10&filter-book=Rates,Credit&filter-dates=2026-10-01..2026-11-10",
    ]  # half a range, and an empty search where there was none, write nothing
