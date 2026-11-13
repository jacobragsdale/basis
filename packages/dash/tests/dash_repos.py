"""A Dash app's repo, made in a test's folder, laid out in its layers: a page of plain Dash beside one of basis's
components, on fixtures, as `basis check` and `basis render` see one. Each repo's package has a name of its own, since
Dash keeps the callbacks of every module it has seen."""

import itertools
from collections.abc import Mapping
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from basis.dash import loading

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


def made(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **changed: str) -> tuple[Path, str]:
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
