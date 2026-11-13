"""States, one look on every grid, graph and stat tile: loading shown only once an answer is slow, an empty grid's
sentence, a failed answer marked on the component it was for until its next one, and stale marked from the status
store."""

import json
import os
import shutil
import subprocess
from typing import Annotated, Any

import pytest
from dash import Input, Output, callback, dcc

import basis.dash as bd
from basis.dash import internals, states, testing
from basis.dash.ids import NOTIFICATIONS, STATUS
from basis.dash.theme import STATIC


class Settings(bd.Settings):
    pass


class Position(bd.Row):
    cusip: str
    par: Annotated[float, bd.format.money("USD", 0)]


GRID = bd.grid("state-grid", Position, key="cusip", dataset="positions")
GRAPH = bd.graph("state-graph", dataset="curve")
TILE = bd.stat("state-tile", "Market value", bd.format.money("USD", 0))


@callback(Output("state-grid", "rowData"), Input("state-go", "data"))
async def rows(_: object) -> list[dict[str, object]]:
    msg = "the warehouse is down"
    raise RuntimeError(msg)


@callback(Output("state-tile", "children"), Input("state-go", "data"))
async def lead(_: object) -> bd.Shown:
    return TILE.show(1.0)


PAGE = bd.page("/", "States", body=[bd.card("Positions", GRID), bd.card("Curve", GRAPH), TILE, dcc.Store(id="state-go", data=1)])
APP = bd.App(name="stated", title="Stated", settings=Settings, pages=[PAGE])


def _sent(made: object) -> Any:  # noqa: ANN401 -- what Dash sends the browser, JSON of any shape
    return json.loads(json.dumps(made, default=lambda found: found.to_plotly_json()))


def _of(found: Any, test: Any) -> list[Any]:  # noqa: ANN401 -- likewise, and the test of each part
    if isinstance(found, list):
        return [item for part in found for item in _of(part, test)]
    if not isinstance(found, dict):
        return []
    here = [found] if "props" in found and test(found) else []
    return here + [item for value in found.get("props", {}).values() for item in _of(value, test)]


def test_each_wrapper_has_its_marks_hidden_until_they_apply() -> None:
    for made, of, prop, dataset in ((GRID, "state-grid", "rowData", "positions"), (GRAPH, "state-graph", "figure", "curve"), (TILE, "state-tile", "children", None)):
        ids = [part["props"]["id"] for part in _of(_sent(made), lambda found: isinstance(found["props"].get("id"), dict) and found["props"]["id"].get("type") in {states.FAILED, states.STALE})]
        expected = [{"type": states.FAILED, "of": of, "prop": prop}]
        if dataset is not None:
            expected.insert(0, {"type": states.STALE, "of": of, "dataset": dataset})
        assert ids == expected
        marks = _of(_sent(made), lambda found: "basis-mark" in str(found["props"].get("className", "")).split(" "))
        assert {mark["props"]["className"] for mark in marks} == {states.HIDDEN}


def test_loading_shows_only_once_an_answer_is_slow_and_dims_in_place() -> None:
    [loading] = _of(_sent(GRID), lambda found: found["type"] == "Loading")
    props = loading["props"]
    assert (props["delay_show"], props["show_initially"]) == (states.DELAY, False)
    assert props["target_components"] == {"state-grid": ["rowData", "dashGridOptions"]}
    assert props["overlay_style"]["visibility"] == "visible"  # the content stays, dimmed, so nothing moves
    [graph] = _of(_sent(GRAPH), lambda found: found["type"] == "Loading")
    assert graph["props"]["target_components"] == {"state-graph": ["figure"]}


def test_an_empty_grid_says_so_and_a_grid_not_yet_answered_shows_nothing() -> None:
    [table] = _of(_sent(GRID), lambda found: found["type"] == "AgGrid")
    options = table["props"]["dashGridOptions"]
    assert options["localeText"] == {"noRowsToShow": "No rows for this view"}
    assert options["loading"] is False
    assert options["suppressNoRowsOverlay"] is True  # until the first answer, which the shell's callback then lets say so
    assert "rowData" not in table["props"]  # no "no rows" before the first answer


def test_a_failed_answer_marks_its_component_and_tells_the_person() -> None:
    with testing.client(APP) as client:
        body = internals.request("state-grid.rowData", [{"id": "state-go", "property": "data", "value": 1}], changed=["state-go.data"])
        answered = client.http.post(f"{client.base}{internals.UPDATE}", json=body).json()
        tile = client.fire("state-tile.children", {"state-go.data": 1})
    side = answered["sideUpdate"]
    assert side[json.dumps({"type": states.FAILED, "of": "state-grid", "prop": "rowData"}, separators=(",", ":"), sort_keys=True)] == {"className": "basis-mark basis-mark-failed"}
    assert NOTIFICATIONS in side
    assert "state-tile" in tile  # a callback that answers is not marked
    assert "response" not in answered or "state-grid" not in answered.get("response", {})


def test_the_shell_clears_a_mark_on_the_next_answer_and_shows_stale_from_the_status_store() -> None:
    with testing.client(APP) as client:
        dependencies = client.dependencies()
    recovered = sorted(str(entry["output"]) for entry in dependencies if "recovered" in json.dumps(entry.get("clientside_function")))
    assert len(recovered) == 3
    assert all(entry["prevent_initial_call"] for entry in dependencies if "recovered" in json.dumps(entry.get("clientside_function")))
    [stale] = [entry for entry in dependencies if "staleness" in json.dumps(entry.get("clientside_function"))]
    assert stale["inputs"] == [{"id": STATUS, "property": "data"}]


@pytest.mark.skipif(shutil.which("node") is None and "CI" not in os.environ, reason="the browser script runs under node, which this machine lacks; CI has it")
def test_the_browser_marks_a_component_stale_while_its_dataset_is() -> None:
    probe = """
    const SCRIPT = require("fs").readFileSync(process.argv[1], "utf8");
    globalThis.window = {dash_clientside: {}};
    eval(SCRIPT);
    const ids = [{type: "s", of: "a", dataset: "positions"}, {type: "s", of: "b", dataset: "curve"}, {type: "s", of: "c", dataset: "unknown"}];
    window.dash_clientside.callback_context = {outputs_list: ids.map((id) => ({id, property: "className"}))};
    const data = {positions: {stale: true}, curve: {stale: false}};
    console.log(JSON.stringify([window.dash_clientside.basis.staleness(data), window.dash_clientside.basis.recovered()]));
    """
    ran = subprocess.run(["node", "-e", probe, str(STATIC / "basis.js")], capture_output=True, text=True, encoding="utf-8", check=True, timeout=30)  # noqa: S607 -- node, on the path
    assert json.loads(ran.stdout) == [["basis-mark basis-mark-stale", states.HIDDEN, states.HIDDEN], states.HIDDEN]


@pytest.mark.skipif(shutil.which("node") is None and "CI" not in os.environ, reason="the browser script runs under node, which this machine lacks; CI has it")
def test_a_grid_says_it_has_no_rows_only_once_it_has_an_answer() -> None:
    probe = """
    const SCRIPT = require("fs").readFileSync(process.argv[1], "utf8");
    const options = {suppressNoRowsOverlay: true};
    let shown = 0;
    const api = {setGridOption: (name, value) => { options[name] = value; }, getDisplayedRowCount: () => 0, showNoRowsOverlay: () => { shown += 1; }};
    globalThis.window = {dash_clientside: {}, dash_ag_grid: {getApiAsync: (id) => id === "g" ? Promise.resolve(api) : Promise.reject(new Error(id))}};
    eval(SCRIPT);
    window.dash_clientside.callback_context = {outputs_list: {id: {type: "f", of: "g", prop: "rowData"}, property: "className"}};
    const answered = window.dash_clientside.basis.recovered([]);
    setTimeout(() => console.log(JSON.stringify({answered, options, shown})), 10);
    """
    ran = subprocess.run(["node", "-e", probe, str(STATIC / "basis.js")], capture_output=True, text=True, encoding="utf-8", check=True, timeout=30)  # noqa: S607 -- node, on the path
    assert json.loads(ran.stdout) == {"answered": states.HIDDEN, "options": {"suppressNoRowsOverlay": False}, "shown": 1}
