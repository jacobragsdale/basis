"""Finding an app, as `basis serve` and `basis dev` do: by its entry point, its module, or `module:attribute`; and the
app `basis dev` builds again, with Dash's dev tools, in the process uvicorn's reloader starts."""

import sys
import types
from importlib.metadata import EntryPoint

import pytest
from dash import Dash, html
from fastapi import FastAPI
from fastapi.testclient import TestClient

import basis.dash as bd
from basis.dash import loading, server

LISTED = [EntryPoint(name="loaded", value="loaded_app:app", group=loading.GROUP)]


@pytest.fixture
def declared(monkeypatch: pytest.MonkeyPatch) -> bd.App:
    module = types.ModuleType("loaded_app")
    monkeypatch.setitem(sys.modules, "loaded_app", module)
    app = bd.App(name="loaded", title="Loaded", settings=bd.Settings, pages=[bd.page("/", "Loaded", layout=html.Div())])
    module.__dict__.update(app=app, made=Dash(__name__, server=False), other="not an app")
    return app


def test_an_app_is_found_by_its_entry_point_its_module_or_its_attribute(declared: bd.App) -> None:
    assert loading.load(listed=LISTED) is declared
    assert loading.load("loaded", listed=LISTED) is declared
    assert loading.load("loaded_app", listed=[]) is declared
    assert loading.load("loaded_app:app", listed=[]) is declared


@pytest.mark.parametrize(
    ("reference", "listed", "message"),
    [
        (None, [], r"the installed packages list no apps in the basis.dash_apps entry points: name the app to serve"),
        ("nowhere_at_all", [], r"no app named 'nowhere_at_all'.*give its module path, as positions.app"),
        ("loaded_app:made", [], r"loaded_app:made is a Dash app the repo made itself, which basis does not serve"),
        ("loaded_app:other", [], r"loaded_app:other is not a bd.App"),
    ],
)
@pytest.mark.usefixtures("declared")
def test_a_reference_that_names_no_app_says_how_to_declare_one(reference: str | None, listed: list[EntryPoint], message: str) -> None:
    with pytest.raises(bd.DeclarationError, match=message):
        loading.load(reference, listed=listed)


@pytest.mark.usefixtures("declared")
def test_dev_builds_the_app_again_with_dashs_dev_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(server.DEV_APP, "loaded_app:app")
    monkeypatch.setattr(server.logs, "setup", lambda: None)  # the test's logging stays pytest's
    built = server.dev_app()
    assert isinstance(built, FastAPI)
    with TestClient(built) as client:
        index = client.get("/").text
    assert '"ui":true' in index
    assert '"disable_version_check":true' in index  # the check calls plotly.com
    assert '"hot_reload":{' in index
