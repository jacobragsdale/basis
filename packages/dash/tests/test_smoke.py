"""`basis smoke --env ENV`, basis-dash's side (`basis.dash.smoke`): the app as an environment serves it, here served in
the process behind its prefix and reached through the test client's transport, as a deploy would be over the network;
its datasets loaded with their smoke examples on the fakes, as the environment's databases would answer them."""

from collections.abc import Generator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import httpx2
import pytest
from dash_repos import FILES, made

import basis.dash as bd
from basis.core.errors import Unavailable
from basis.dash import fixtures, loading, smoke
from basis.dash.app import App

SMOKED = FILES["PKG/data.py"].replace("ttl_s=60, stale_s=3600)", "ttl_s=60, stale_s=3600, smoke={'as_of': date(2026, 11, 13)})")


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str | None = "http://testserver/apps/desk/") -> App:
    repo, _ = made(tmp_path, monkeypatch, PKG__data__py=SMOKED)
    monkeypatch.syspath_prepend(str(repo))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))  # no settings file of the developer's
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    for name in ("BASIS_SMOKE__URL", "BASIS_ENV"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BASIS_ENV", "qa")  # as a deployment's settings say which environment they are
    monkeypatch.setenv("BASIS_CACHE_MB", "64")
    if url is not None:
        monkeypatch.setenv("BASIS_SMOKE__URL", url)
    return loading.load()


@contextmanager
def _served(app: App, *, git_sha: str | None = "abc123") -> Generator[httpx2.BaseTransport]:
    settings = app.settings.given(serve={"prefix": "/apps/desk/"}, git_sha=git_sha)
    with bd.testing.client(app, settings=settings) as opened:
        yield opened.http._transport  # noqa: SLF001 -- the test client's own transport, which reaches the app in the process


def test_a_ready_app_whose_pages_answer_and_whose_datasets_load_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(tmp_path, monkeypatch)
    with _served(app) as transport:
        assert smoke.smoke(tmp_path, "qa", commit="abc123", app=app, transport=transport, fakes=fixtures.of(app)) == []


def test_an_app_serving_another_commit_is_named(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(tmp_path, monkeypatch)
    with _served(app) as transport:
        found = smoke.smoke(tmp_path, "qa", commit="def456", app=app, transport=transport, fakes=fixtures.of(app))
    assert found == [("http://testserver/apps/desk/ serves commit abc123, not def456", "wait until the deploy of def456 finishes, and check that its release reached the environment")]


def test_an_address_that_is_not_the_apps_is_named_before_its_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(tmp_path, monkeypatch, url="http://testserver/apps/elsewhere")
    with _served(app) as transport:
        [(reason, fix)] = smoke.smoke(tmp_path, "qa", app=app, transport=transport, fakes=fixtures.of(app))
    assert reason == "http://testserver/apps/elsewhere/health/ready answered 200 with text/html, not JSON"  # Dash's index, which answers any path
    assert fix == "check that BASIS_SMOKE__URL is the app's address, as a browser opens it, its prefix included"


def test_with_nowhere_to_reach_the_app_its_datasets_still_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(tmp_path, monkeypatch, url=None)
    fakes = fixtures.of(app)
    [warehouse] = fakes.values()
    assert isinstance(warehouse, fixtures.FixtureDatabase)
    warehouse.fails(Unavailable, query="positions")
    found = smoke.smoke(tmp_path, "qa", app=app, fakes=fakes)
    assert [reason for reason, _ in found] == ["basis smoke has nowhere to reach app desk in qa", "dataset positions did not load in qa with its smoke example"]
    assert found[0][1] == "set BASIS_SMOKE__URL in qa's settings to the app's address there, as a browser opens it, as https://qa.example.com/apps/desk/"


def test_a_smoke_example_names_each_param_of_its_datasets_and_no_other() -> None:
    warehouse = bd.database("smoked", dialect="oracle", deadline_s=5, max_concurrent=1)

    class Mark(bd.Row):
        price: float

    with pytest.raises(bd.DeclarationError, match=r"smoked_marks's smoke example is \{'day': 1\}: give the params basis smoke loads it with, each of its own and no other, as smoke=\{as_of=...\}"):
        bd.query("smoked_marks", warehouse, "sql/positions.sql", Mark, binds={"as_of": date}, ttl_s=1, stale_s=1, smoke={"day": 1})
