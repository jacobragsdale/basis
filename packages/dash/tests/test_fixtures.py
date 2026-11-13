"""An app run on its fixtures: a CSV per query, read through the query's row, a query's answer the rows of the day it
binds; a fake that fails on demand; `basis dev` and `bd.testing.client` on the fakes the app declares, which stand in
for every resource or none."""

import logging
import sys
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import basis.dash as bd
from basis.core.errors import Unavailable
from basis.dash import fixtures, server, testing

TESTS = Path(__file__).parent


@pytest.fixture(autouse=True)
def on_path(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The tests' folder on the path, as a repo's root is, so its app package imports."""
    monkeypatch.syspath_prepend(str(TESTS))
    yield
    for name in [name for name in sys.modules if name == "fixture_app" or name.startswith("fixture_app.")]:
        monkeypatch.delitem(sys.modules, name)


def _app() -> bd.App:
    from fixture_app.app import app  # noqa: PLC0415 -- the tests' package, on the path once the fixture puts it there

    return app


def test_a_test_runs_the_app_on_the_fakes_it_declares(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    with testing.client(_app()) as client:
        assert client.fire("fixture-par.children", {"fixture-day.value": "2026-11-13"})["fixture-par"] == {"children": "2 positions, 1,250,000 par"}
        assert client.fire("fixture-par.children", {"fixture-day.value": "2026-11-12"})["fixture-par"] == {"children": "1 positions, 5 par"}
        assert client.fire("fixture-par.children", {"fixture-day.value": "2026-11-01"})["fixture-par"] == {"children": "0 positions, 0 par"}


def test_a_fixture_database_fails_on_demand_as_the_database_would(caplog: pytest.LogCaptureFixture) -> None:
    from fixture_app import dev  # noqa: PLC0415 -- the tests' package, on the path once the fixture puts it there
    from fixture_app.data import WAREHOUSE  # noqa: PLC0415

    made = dev.fakes()
    warehouse = made[WAREHOUSE]
    assert isinstance(warehouse, fixtures.FixtureDatabase)
    warehouse.fails(Unavailable, query="positions", times=1)
    with testing.client(_app(), fakes=made) as client:
        assert "fixture-par" not in client.fire("fixture-par.children", {"fixture-day.value": "2026-11-13"})  # the output keeps what it showed
        assert "database warehouse could not be reached" in caplog.text
        assert client.fire("fixture-par.children", {"fixture-day.value": "2026-11-13"})["fixture-par"] == {"children": "2 positions, 1,250,000 par"}
    assert warehouse.queries == [("positions", {"as_of": date(2026, 11, 13)})] * 2
    with pytest.raises(fixtures.FixtureError, match="has no query trades: name one of positions"):
        warehouse.fails(Unavailable, query="trades")
    with pytest.raises(fixtures.FixtureError, match="only reads"), warehouse.statements(writes=True):
        pass


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, r"query positions has no fixture: write .*positions\.csv, a CSV of account, par, as_of"),
        ("account,par,as_of\nA1,,2026-11-13\n", r"fixture positions\.csv does not match its row: - par: 1 nulls in a column that may not be null"),
        ("account,par\nA1,1\n", r"fixture positions\.csv does not match its row: - missing columns: as_of"),
    ],
)
def test_a_fixture_that_does_not_match_its_row_fails_when_the_fake_is_made(tmp_path: Path, content: str | None, message: str) -> None:
    from fixture_app.data import WAREHOUSE  # noqa: PLC0415 -- the tests' package, on the path once the fixture puts it there

    if content is not None:
        (tmp_path / "positions.csv").write_text(content, encoding="utf-8")
    with pytest.raises(bd.DeclarationError, match=message):
        fixtures.database(WAREHOUSE, tmp_path)


def test_basis_dev_serves_the_app_on_its_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(server.DEV_APP, "fixture_app.app:app")
    monkeypatch.delenv(server.DEV_ENV, raising=False)
    monkeypatch.setattr(server.logs, "setup", lambda: None)  # the test's logging stays pytest's
    with TestClient(server.dev_app()) as client:
        assert client.get("/health/ready").status_code == 200


def test_an_app_declares_fakes_it_can_be_run_on(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app()
    missing = bd.App(name="fixtures", title="Fixtures", settings=bd.Settings, resources=app.resources, pages=app.pages)
    with pytest.raises(bd.DeclarationError, match=r"declares no fakes to run on: give its bd\.App fakes='module:function'"):
        fixtures.of(missing)
    with pytest.raises(bd.DeclarationError, match=r"no module fixture_app\.nowhere exists"):
        fixtures.of(bd.App(name="fixtures", title="Fixtures", settings=bd.Settings, resources=app.resources, pages=app.pages, fakes="fixture_app.nowhere:fakes"))
    with pytest.raises(bd.DeclarationError, match=r"fixture_app\.dev has no made"):
        fixtures.of(bd.App(name="fixtures", title="Fixtures", settings=bd.Settings, resources=app.resources, pages=app.pages, fakes="fixture_app.dev:made"))
    import fixture_app.dev  # noqa: PLC0415 -- the tests' package, on the path once the fixture puts it there

    monkeypatch.setattr(fixture_app.dev, "fakes", lambda: [1])
    with pytest.raises(bd.DeclarationError, match="returns a list: return a fake of each resource, by resource"):
        fixtures.of(app)
    monkeypatch.setattr(fixture_app.dev, "fakes", lambda: {1: object()})
    with pytest.raises(bd.DeclarationError, match="returns a fake for 1: key each by the resource it fakes"):
        fixtures.of(app)
