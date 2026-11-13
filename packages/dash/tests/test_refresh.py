"""A page's data read again: a timer that runs its callbacks, each load still answering from the cache while fresh; a
reload a person asks for, no more often than its dataset's declared `reload_s`; and when each value was read."""

import sys
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

import basis.dash as bd
from basis.dash import declarations, fixtures, testing

TESTS = Path(__file__).parent
DAY = date(2026, 11, 13)


@pytest.fixture
def declared(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[bd.App, bd.Query]]:
    monkeypatch.syspath_prepend(str(TESTS))
    from fixture_app.app import app  # noqa: PLC0415 -- the tests' package, on the path once the fixture puts it there
    from fixture_app.data import POSITIONS  # noqa: PLC0415

    yield app, POSITIONS
    for name in [name for name in sys.modules if name == "fixture_app" or name.startswith("fixture_app.")]:
        monkeypatch.delitem(sys.modules, name)


def test_a_refresh_is_a_timer_no_faster_than_its_floor() -> None:
    timer = bd.refresh("positions-every", every_s=60)
    assert timer.to_plotly_json()["props"] == {"id": "positions-every", "interval": 60_000, "n_intervals": 0}
    with pytest.raises(bd.DeclarationError, match="give every_s, 5 or more"):
        bd.refresh("positions-every", every_s=1)


def test_a_reload_drops_a_value_no_more_often_than_its_reload_s(declared: tuple[bd.App, bd.Query]) -> None:
    app, positions = declared
    (resource,) = app.resources
    assert isinstance(resource, bd.Database)
    warehouse = fixtures.database(resource, TESTS / "fixtures")
    with testing.client(app, fakes={"warehouse": warehouse}) as client:
        now = [1000.0]
        client.built.runtime.cache.clock = lambda: now[0]
        portal = client.http.portal
        assert portal is not None  # inside the client's block

        async def load() -> int:
            return len(await bd.load(positions, as_of=DAY))

        assert bd.loaded(positions, as_of=DAY) is None
        before = datetime.now(UTC)
        portal.call(load)
        read = bd.loaded(positions, as_of=DAY)
        assert read is not None
        assert read >= before
        assert bd.reload(positions, as_of=DAY) is False  # read just now, and its reload_s is 30 s
        portal.call(load)
        assert len(warehouse.queries) == 1
        now[0] += 30
        assert bd.reload(positions, as_of=DAY) is True
        assert bd.loaded(positions, as_of=DAY) is None
        portal.call(load)
        assert len(warehouse.queries) == 2


def test_a_dataset_is_reloaded_only_where_it_declares_reload_s(declared: tuple[bd.App, bd.Query]) -> None:
    app, positions = declared
    with testing.client(app):
        view = next(dataset for dataset in declarations.datasets(app.resources) if isinstance(dataset, bd.View))
        with pytest.raises(bd.LoadError, match="computed from its inputs: reload each of them"):
            bd.reload(view, as_of=DAY)
    plain = bd.query("plain", bd.database("plain", dialect="mssql", deadline_s=1, max_concurrent=1), "sql/positions.sql", positions.row, ttl_s=1, stale_s=0)
    with pytest.raises(bd.LoadError, match="declares no reload_s, so a person cannot ask for it again: declare reload_s"):
        bd.reload(plain)
    with pytest.raises(bd.DeclarationError, match="reload_s is 0: give the fewest seconds"):
        bd.query("plain", plain.database, "sql/positions.sql", positions.row, ttl_s=1, stale_s=0, reload_s=0)
