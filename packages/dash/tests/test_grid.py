"""The grid, which reads its row: each column's header, alignment, format, filter and least width from the row; the
page's overrides; the totals, computed on the server; the quick filter, in the browser; and the refusals."""

import json
import math
import os
import re
import shutil
import subprocess
from datetime import date
from typing import Annotated, Any

import pandas as pd
import pytest
from dash import Input, Output, Patch, callback, dcc

import basis.dash as bd
from basis.core.display import Display
from basis.dash import grids, testing, theme
from basis.dash.errors import DeclarationError
from basis.dash.grids import SEARCH
from basis.dash.theme import STATIC


class Settings(bd.Settings):
    pass


class Position(bd.Row):
    cusip: str
    sector: str
    maturity: date
    par: Annotated[float, bd.format.money("USD", 0)]
    price: Annotated[float, bd.format.price(3)]
    ytw: Annotated[float, bd.format.pct(2)]
    oas: Annotated[float, bd.format.bps(0)] | None
    pnl: Annotated[float, bd.format.money("USD", 0)]
    quantity: float
    callable: bool


GRID = bd.grid(
    "grid-positions",
    Position,
    key="cusip",
    totals={"par": "sum", "pnl": "sum", "ytw": bd.weighted("par"), "oas": bd.weighted("par"), "price": "mean", "quantity": "count"},
    columns={"pnl": bd.column(signed=True, header="P&L"), "par": bd.column(bar=True), "oas": bd.column(heat=True), "cusip": bd.column(pin="left", width=110), "callable": bd.column(hide=True)},
)
FRAME = pd.DataFrame({
    "cusip": ["A1", "B2", "C3"],
    "sector": ["Treasury", "Agency", "Corporate"],
    "maturity": [date(2030, 1, 15), date(2035, 6, 1), date(2040, 11, 15)],
    "par": [1_000_000.0, 3_000_000.0, math.nan],
    "price": [99.5, 101.25, 100.0],
    "ytw": [0.04, 0.05, 0.09],
    "oas": [0.0050, math.nan, 0.0150],
    "pnl": [1200.5, -300.25, 0.1],
    "quantity": [1.0, math.nan, 3.0],
    "callable": [True, False, True],
})  # fmt: skip


@callback(Output("grid-positions", "rowData"), Output("grid-positions", "dashGridOptions"), Input("grid-go", "data"))
async def show(_: object) -> tuple[list[dict[str, object]], Patch]:
    return [{"cusip": "A1"}], GRID.totals(FRAME)


APP = bd.App(name="gridded", title="Gridded", settings=Settings, pages=[bd.page("/", "Positions", body=[bd.card("Positions", GRID, flush=True), dcc.Store(id="grid-go", data=1)])])


def _sent(made: object) -> Any:  # noqa: ANN401 -- what Dash sends the browser, JSON of any shape
    """A component as Dash sends it, its children too."""
    return json.loads(json.dumps(made, default=lambda found: found.to_plotly_json()))


def _table(made: object) -> Any:  # noqa: ANN401 -- likewise
    """The props of the AG Grid inside a grid."""
    return _of_type(_sent(made), "AgGrid")[0]["props"]


def _of_type(found: Any, kind: str) -> list[Any]:  # noqa: ANN401 -- likewise
    if isinstance(found, list):
        return [item for part in found for item in _of_type(part, kind)]
    if not isinstance(found, dict):
        return []
    here = [found] if found.get("type") == kind else []
    return here + [item for value in found.get("props", {}).values() for item in _of_type(value, kind)]


def _columns() -> dict[str, Any]:
    return {str(column["field"]): column for column in _table(GRID)["columnDefs"]}


def test_each_column_comes_from_the_row() -> None:
    columns = _columns()
    assert list(columns) == Position.names()
    assert {name: column["headerName"] for name, column in columns.items()} == {
        "cusip": "CUSIP",
        "sector": "Sector",
        "maturity": "Maturity",
        "par": "Par",
        "price": "Price",
        "ytw": "YTW",
        "oas": "OAS",
        "pnl": "P&L",
        "quantity": "Quantity",
        "callable": "Callable",
    }
    assert {name: (column["cellDataType"], column["filter"]) for name, column in columns.items() if name in {"sector", "maturity", "par", "callable"}} == {
        "sector": ("text", "agTextColumnFilter"),
        "maturity": ("dateString", "agDateColumnFilter"),
        "par": ("number", "agNumberColumnFilter"),
        "callable": (False, "agTextColumnFilter"),
    }
    par = columns["par"]
    assert (par["type"], par["cellClass"]) == ("rightAligned", ["ag-right-aligned-cell", "basis-num"])
    assert par["valueFormatter"] == {"function": 'basisFormat(params.value, {"decimals":0,"symbol":"$"})'}
    assert columns["quantity"]["valueFormatter"] == {"function": 'basisFormat(params.value, {"decimals":2})'}  # a number with no format: the formatter's default
    assert "valueFormatter" not in columns["sector"]
    assert columns["callable"]["valueFormatter"] == {"function": "basisCheck(params.value)"}  # a check mark, never an editable-looking checkbox


def test_a_column_is_never_narrower_than_its_widest_value_or_its_header() -> None:
    columns = _columns()
    total = bd.text(-1_234_567_890.123, bd.format.money("USD", 0))
    assert int(columns["par"]["minWidth"]) >= len(total) * 7
    assert int(columns["maturity"]["minWidth"]) >= 100
    assert int(columns["ytw"]["minWidth"]) < int(columns["par"]["minWidth"])


def test_a_page_overrides_a_column_where_it_must() -> None:
    columns = _columns()
    assert (columns["cusip"]["pinned"], columns["cusip"]["width"], columns["cusip"]["suppressSizeToFit"]) == ("left", 110, True)
    assert columns["callable"]["hide"] is True
    assert columns["pnl"]["cellClassRules"] == {"basis-gain": "params.value > 0", "basis-loss": "params.value < 0"}
    assert columns["par"]["cellStyle"] == {"function": 'basisScale(params, "bar")'}
    assert columns["oas"]["cellStyle"] == {"function": 'basisScale(params, "heat")'}


def test_the_grid_is_community_quartz_in_the_tokens_with_stable_ids_and_copyable_text() -> None:
    table = _table(GRID)
    options = table["dashGridOptions"]
    assert isinstance(options, dict)
    assert options["theme"] == theme.GRID
    assert (options["enableCellTextSelection"], options["ensureDomOrder"]) == (True, True)
    assert table["getRowId"] == "params.data.cusip"
    assert table["style"] == {"height": "520px", "width": "100%"}
    assert "enableEnterpriseModules" not in table
    assert "licenseKey" not in table
    assert not re.search(r"https?://", json.dumps(table))


def test_a_short_table_is_as_tall_as_its_rows() -> None:
    options = _table(bd.grid("grid-positions", Position, height=None, search=False))["dashGridOptions"]
    assert isinstance(options, dict)
    assert options["domLayout"] == "autoHeight"


def test_totals_are_the_server_s_and_skip_missing_values() -> None:
    [found] = grids.totals(FRAME, GRID.declared)
    assert found["par"] == 4_000_000.0  # the NaN skipped
    assert found["pnl"] == math.fsum([1200.5, -300.25, 0.1])
    assert found["ytw"] == pytest.approx((0.04 * 1e6 + 0.05 * 3e6) / 4e6)  # the third row's par is missing, so its yield weighs nothing
    assert found["oas"] == pytest.approx(0.0050)  # only the first row has both
    assert found["price"] == pytest.approx((99.5 + 101.25 + 100.0) / 3)
    assert found["quantity"] == 2.0
    assert found["cusip"] == "Total"  # the label, in the first text column with no total
    assert set(found) == {"par", "pnl", "ytw", "oas", "price", "quantity", "cusip"}


def test_a_total_of_nothing_is_none() -> None:
    empty = FRAME.iloc[0:0]
    [found] = grids.totals(empty, GRID.declared)
    assert (found["par"], found["ytw"], found["quantity"]) == (None, None, 0.0)
    least = bd.grid("grid-least", Position, totals={"par": "min", "pnl": "max"})
    [found] = grids.totals(FRAME, least.declared)
    assert (found["par"], found["pnl"]) == (1_000_000.0, 1200.5)
    assert grids.totals(FRAME, bd.grid("grid-none", Position).declared) == []


def test_a_callback_sends_the_rows_and_the_totals_as_a_change_to_the_grid_s_options() -> None:
    with testing.client(APP) as client:
        answered = client.fire("..grid-positions.rowData...grid-positions.dashGridOptions..", {"grid-go.data": 1})
    [operation] = _sent(answered["grid-positions"]["dashGridOptions"])["operations"]
    assert operation["location"] == ["pinnedBottomRowData"]
    assert operation["params"]["value"][0]["cusip"] == "Total"


def test_the_quick_filter_runs_in_the_browser_alone() -> None:
    sent = json.dumps(_sent(GRID))
    assert '{"type": "basis-grid-search", "grid": "grid-positions"}' in sent
    assert "Filter the rows" in sent  # the box's name, in its label
    with testing.client(APP) as client:
        search = next(entry for entry in client.dependencies() if SEARCH in str(entry["output"]))
    assert search["clientside_function"] == {"namespace": "basis", "function_name": "search"}
    assert "grid-search" not in json.dumps(_table(bd.grid("grid-positions", Position, search=False)))


@pytest.mark.parametrize(
    ("given", "refused"),
    [
        ({"key": "par"}, "key is 'par'"),
        ({"key": "oas"}, "key is 'oas'"),
        ({"key": "isin"}, "key is 'isin'"),
        ({"totals": {"sector": "sum"}}, "totals 'sector', which is no number column"),
        ({"totals": {"par": "median"}}, "total of 'par' is 'median'"),
        ({"totals": {"ytw": bd.weighted("sector")}}, "weights 'ytw' by 'sector'"),
        ({"columns": {"isin": bd.column(header="ISIN")}}, "shows 'isin', which Position does not declare"),
        ({"columns": {"sector": bd.column(signed=True)}}, "no number, so it takes no signed colours"),
        ({"columns": {"par": bd.column(bar=True, heat=True)}}, "has a bar and a heat scale"),
        ({"columns": {"par": bd.column(header=" ")}}, "has an empty header"),
        ({"columns": {"par": {"header": "Par"}}}, "give bd.column"),
    ],
)
def test_a_grid_is_refused_where_its_row_does_not_hold_what_it_names(given: dict[str, object], refused: str) -> None:
    with pytest.raises(DeclarationError, match=re.escape(refused)):
        bd.grid("grid-refused", Position, **given)  # ty: ignore[invalid-argument-type] -- each case's arguments


def test_a_grid_needs_a_row() -> None:
    with pytest.raises(DeclarationError, match=r"give a bd\.Row subclass"):
        bd.grid("grid-refused", dict)  # ty: ignore[invalid-argument-type] -- the refused row


@pytest.mark.parametrize(("name", "shown"), [("yield_to_worst", "Yield to worst"), ("oas_bps", "OAS BPS"), ("dv01", "DV01"), ("market_value_usd", "Market value USD"), ("x", "X")])
def test_a_header_is_its_name_in_words(name: str, shown: str) -> None:
    assert grids.header(name) == shown


@pytest.mark.parametrize("scheme", theme.SCHEMES)
@pytest.mark.parametrize(("pole", "share"), [("div-2", 24), ("div-10", 24), ("primary", 20), ("loss", 20)])
def test_text_over_the_heat_scale_and_bars_reads_at_4_5_to_1(scheme: theme.Scheme, pole: str, share: int) -> None:
    under = _mixed(theme.value(pole, scheme), theme.value("card", scheme), share / 100)
    for text in ("card-foreground", "gain", "loss"):
        ratio = theme.contrast(theme.value(text, scheme), under)
        assert ratio >= 4.5, f"{text} over {share}% of {pole} in {scheme} is {ratio:.2f}:1"


@pytest.mark.skipif(shutil.which("node") is None and "CI" not in os.environ, reason="the browser script runs under node, which this machine lacks; CI has it")
def test_the_scale_is_drawn_against_the_column_s_largest_magnitude() -> None:
    probe = """
    const SCRIPT = require("fs").readFileSync(process.argv[1], "utf8");
    globalThis.window = {};
    eval(SCRIPT);
    const scale = window.dashAgGridFunctions.basisScale;
    const rows = [{par: 2}, {par: -4}, {par: null}];
    const api = {getGridOption: (name) => name === "rowData" ? rows : undefined};
    const at = (value, pinned) => scale({value, api, colDef: {field: "par"}, node: {rowPinned: pinned}}, process.argv[2]);
    console.log(JSON.stringify([at(2), at(-4), at(0), at(null), at(9, "bottom")]));
    """
    bars = json.loads(subprocess.run(["node", "-e", probe, str(STATIC / "basis.js"), "bar"], capture_output=True, text=True, encoding="utf-8", check=True, timeout=30).stdout)  # noqa: S607 -- node, on the path
    assert "var(--basis-primary) 20%, transparent) 50.00%" in bars[0]["backgroundImage"]
    assert "var(--basis-loss) 20%, transparent) 100.00%" in bars[1]["backgroundImage"]
    assert bars[2:] == [None, None, None]
    heat = json.loads(subprocess.run(["node", "-e", probe, str(STATIC / "basis.js"), "heat"], capture_output=True, text=True, encoding="utf-8", check=True, timeout=30).stdout)  # noqa: S607 -- likewise
    assert heat[0] == {"backgroundColor": "color-mix(in oklch, var(--basis-div-2) 12.0%, transparent)"}
    assert heat[1] == {"backgroundColor": "color-mix(in oklch, var(--basis-div-10) 24.0%, transparent)"}


def _mixed(colour: str, surface: str, share: float) -> str:
    """`share` of `colour` over `surface`, as `color-mix(in oklch, ...)` makes it, written as OKLCH."""
    found = [_parsed(colour), _parsed(surface)]
    lightness, chroma = (share * found[0][index] + (1 - share) * found[1][index] for index in (0, 1))
    hue = found[0][2] if found[0][1] >= found[1][1] else found[1][2]
    return f"oklch({lightness:.4f} {chroma:.4f} {hue:.1f})"


def _parsed(colour: str) -> tuple[float, float, float]:
    found = re.match(r"oklch\(([\d.]+) ([\d.]+) ([\d.]+)\)", colour)
    assert found is not None, colour
    lightness, chroma, hue = (float(part) for part in found.groups())
    return lightness, chroma, hue


def test_a_grid_s_declaration_stays_on_the_server() -> None:
    assert "declared" not in json.dumps(_sent(GRID))
    assert isinstance(GRID.declared.row, type)
    assert GRID.declared.totals["ytw"] == bd.weighted("par")
    assert Display(kind="money", currency="USD", decimals=0) == Position.column("par").display


def _node(probe: str, *arguments: str) -> Any:  # noqa: ANN401 -- what the probe prints, JSON of any shape
    ran = subprocess.run(["node", "-e", probe, str(STATIC / "basis.js"), *arguments], capture_output=True, text=True, encoding="utf-8", check=True, timeout=30)  # noqa: S607 -- node, on the path
    return json.loads(ran.stdout)


needs_node = pytest.mark.skipif(shutil.which("node") is None and "CI" not in os.environ, reason="the browser script runs under node, which this machine lacks; CI has it")


def test_the_grid_tells_the_browser_what_its_totals_are() -> None:
    table = _table(GRID)
    assert table["dashGridOptions"]["context"]["basis"] == {
        "totals": {"par": "sum", "pnl": "sum", "ytw": {"weighted": "par"}, "oas": {"weighted": "par"}, "price": "mean", "quantity": "count"},
        "label": "cusip",
        "key": "cusip",
        "count": {"type": grids.COUNT, "grid": "grid-positions"},
        "noun": "rows",
    }
    assert table["eventListeners"] == {event: ["basisTotals(params)"] for event in ("firstDataRendered", "rowDataUpdated", "modelUpdated", "pinnedRowDataChanged")}
    count = next(part for part in _of_type(_sent(GRID), "Span") if part["props"].get("className") == "basis-grid-count")
    assert count["props"]["role"] == "status"


@needs_node
def test_the_browser_totals_rows_as_the_server_does() -> None:
    declared = _table(GRID)["dashGridOptions"]["context"]["basis"]
    rows = json.loads(FRAME.drop(columns=["maturity"]).to_json(orient="records"))
    probe = """
    const SCRIPT = require("fs").readFileSync(process.argv[1], "utf8");
    globalThis.window = {};
    eval(SCRIPT);
    const [rows, declared] = JSON.parse(process.argv[2]);
    console.log(JSON.stringify(window.dash_clientside.basis.totalsOf(rows, declared)));
    """
    browser = _node(probe, json.dumps([rows, declared]))
    [server] = grids.totals(FRAME, GRID.declared)
    assert browser.keys() == server.keys()
    for name, value in server.items():
        assert browser[name] == (pytest.approx(value, rel=1e-15) if isinstance(value, float) else value), name


@needs_node
def test_a_filter_totals_the_rows_it_keeps_and_clearing_it_brings_the_server_s_back() -> None:
    probe = """
    const SCRIPT = require("fs").readFileSync(process.argv[1], "utf8");
    const props = [];
    globalThis.window = {dash_clientside: {set_props: (id, given) => props.push(given.children)}};
    eval(SCRIPT);
    const rows = [{cusip: "A", par: 1}, {cusip: "B", par: 2}, {cusip: "C", par: 4}];
    const server = [{cusip: "Total", par: 7}];
    let pinned = server, kept = rows;
    const options = {context: {basis: {totals: {par: "sum"}, label: "cusip", key: "cusip", count: {type: "c", grid: "g"}, noun: "holdings"}}};
    const api = {
      getGridOption: (name) => name === "pinnedBottomRowData" ? pinned : options[name],
      setGridOption: (name, value) => { pinned = value; },
      forEachNode: (each) => rows.forEach((data) => each({data})),
      forEachNodeAfterFilter: (each) => kept.forEach((data) => each({data})),
      isAnyFilterPresent: () => kept !== rows,
    };
    const totals = window.dashAgGridFunctions.basisTotals;
    const seen = [];
    totals({api}); seen.push(pinned);
    kept = rows.slice(1); totals({api}); seen.push(pinned);
    pinned = [{cusip: "Total", par: 70}]; totals({api}); seen.push(pinned);
    kept = rows; totals({api}); seen.push(pinned);
    console.log(JSON.stringify({seen, props}));
    """
    found = _node(probe)
    assert found["seen"] == [
        [{"cusip": "Total", "par": 7}],  # nothing filtered: the server's
        [{"par": 6, "cusip": "Total"}],  # filtered: the rows kept
        [{"par": 6, "cusip": "Total"}],  # the server answers again while filtered: still the rows kept
        [{"cusip": "Total", "par": 70}],  # cleared: the server's latest
    ]
    assert found["props"] == ["3 holdings", "2 of 3 holdings", "2 of 3 holdings", "3 holdings"]
