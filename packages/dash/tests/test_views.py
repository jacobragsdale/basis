"""A view: a pure function of datasets and params, declared, run in a worker thread, conformed to its row, and cached by
its inputs' versions and its params, so a view is computed again when an input is read again, and never otherwise."""

import threading
from collections.abc import Iterator, Mapping
from datetime import date
from pathlib import Path
from typing import Annotated

import pandas as pd
import pytest
from dash import html

import basis.dash as bd
from basis.core import fakes
from basis.core.databases import Bind
from basis.core.rows import SchemaError
from basis.dash import testing


class Position(bd.Row):
    account: str
    sector: str
    par: float


class Sector(bd.Row):
    sector: str
    par: Annotated[float, bd.format.money("USD", 0)]
    weight: Annotated[float, bd.format.pct(1)]


class Largest(bd.Row):
    sector: str


SQL_FILE = Path(__file__).parent / "sql" / "holdings.sql"
WAREHOUSE = bd.database("warehouse", dialect="mssql", deadline_s=5, max_concurrent=2)
POSITIONS = bd.query("positions", WAREHOUSE, "sql/holdings.sql", Position, binds={"as_of": date}, ttl_s=60, stale_s=0)
DAY = date(2026, 11, 13)
computed: list[tuple[str, str]] = []
"""Each view's computation, with the thread it ran in."""


def by_sector(positions: pd.DataFrame, minimum: float) -> pd.DataFrame:
    computed.append(("sectors", threading.current_thread().name))
    positions.loc[:, "par"] = 0.0  # a function's change to its input is its own
    kept = positions.assign(par=[1_000_000.0, 500_000.0, 500_000.0])
    totals = kept.groupby("sector")["par"].sum()
    totals = totals[totals >= minimum]
    return pd.DataFrame({"sector": totals.index, "par": totals.to_numpy(), "weight": (totals / totals.sum()).to_numpy()})


def largest(sectors: pd.DataFrame) -> pd.DataFrame:
    computed.append(("largest", threading.current_thread().name))
    return sectors.nlargest(1, "par")[["sector"]]


def wrong(positions: pd.DataFrame) -> pd.DataFrame:
    return positions


def no_frame(positions: pd.DataFrame) -> object:
    return positions.to_dict()


SECTORS = bd.view("sectors", by_sector, Sector, positions=POSITIONS)
LARGEST = bd.view("largest", largest, Largest, sectors=SECTORS)
WRONG = bd.view("wrong", wrong, Sector, positions=POSITIONS)
NO_FRAME = bd.view("no_frame", no_frame, Sector, positions=POSITIONS)
APP = bd.App(name="views", title="Views", settings=bd.Settings, resources=[WAREHOUSE], pages=[bd.page("/", "Views", layout=html.Div())])


def _positions(binds: Mapping[str, Bind]) -> pd.DataFrame:
    assert binds == {"as_of": DAY}
    return pd.DataFrame({"account": ["A1", "A2", "A3"], "sector": ["rates", "credit", "rates"], "par": [1.0, 2.0, 3.0]})


@pytest.fixture
def client() -> Iterator[testing.Client]:
    computed.clear()
    database = fakes.Database("warehouse")
    database.answers[SQL_FILE.read_text(encoding="utf-8")] = _positions
    with testing.client(APP, fakes={"warehouse": database}) as running:
        yield running


def _load(client: testing.Client, dataset: bd.Dataset, /, **params: object) -> pd.DataFrame:
    async def loading() -> pd.DataFrame:
        return await bd.load(dataset, **params)

    portal = client.http.portal
    assert portal is not None  # inside the client's block
    return portal.call(loading)


def test_a_view_is_computed_once_off_the_loop_and_conformed_to_its_row(client: testing.Client) -> None:
    shown = [_load(client, SECTORS, as_of=DAY, minimum=0.0) for _ in range(3)]
    assert [name for name, _ in computed] == ["sectors"]
    assert computed[0][1] != threading.current_thread().name
    assert computed[0][1] != "MainThread"
    assert shown[0].to_dict("list") == {"sector": ["credit", "rates"], "par": [500_000.0, 1_500_000.0], "weight": [0.25, 0.75]}
    assert _load(client, POSITIONS, as_of=DAY)["par"].tolist() == [1.0, 2.0, 3.0]  # the function's change to its input was its own


def test_a_views_own_params_are_part_of_its_key(client: testing.Client) -> None:
    assert _load(client, SECTORS, as_of=DAY, minimum=600_000.0)["sector"].tolist() == ["rates"]
    assert _load(client, SECTORS, as_of=DAY, minimum=0.0)["sector"].tolist() == ["credit", "rates"]
    assert len(computed) == 2


def test_a_view_is_computed_again_when_an_input_is_read_again_and_never_otherwise(client: testing.Client) -> None:
    now = [1000.0]
    client.built.runtime.cache.clock = lambda: now[0]
    _load(client, LARGEST, as_of=DAY, minimum=0.0)
    now[0] += 59
    assert _load(client, LARGEST, as_of=DAY, minimum=0.0)["sector"].tolist() == ["rates"]
    assert [name for name, _ in computed] == ["sectors", "largest"]  # a view of a view, each once
    now[0] += 3600  # past the positions' TTL: read again, so each view above it is computed again
    _load(client, LARGEST, as_of=DAY, minimum=0.0)
    assert [name for name, _ in computed] == ["sectors", "largest", "sectors", "largest"]
    assert len(client.built.runtime.cache) == 3


def test_a_view_that_returns_what_its_row_does_not_declare_is_refused(client: testing.Client) -> None:
    with pytest.raises(SchemaError, match="view wrong does not match its row"):
        _load(client, WRONG, as_of=DAY)
    with pytest.raises(TypeError, match="returned a dict: return a pandas frame of its row's columns"):
        _load(client, NO_FRAME, as_of=DAY)


def test_a_view_is_as_old_as_its_oldest_input(client: testing.Client) -> None:
    runtime, portal = client.built.runtime, client.http.portal
    assert portal is not None  # inside the client's block
    view = portal.call(runtime.fetch, SECTORS, {"as_of": DAY, "minimum": 0.0})
    read = portal.call(runtime.fetch, POSITIONS, {"as_of": DAY})
    assert view.loaded_at == read.loaded_at
    assert view.version > read.version
