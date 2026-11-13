"""The agent's eyes (`basis.dash.render`): `basis render PAGE` prints what each output of a page shows, a grid's rows as
the grid writes them, a figure's traces and a text, on the app's fakes; `basis load DATASET` a dataset's first rows and
how the cache answered each load."""

import json
from pathlib import Path

import pytest
from dash_repos import made

from basis.core import command
from basis.dash import loading, render
from basis.dash.app import App


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **changed: str) -> App:
    repo, _ = made(tmp_path, monkeypatch, **changed)
    monkeypatch.syspath_prepend(str(repo))
    return loading.load()


def test_a_page_shows_its_grid_its_figure_and_its_text_as_a_person_sees_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(tmp_path, monkeypatch)
    rendered = render.render(app, "/", {})
    outputs = rendered.shown["outputs"]
    assert outputs["positions.rowData"] == {
        "kind": "grid",
        "rows": 3,
        "columns": ["account", "sector", "par"],
        "first": [{"account": "A1", "sector": "Rates", "par": 1000000.0}, {"account": "A1", "sector": "Credit", "par": 2500000.0}, {"account": "A2", "sector": "Credit", "par": 500000.0}],
    }  # a grid of plain dash_ag_grid's, whose columns declare no format
    assert outputs["by-sector.figure"]["traces"] == [{"type": "bar", "name": None, "points": 2}]
    assert outputs["total.children"] == {"kind": "text", "text": "3 positions"}
    loads = [(found["dataset"], found["rows"], found["cache"]) for found in rendered.shown["loads"]]
    assert loads == [("positions", 3, "miss"), ("positions", 3, "hit"), ("sectors", 2, "miss")]  # the view's input, from the cache
    assert (rendered.shown["failed"], rendered.failed) == ([], ())


def test_a_basis_grid_writes_each_number_in_its_columns_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rendered = render.render(_app(tmp_path, monkeypatch), "Sectors", {})
    grid = rendered.shown["outputs"]["sector-grid.rowData"]
    assert (grid["rows"], grid["columns"]) == (2, ["Sector", "Par"])
    assert grid["first"] == [{"Sector": "Credit", "Par": "$3,000,000"}, {"Sector": "Rates", "Par": "$1,000,000"}]
    assert rendered.shown["outputs"]["sector-chart.figure"]["kind"] == "figure"


def test_a_set_value_stands_in_for_the_layouts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rendered = render.render(_app(tmp_path, monkeypatch), "/", render.parsed(["as-of.value=2026-11-12"], "--set"))
    assert (rendered.shown["outputs"]["positions.rowData"]["rows"], rendered.shown["outputs"]["total.children"]["text"]) == (0, "0 positions")
    with pytest.raises(render.RenderError, match=r"page / has no component 'as-at': --set ID.PROP=VALUE names a component of the page's, as "):
        render.render(_app(tmp_path / "again", monkeypatch), "/", {"as-at.value": "2026-11-12"})


def test_a_page_it_cannot_find_names_those_it_can(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(render.RenderError, match=r"app desk has no page '/nowhere': name one by its path or title, of / \(Overview\), /sectors \(Sectors\)"):
        render.render(_app(tmp_path, monkeypatch), "/nowhere", {})


def test_a_failing_callback_is_in_the_json_and_fails_the_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    failing = "\n@callback(Output('total', 'title'), Input('as-of', 'value'))\nasync def title(as_of: str) -> str:\n    return str(1 / 0)\n"
    repo, _ = made(tmp_path, monkeypatch, CALLBACKS=failing)
    monkeypatch.syspath_prepend(str(repo))
    assert command.run(["render", "/"], cwd=repo, installed=command.installed()) == 1
    captured = capsys.readouterr()
    shown = json.loads(captured.out)
    assert shown["failed"] == [{"callback": "total.title", "error": "a callback failed: ZeroDivisionError: division by zero"}]
    assert shown["notifications"][0]["title"] == "Something went wrong."
    assert captured.err.splitlines()[-1] == "ERROR: callback total.title failed on page /: a callback failed: ZeroDivisionError: division by zero"


def test_load_prints_a_datasets_first_rows_and_how_the_cache_answered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    repo, _ = made(tmp_path, monkeypatch)
    monkeypatch.syspath_prepend(str(repo))
    assert command.run(["load", "positions", "--param", "as_of=2026-11-13"], cwd=repo, installed=command.installed()) == 0
    shown = json.loads(capsys.readouterr().out)
    assert (shown["dataset"], shown["kind"], shown["params"], shown["rows"]) == ("positions", "query", {"as_of": "2026-11-13"}, 3)
    assert shown["columns"] == ["account", "sector", "par", "as_of"]
    assert shown["first"][0] == {"account": "A1", "sector": "Rates", "par": "$1,000,000", "as_of": "2026-11-13"}
    assert [found["cache"] for found in shown["loads"]] == ["miss", "hit"]


def test_load_refuses_a_param_that_is_not_of_its_type_or_a_dataset_the_app_has_not(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(tmp_path, monkeypatch)
    with pytest.raises(render.RenderError, match=r"--param as_of=yesterday is not a date, which positions's as_of is: give one, as 2026-11-13"):
        render.load(app, "positions", {"as_of": "yesterday"})
    with pytest.raises(render.RenderError, match=r"app desk has no dataset 'trades': name one of positions, sectors"):
        render.load(app, "trades", {})
