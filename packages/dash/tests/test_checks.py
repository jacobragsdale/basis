"""`basis check`'s checks of a Dash app's repo (`basis.dash.checks`, the part's `checks`): a repo laid out in its layers,
with a page of plain Dash and one of basis's components, passes; each rule broken is one problem, its reason and its
fix. Each test's repo has a package of its own name, since Dash keeps the callbacks of every module it has seen."""

import itertools
import warnings
from collections.abc import Mapping
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from basis.dash import checks, loading

_names = itertools.count()

FILES: Mapping[str, str] = {
    "pyproject.toml": '[project]\nname = "desk"\nversion = "0.1.0"\n\n[tool.basis]\nparts = ["dash"]\n',
    "fixtures/positions.csv": "account,sector,par,as_of\nA1,Rates,1000000,2026-11-13\nA1,Credit,2500000,2026-11-13\nA2,Credit,500000,2026-11-13\n",
    "PKG/__init__.py": "",
    "PKG/sql/positions.sql": "select account, sector, par, as_of from positions where as_of = :as_of\n",
    "PKG/app.py": """import basis.dash as bd
from PKG.data import WAREHOUSE
from PKG.pages import overview, sectors

app = bd.App(name="desk", title="Desk", settings=bd.Settings, resources=[WAREHOUSE], pages=[overview.page, sectors.page], fakes="PKG.dev:fakes")
""",
    "PKG/data.py": """from datetime import date
from typing import Annotated

import basis.dash as bd
from PKG import domain


class Position(bd.Row):
    account: str
    sector: str
    par: Annotated[float, bd.format.money("USD", 0)]
    as_of: date


class Sector(bd.Row):
    sector: str
    par: Annotated[float, bd.format.money("USD", 0)]


WAREHOUSE = bd.database("warehouse", dialect="oracle", deadline_s=5, max_concurrent=2)
POSITIONS = bd.query("positions", WAREHOUSE, "sql/positions.sql", Position, binds={"as_of": date}, ttl_s=60, stale_s=3600)
SECTORS = bd.view("sectors", domain.by_sector, Sector, positions=POSITIONS)
""",
    "PKG/domain.py": """import pandas as pd


def by_sector(positions: pd.DataFrame) -> pd.DataFrame:
    return positions.groupby("sector", as_index=False)["par"].sum()
""",
    "PKG/dev.py": """from basis.dash import fixtures
from PKG.data import WAREHOUSE


def fakes() -> dict[object, object]:
    return {WAREHOUSE: fixtures.database(WAREHOUSE)}
""",
    "PKG/pages/__init__.py": "",
    "PKG/pages/overview.py": """from datetime import date

import dash_ag_grid as dag
import plotly.graph_objects as go
from dash import Input, Output, callback, dash_table, dcc, html

import basis.dash as bd
from PKG.data import POSITIONS, SECTORS

LAYOUT = html.Div([
    dcc.Input(id="as-of", value="2026-11-13"),
    html.A("The desk's wiki", href="https://wiki.example.com/desk"),
    dcc.Graph(id="by-sector"),
    dag.AgGrid(id="positions", columnDefs=[{"field": "account"}, {"field": "sector"}, {"field": "par"}]),
    html.Div(id="total"),
    EXTRA
])
page = bd.page("/", "Overview", layout=LAYOUT)


@callback(Output("positions", "rowData"), Output("by-sector", "figure"), Input("as-of", "value"))
async def show(as_of: str) -> tuple[bd.Rows, go.Figure]:
    day = date.fromisoformat(as_of)
    positions, sectors = await bd.load(POSITIONS, as_of=day), await bd.load(SECTORS, as_of=day)
    return bd.rows(positions), go.Figure(go.Bar(x=sectors["sector"], y=sectors["par"]))


@callback(Output("total", "children"), Input("positions", "rowData"))
def total(rows: list[dict[str, object]] | None) -> str:
    return f"{len(rows or [])} positions"

CALLBACKS
""",
    "PKG/pages/sectors.py": """from datetime import date

from dash import Input, Output, callback

import basis.dash as bd
from PKG.data import SECTORS, Sector

page = bd.page("/sectors", "Sectors", body=[bd.card("By sector", bd.graph("sector-chart")), bd.card("Sectors", bd.grid("sector-grid", Sector, key="sector", totals={"par": "sum"}))])


@callback(Output("sector-chart", "figure"), Output("sector-grid", "rowData"), Input("sector-chart", "id"))
async def show(_: str) -> tuple[bd.Figure, bd.Rows]:
    sectors = await bd.load(SECTORS, as_of=date(2026, 11, 13))
    return bd.charts.bar(sectors, Sector, x="sector", y="par"), bd.rows(sectors)
""",
}


def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **changed: str) -> tuple[Path, str]:
    """A repo of `FILES`, its package named for the test, with `changed` files, by path, `PKG` the package, and
    `EXTRA` and `CALLBACKS` in the overview page what a test adds to its layout and its callbacks."""
    package = f"desk{next(_names)}"
    files = {**FILES, **{_path(key): text for key, text in changed.items()}}
    files["PKG/pages/overview.py"] = files["PKG/pages/overview.py"].replace("EXTRA", changed.get("EXTRA", "")).replace("CALLBACKS", changed.get("CALLBACKS", ""))
    for path, text in files.items():
        if path in {"EXTRA", "CALLBACKS"}:
            continue
        target = tmp_path / path.replace("PKG", package)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text.replace("PKG", package), encoding="utf-8")
    monkeypatch.setattr(loading, "entry_points", lambda **_: [EntryPoint(name="desk", value=f"{package}.app:app", group=loading.GROUP)])
    return tmp_path, package


def _path(key: str) -> str:
    """`PKG__data__py` as `PKG/data.py`; `EXTRA` and `CALLBACKS` as they are."""
    *folders, suffix = key.split("__")
    return f"{'/'.join(folders)}.{suffix}" if folders else key


def _reasons(repo: Path) -> list[str]:
    return [reason for reason, _ in checks.problems(repo)]


def test_an_app_in_its_layers_with_pages_of_plain_dash_and_of_basis_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _ = _repo(tmp_path, monkeypatch)
    assert checks.problems(repo) == []


def test_imports_go_down_the_layers_and_the_domain_stays_pure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    domain = "import pandas as pd\nimport basis.dash as bd\n\n\ndef by_sector(positions: pd.DataFrame) -> pd.DataFrame:\n    return positions.groupby('sector', as_index=False)['par'].sum()\n"
    repo, package = _repo(tmp_path, monkeypatch, PKG__domain__py=domain, PKG__dev__py=FILES["PKG/dev.py"])
    (repo / package / "data.py").write_text((repo / package / "data.py").read_text(encoding="utf-8") + f"\nfrom {package}.pages import sectors\n", encoding="utf-8")
    found = checks.problems(repo)
    upward = f"{package}/data.py:24 imports {package}.pages.sectors, so the data layer reaches the pages layer above it"
    impure = f"{package}/domain.py, a domain module, imports basis.dash"
    assert [reason for reason, _ in found if reason in {upward, impure}] == [upward, impure]
    assert dict(found)[upward] == "imports go down the layers alone: app, then pages, then data, then domain: move what it needs down"
    assert dict(found)[impure].startswith("keep its functions pure: frames and values in, a frame out, with no Dash, no bd.load and no I/O")


def test_a_views_function_outside_the_domain_is_held_pure_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wrapped = 'def by_sector(positions):\n    return domain.by_sector(positions)\n\n\nSECTORS = bd.view("sectors", by_sector,'
    data = FILES["PKG/data.py"].replace('SECTORS = bd.view("sectors", domain.by_sector,', wrapped)
    repo, package = _repo(tmp_path, monkeypatch, PKG__data__py=data)
    assert f"view sectors's function, by_sector, is in {package}.data, which imports basis.dash" in _reasons(repo)


def test_a_callback_in_a_module_that_is_neither_the_apps_nor_a_pages_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = "from dash import Input, Output, callback\n\n\n@callback(Output('total', 'title'), Input('as-of', 'value'))\ndef title(value: str) -> str:\n    return value\n"
    repo, package = _repo(tmp_path, monkeypatch, PKG__helpers__py=helper)
    [(reason, fix)] = checks.problems(repo)
    assert reason == f"{package}/helpers.py:4 declares a callback, which app desk leaves out, since that module is neither the app's nor a page's"
    assert fix == f"declare it in the module of the page it serves ({package}.pages.overview, {package}.pages.sectors), or in {package}.app, whose callbacks alone are the app's"


def test_a_callback_that_loads_and_is_not_async_is_refused_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sync = "\n@callback(Output('total', 'title'), Input('as-of', 'value'))\ndef title(as_of: str) -> str:\n    return str(len(bd.load(POSITIONS, as_of=date.fromisoformat(as_of))))\n"
    repo, package = _repo(tmp_path, monkeypatch, CALLBACKS=sync)
    assert checks.problems(repo) == [
        (f"callback {package}.pages.overview.title calls bd.load and is not async, so it would run on the event loop's thread", "make it `async def title(...)` and `await bd.load(...)`")
    ]


def test_every_id_and_property_a_callback_names_is_one_its_page_has(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wrong = "\n@callback(Output('totl', 'children'), Input('as-of', 'valu'), prevent_initial_call=True)\ndef wrong(value: str) -> str:\n    return value\n"
    repo, _ = _repo(tmp_path, monkeypatch, CALLBACKS=wrong)
    found = checks.problems(repo)
    assert found[0] == ("callback totl.children's output totl.children names no component of page /", "give a component that id in the page's layout, or name one it has")
    assert found[1][0] == "callback totl.children's input as-of.valu names a property Input does not have"
    assert found[1][1].startswith("name one of its properties, as ")
    assert len(found) == 2


def test_a_callback_that_fails_on_the_fakes_from_the_first_values_is_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    failing = "\n@callback(Output('total', 'title'), Input('as-of', 'value'))\nasync def title(as_of: str) -> str:\n    return str(1 / 0)\n"
    repo, _ = _repo(tmp_path, monkeypatch, CALLBACKS=failing)
    [(reason, fix)] = checks.problems(repo)
    assert reason == "callback total.title failed on the fakes, from page /'s first values: a callback failed: ZeroDivisionError: division by zero"
    assert fix == "fix it, as `uv run basis render /` shows it"


def test_an_answer_past_the_budget_or_a_callback_that_holds_the_loop_is_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    held = "\nimport time\n\n\n@callback(Output('total', 'title'), Input('as-of', 'value'))\ndef title(as_of: str) -> str:\n    time.sleep(0.15)\n    return as_of\n"
    repo, _ = _repo(tmp_path, monkeypatch, CALLBACKS=held)
    monkeypatch.setattr(checks, "LOOP_BUDGET_MS", 100.0)
    monkeypatch.setattr(checks.sizes, "BUDGET_BYTES", 200)
    found = dict(checks.problems(repo))
    [loop] = [reason for reason in found if reason.startswith("callback total.title held the event loop for ")]
    assert loop.endswith(" ms on the fakes, from page /'s first values, past the 100 ms every user's page waits on")
    assert found[loop] == "move the work into a view (bd.view), which runs in a worker thread, or narrow what it computes"
    assert any(reason.startswith("callback positions.rowData (and 1 more) answered 0.0 MB on the fakes, from page /'s first values, past the 0 MB budget") for reason in found)


def test_a_url_of_another_origin_the_browser_would_fetch_is_refused_but_a_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    extra = "html.Img(id='logo', src='https://cdn.example.com/logo.png'), html.Link(rel='stylesheet', href=dag.themes.QUARTZ),"
    repo, _ = _repo(tmp_path, monkeypatch, EXTRA=extra)
    reasons = _reasons(repo)
    assert reasons == [
        "page /'s Img 'logo' names https://cdn.example.com/logo.png in its src, of another origin, which the browser would fetch",
        "page /'s Link names https://cdn.jsdelivr.net/npm/ag-grid-community@35.3.1/styles/ag-theme-quartz.min.css in its href, of another origin, which the browser would fetch",
    ]  # and not the wiki's link, nor the SVG namespace in basis's icons


def test_a_stylesheet_of_the_apps_own_assets_that_fetches_another_origin_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, package = _repo(tmp_path, monkeypatch)
    (repo / package / "assets").mkdir()
    (repo / package / "assets" / "desk.css").write_text("@import url('https://fonts.example.com/css?family=Sans');\nbody { color: red; }\n", encoding="utf-8")
    [(reason, fix)] = checks.problems(repo)
    assert reason == "app desk's stylesheet /assets/desk.css fetches https://fonts.example.com/css?family=Sans, of another origin"
    assert fix == "vendor the file into the repo's assets/, and name it by a path of the app's own"


def test_rows_in_a_store_a_data_table_a_figure_plotly_refuses_and_a_grids_empty_column_are_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    extra = "dcc.Store(id='kept'), dcc.Graph(id='broken'), dash_table.DataTable(id='old'), dag.AgGrid(id='narrow', columnDefs=[{'field': 'account'}, {'field': 'desk'}]),"
    more = """
@callback(Output('kept', 'data'), Output('broken', 'figure'), Output('narrow', 'rowData'), Input('as-of', 'value'))
async def keep(as_of: str) -> tuple[object, object, object]:
    positions = await bd.load(POSITIONS, as_of=date.fromisoformat(as_of))
    return bd.rows(positions), {"data": [{"type": "bar", "bogus": 1}]}, bd.rows(positions)
"""
    repo, _ = _repo(tmp_path, monkeypatch, EXTRA=extra, CALLBACKS=more)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", r"\s*The dash_table.DataTable will be removed", DeprecationWarning)  # Dash's own, which the check's line says too
        reasons = _reasons(repo)
    assert "page / holds DataTable 'old', a dash_table.DataTable, which Dash deprecates" in reasons
    assert "callback kept.data (and 2 more) answers kept.data, a store, with rows" in reasons
    assert "callback kept.data (and 2 more) answers narrow.rowData with rows that hold no desk, a column of the grid on page /" in reasons
    [figure] = [reason for reason in reasons if reason.startswith("callback kept.data (and 2 more) answers broken.figure with a figure Plotly refuses: ")]
    assert "bogus" in figure


def test_an_app_that_reads_and_declares_no_fakes_or_does_not_load_is_one_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = FILES["PKG/app.py"].replace(', fakes="PKG.dev:fakes"', "")
    repo, _ = _repo(tmp_path, monkeypatch, PKG__app__py=app)
    assert _reasons(repo) == ["app desk lists resources and declares no fakes, so basis check cannot run its callbacks"]
    monkeypatch.setattr(loading, "entry_points", lambda **_: [EntryPoint(name="desk", value="nowhere_at_all.app:app", group=loading.GROUP)])
    [(reason, fix)] = checks.problems(repo)
    assert reason == "the repo's Dash app does not load"
    assert fix.startswith("no app named 'desk'")
