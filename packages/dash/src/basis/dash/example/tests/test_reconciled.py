"""Every page of the app on its fakes, and that the numbers they show are one set: the overview's, the positions',
the P&L's and the curve's totals agree, for the whole book and for each book alone."""

import json

import basis.dash as bd
import pytest

from basis.dash.example.app import app

AS_OF = "2026-11-12"


def _callback(page: str, *outputs: str) -> str:
    """The output of the page's one callback, as Dash names a callback of several."""
    return "...".join(f"{page}-{output}" for output in outputs).join(("..", ".."))


OVERVIEW = _callback(
    "overview", "market-value.children", "dv01.children", "yield.children", "pnl.children", "sectors.figure", "ratings.figure", "maturities.figure", "issuers.rowData", "issuers.dashGridOptions"
)
POSITIONS = _callback("positions", "grid.rowData", "grid.dashGridOptions", "market-value.children", "dv01.children", "yield.children", "duration.children", "sectors.figure", "scatter.figure")
PNL = _callback("pnl", "total.children", "carry.children", "rates.children", "spread.children", "explained.figure", "sectors.figure", "movers.rowData", "movers.dashGridOptions")
CURVE = _callback("curve", "2y.children", "10y.children", "30y.children", "2s10s.children", "chart.figure", "tenors.rowData", "book-chart.figure", "dv01.figure")
MONEY = bd.format.money("USD", 0)


def _texts(answered: dict[str, dict[str, object]], component: str) -> str:
    """Every text a tile shows, as JSON holds it."""
    return json.dumps(answered[component]["children"], ensure_ascii=False)


@pytest.mark.parametrize("book", [None, "Credit", "Liquidity"])
def test_the_pages_show_one_market_value_one_dv01_and_one_days_pnl(book: str | None) -> None:
    with bd.testing.client(app) as client:
        overview = client.fire(OVERVIEW, {"overview-as-of.value": AS_OF, "overview-book.value": book})
        positions = client.fire(POSITIONS, {"as-of.value": AS_OF, "book.value": book, "search.value": ""})
        pnl = client.fire(PNL, {"pnl-as-of.value": AS_OF, "pnl-book.value": book})
    rows = positions["positions-grid"]["rowData"]
    market_value, dv01, day = (bd.text(sum(row[name] for row in rows), MONEY) for name in ("market_value", "dv01", "pnl"))
    assert market_value in _texts(overview, "overview-market-value")
    assert market_value in _texts(positions, "positions-market-value")
    assert dv01 in _texts(overview, "overview-dv01")
    assert day in _texts(overview, "overview-pnl")
    assert day in _texts(pnl, "pnl-total")
    issuers = overview["overview-issuers"]["rowData"]
    assert sum(row["market_value"] for row in issuers) == pytest.approx(sum(row["market_value"] for row in rows))
    explained = pnl["pnl-explained"]["figure"]["data"][0]
    assert explained["x"][-1] == "Day's P&L"
    assert explained["text"][-1] == day  # the waterfall's total, carry, rates, spread and the residual summed


def test_a_book_holds_only_its_own_largest_moves() -> None:
    with bd.testing.client(app) as client:
        pnl = client.fire(PNL, {"pnl-as-of.value": AS_OF, "pnl-book.value": "Municipals"})
    movers = pnl["pnl-movers"]["rowData"]
    assert {row["book"] for row in movers} == {"Municipals"}
    assert len(movers) == 25  # the desk's number


def test_the_curve_page_shows_each_tenor_and_its_moves() -> None:
    with bd.testing.client(app) as client:
        curve = client.fire(CURVE, {"curve-as-of.value": AS_OF, "curve-book.value": None})
    tenors = curve["curve-tenors"]["rowData"]
    assert [row["tenor"] for row in tenors] == ["3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "20Y", "30Y"]
    ten = next(row for row in tenors if row["tenor"] == "10Y")
    assert bd.text(ten["ytw"], bd.format.pct(3)) in _texts(curve, "curve-10y")
    assert bd.text(ten["day"], bd.format.bps(1)).lstrip("-") in _texts(curve, "curve-10y")
    assert [trace["name"] for trace in curve["curve-chart"]["figure"]["data"]] == ["As of", "Prior day", "One week before", "One month before"]


def test_every_page_builds_inside_the_shell() -> None:
    with bd.testing.client(app) as client:
        for page in app.pages:
            assert page.title in str(client.page(page.path))
