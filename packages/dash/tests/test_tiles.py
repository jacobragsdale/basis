"""Stat tiles, badges and the freshness line: a tile's value, change and sparkline written in its formats, a change
that rounds to nothing shown flat, a badge's tones held to 4.5:1, and the freshness line read from the status store in
the browser, the oldest load's time or what is stale."""

import json
import math
import os
import shutil
import subprocess
from typing import Any

import pytest
from dash import html

import basis.dash as bd
from basis.core.display import DEFAULT_ZONE
from basis.dash import testing, theme, tiles
from basis.dash.errors import DeclarationError
from basis.dash.ids import STATUS
from basis.dash.pages import FRESHNESS
from basis.dash.theme import STATIC

MARKET_VALUE = bd.stat("tile-mv", "Market value", bd.format.money("USD", 0), period="since yesterday")
RISK = bd.stat("tile-dv01", "DV01", bd.format.money("USD", 0), good="down")
YIELD = bd.stat("tile-ytw", "Yield", bd.format.pct(2), change=bd.format.bps(1), period="on the week")


def _sent(made: object) -> Any:  # noqa: ANN401 -- what Dash sends the browser, JSON of any shape
    return json.loads(json.dumps(made, default=lambda found: found.to_plotly_json()))


def _by_class(found: Any, name: str) -> list[Any]:  # noqa: ANN401 -- likewise
    if isinstance(found, list):
        return [item for part in found for item in _by_class(part, name)]
    if not isinstance(found, dict):
        return []
    props = found.get("props", {})
    here = [found] if name in str(props.get("className", "")).split(" ") else []
    return here + [item for value in props.values() for item in _by_class(value, name)]


def _text(found: Any) -> str:  # noqa: ANN401 -- likewise
    if isinstance(found, str):
        return found
    if isinstance(found, list):
        return "".join(_text(part) for part in found)
    if isinstance(found, dict):
        return _text(found.get("props", {}).get("children", ""))
    return ""


def test_a_tile_waits_with_its_label_and_a_dash() -> None:
    sent = _sent(MARKET_VALUE)
    assert sent["props"]["id"] == "tile-mv"
    assert sent["props"]["style"] == {"gridColumn": "span 3"}
    assert sent["props"]["aria-label"] == "Market value"
    assert _text(_by_class(sent, "basis-stat-label")) == "Market value"
    assert _text(_by_class(sent, "basis-stat-value")) == "\N{EM DASH}"


def test_a_tile_shows_its_value_and_a_signed_change_in_the_gain_colour() -> None:
    shown = _sent(MARKET_VALUE.show(2_463_000_000.4, change=12_500_000.0))
    assert _text(_by_class(shown, "basis-stat-value")) == "$2,463,000,000"
    [change] = _by_class(shown, "basis-stat-change")
    assert change["props"]["className"] == "basis-stat-change basis-gain"
    assert _text(change) == "+$12,500,000since yesterday"
    assert _by_class(change, "basis-icon-trending-up")


def test_a_fall_is_good_where_the_tile_says_so() -> None:
    [change] = _by_class(_sent(RISK.show(410_000.0, change=-25_000.0)), "basis-stat-change")
    assert change["props"]["className"] == "basis-stat-change basis-gain"
    assert _text(change) == "-$25,000"  # the sign the value and the grid write
    assert _by_class(change, "basis-icon-trending-down")
    [rise] = _by_class(_sent(RISK.show(410_000.0, change=25_000.0)), "basis-stat-change")
    assert rise["props"]["className"] == "basis-stat-change basis-loss"


def test_a_change_in_another_format_and_one_that_rounds_to_nothing() -> None:
    [change] = _by_class(_sent(YIELD.show(0.0512, change=0.00031)), "basis-stat-change")
    assert _text(change) == "+3.1\N{NO-BREAK SPACE}bpon the week"
    [flat] = _by_class(_sent(YIELD.show(0.0512, change=0.000001)), "basis-stat-change")
    assert flat["props"]["className"] == "basis-stat-change basis-flat"
    assert _text(flat) == "0.0\N{NO-BREAK SPACE}bpon the week"  # no sign and no colour where it rounds to nothing


def test_a_missing_value_shows_a_dash() -> None:
    assert _text(_by_class(_sent(MARKET_VALUE.show(math.nan)), "basis-stat-value")) == "\N{EM DASH}"
    assert _text(_by_class(_sent(MARKET_VALUE.show(None)), "basis-stat-value")) == "\N{EM DASH}"


def test_a_sparkline_draws_the_history_thinned_and_marks_its_last_point() -> None:
    history = [float(index) for index in range(1000)] + [math.nan]
    [spark] = [part for part in MARKET_VALUE.show(1.0, history=history) if getattr(part, "className", "") == "basis-stat-spark"]
    figure = spark.figure  # ty: ignore[unresolved-attribute] -- the graph's figure, which show sets
    line, last = figure["data"]
    assert len(line["y"]) == tiles.SPARK_POINTS
    assert (line["y"][0], line["y"][-1]) == (0.0, 999.0)
    assert list(last["y"]) == [999.0]
    assert spark.config["staticPlot"] is True  # ty: ignore[unresolved-attribute] -- likewise


@pytest.mark.parametrize(
    ("given", "refused"),
    [({"label": " "}, "has no label"), ({"display": {"kind": "money"}}, "give one of bd.format's"), ({"good": "sideways"}, "good is 'sideways'"), ({"span": 0}, "spans 1 to 12 columns")],
)
def test_a_tile_is_refused_where_it_cannot_be_shown(given: dict[str, object], refused: str) -> None:
    arguments: dict[str, object] = {"label": "Market value", "display": bd.format.money("USD", 0)} | given
    with pytest.raises(DeclarationError, match=refused):
        bd.stat("tile-refused", **arguments)  # ty: ignore[invalid-argument-type] -- each case's arguments


def test_a_badge_is_a_word_in_a_tone() -> None:
    sent = _sent(bd.badge("Callable", "info", icon_name="info"))
    assert sent["props"]["className"] == "basis-badge basis-badge-info"
    assert _text(sent) == "Callable"
    with pytest.raises(DeclarationError, match="a badge's tone"):
        bd.badge("Callable", "loud")  # ty: ignore[invalid-argument-type] -- the refused tone


@pytest.mark.parametrize("scheme", theme.SCHEMES)
@pytest.mark.parametrize(
    ("text", "fill"), [("secondary-foreground", "secondary"), ("info", "info-subtle"), ("success", "success-subtle"), ("warning", "warning-subtle"), ("destructive", "destructive-subtle")]
)
def test_every_badge_s_text_reads_on_its_fill(scheme: theme.Scheme, text: str, fill: str) -> None:
    assert theme.contrast(theme.value(text, scheme), theme.value(fill, scheme)) >= 4.5


def test_a_page_s_freshness_line_names_the_datasets_it_reports() -> None:
    class Settings(bd.Settings):
        pass

    page = bd.page("/", "Positions", body=[html.Div()], datasets=["positions", "sectors"])
    with testing.client(bd.App(name="fresh", title="Fresh", settings=Settings, pages=[page])) as client:
        [line] = _by_class(client.page("/"), "basis-freshness")
        fresh = next(entry for entry in client.dependencies() if FRESHNESS in str(entry["output"]))
    assert line["props"]["id"] == {"type": FRESHNESS, "page": "/", "datasets": "positions,sectors"}
    assert line["props"]["role"] == "status"
    assert fresh["clientside_function"] == {"namespace": "basis", "function_name": "freshness"}
    assert fresh["inputs"] == [{"id": STATUS, "property": "data"}]
    with pytest.raises(DeclarationError, match="reports 3"):
        bd.page("/", "Positions", body=[], datasets=[3])


def test_the_browser_s_zone_is_the_server_s() -> None:
    assert f'var ZONE = "{DEFAULT_ZONE}";' in (STATIC / "basis.js").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None and "CI" not in os.environ, reason="the browser script runs under node, which this machine lacks; CI has it")
def test_the_freshness_line_says_when_the_data_was_read_or_what_is_stale() -> None:
    probe = """
    const SCRIPT = require("fs").readFileSync(process.argv[1], "utf8");
    globalThis.window = {dash_clientside: {}};
    eval(SCRIPT);
    const ids = JSON.parse(process.argv[2]);
    window.dash_clientside.callback_context = {outputs_list: [ids.map((id) => ({id, property: "children"}))]};
    const now = "2026-11-13T20:00:00Z";
    const data = {
      positions: {loaded_at: "2026-11-13T19:05:03.5Z", rows: 1234, stale: false},
      sectors: {loaded_at: "2026-11-13T19:10:00Z", rows: 5, stale: false},
      curve: {loaded_at: "2026-11-12T21:30:00Z", rows: 8, stale: true},
    };
    console.log(JSON.stringify([window.dash_clientside.basis.freshness(data, now), window.dash_clientside.basis.freshness({}, now)]));
    """
    ids = [{"type": FRESHNESS, "page": "/", "datasets": "positions,sectors"}, {"type": FRESHNESS, "page": "/curve", "datasets": ""}]
    ran = subprocess.run(["node", "-e", probe, str(STATIC / "basis.js"), json.dumps(ids)], capture_output=True, text=True, encoding="utf-8", check=True, timeout=30)  # noqa: S607 -- node, on the path
    [texts, classes, titles], [empty_texts, empty_classes, _] = json.loads(ran.stdout)
    assert texts == ["Data as of 14:05:03 EST", "Stale: curve, as read 2026-11-12 16:30:00 EST"]
    assert classes == ["basis-freshness", "basis-freshness basis-stale"]
    assert titles[0] == "positions: 1,234 rows, read 14:05:03 EST\nsectors: 5 rows, read 14:10:00 EST"
    assert (empty_texts, empty_classes) == (["", ""], ["basis-freshness", "basis-freshness"])
