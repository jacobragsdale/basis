"""An empty app, built in the process and driven through FastAPI's `TestClient`, with no browser and no socket: what
building it gives Dash, its health and readiness, a callback's round trip, its pages inside basis's chrome, and its
path prefix behind a proxy that strips it and one that keeps it."""

import logging
import sys
import types
from collections.abc import Iterator
from http import HTTPStatus

import pytest
from dash import Input, Output, callback, dcc, html

import basis.dash as bd
from basis.dash import internals, testing
from basis.dash.ids import NOTIFICATIONS, STATUS


class Settings(bd.Settings):
    pass


OVERVIEW = bd.page("/", "Overview", layout=html.Div([dcc.Input(id="app-name", value="world"), html.Div(id="app-greeting")]))
POSITIONS = bd.page("/positions", "Positions", layout=lambda **_: html.Div("Every position", id="app-positions"))


@callback(Output("app-greeting", "children"), Input("app-name", "value"))
async def greet(name: str) -> str:
    return f"hello, {name}"


APP = bd.App(name="positions", title="Positions", settings=Settings, pages=[OVERVIEW, POSITIONS])


@pytest.fixture
def client() -> Iterator[testing.Client]:
    with testing.client(APP, settings=Settings.given(git_sha="abc123")) as running:
        yield running


def test_the_index_chooses_react_19_and_reaches_no_other_origin(client: testing.Client) -> None:
    index = client.index()
    major, _, rest = internals.REACT.partition(".")
    assert f"deps/react@{major}.v" in index  # Dash fingerprints the file: react@19.vVERSIONmHASH.2.4.min.js
    assert f".{rest}.min.js" in index
    assert "http://" not in index
    assert "https://" not in index  # every script and stylesheet is the app's own
    assert "<title>Positions</title>" in index
    assert "_dash-component-suites/basis.dash/static/basis.v" in index  # basis's own files, fingerprinted
    assert '"ui":false' in index  # Dash's dev tools are basis dev's alone
    assert "hot_reload" not in index


def test_health_answers_with_the_commit_and_readiness_follows_the_lifecycle(client: testing.Client) -> None:
    assert client.http.get("/health/live").json() == {"status": "live", "commit": "abc123"}
    assert client.http.get("/health/ready").json() == {"status": "ready", "commit": "abc123"}
    client.built.lifecycle.drain()
    draining = client.http.get("/health/ready")
    assert (draining.status_code, draining.json()) == (HTTPStatus.SERVICE_UNAVAILABLE, {"status": "draining"})


def test_a_callback_round_trips_through_dashs_own_route(client: testing.Client) -> None:
    assert client.fire("app-greeting.children", {"app-name.value": "desk"}) == {"app-greeting": {"children": "hello, desk"}}
    assert "app-greeting.children" in [entry["output"] for entry in client.dependencies()]


def test_every_page_shows_inside_the_chrome_with_its_two_fixed_ids(client: testing.Client) -> None:
    chrome = client.layout()
    assert {NOTIFICATIONS, STATUS} <= set(_ids(chrome))
    assert _found(chrome, STATUS)["props"] == {"id": STATUS, "data": {}}
    assert _found(chrome, NOTIFICATIONS)["type"] == "NotificationContainer"
    for page in APP.pages:
        shown = client.page(page.path)
        assert {NOTIFICATIONS, STATUS}.isdisjoint(_ids(shown))  # the page shows inside them, once
    assert "app-positions" in _ids(client.page("/positions"))
    assert "app-name" in _ids(client.page("/"))


def test_each_request_is_one_log_record_and_a_passing_health_check_none(client: testing.Client, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="basis.core.serving.requests")
    answered = client.http.get("/")
    client.http.get("/health/ready")
    assert [record.getMessage() for record in caplog.records] == ["GET /: 200"]
    assert "x-request-id" in answered.headers


@pytest.mark.parametrize(("stripped", "health", "index"), [(False, "/apps/pos/health/ready", "/apps/pos/"), (True, "/health/ready", "/")])
def test_a_path_prefix_maps_to_dashs_prefixes_and_health_follows_it(stripped: bool, health: str, index: str) -> None:  # noqa: FBT001 -- a parameter of the case
    settings = Settings.given(serve={"prefix": "/apps/pos/", "prefix_stripped": stripped})
    with testing.client(APP, settings=settings) as client:
        assert client.http.get(health).status_code == HTTPStatus.OK
        assert client.http.get("/health/live").status_code == HTTPStatus.OK  # where the cluster's probe asks, past the proxy
        page = client.http.get(index)
        assert page.status_code == HTTPStatus.OK
        assert '"requests_pathname_prefix":"\\u002fapps\\u002fpos\\u002f"' in page.text  # where the page asks for everything, as Dash writes it
        assert client.page("/positions")["props"] == {"children": "Every position", "id": "app-positions"}


OTHER = """
import basis.dash as bd
from dash import Input, Output, callback, html

page = bd.page("/", "Other", layout=html.Div([html.Button(id="other-go"), html.Div(id="other-shown")]))


@callback(Output("other-shown", "children"), Input("other-go", "n_clicks"))
def shown(clicks):
    return f"clicked {clicks}"


app = bd.App(name="other", title="Other", settings=bd.Settings, pages=[page])
"""


def test_a_second_app_in_one_process_has_its_own_callbacks_and_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("other_app")
    monkeypatch.setitem(sys.modules, "other_app", module)
    exec(compile(OTHER, "other_app.py", "exec"), module.__dict__)  # noqa: S102 -- an app declared in a module of its own, as a repo's is
    with testing.client(module.app) as client:
        outputs = [entry["output"] for entry in client.dependencies()]
        assert "app-greeting.children" not in outputs
        assert client.fire("other-shown.children", {"other-go.n_clicks": 2}) == {"other-shown": {"children": "clicked 2"}}
        assert "other-shown" in _ids(client.page("/"))
    with testing.client(APP) as client:  # built again, after another
        assert "other-shown.children" not in [entry["output"] for entry in client.dependencies()]
        assert client.fire("app-greeting.children", {"app-name.value": "again"}) == {"app-greeting": {"children": "hello, again"}}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"name": "Positions"}, "an app's name is lower-case letters, digits and dashes"),
        ({"title": " "}, "has no title"),
        ({"settings": dict}, "give bd.Settings, or a subclass of it"),
        ({"pages": []}, "lists no pages"),
        ({"pages": [OVERVIEW, OVERVIEW]}, "more than one page at /: give each page a path of its own"),
        ({"fakes": "positions.dev"}, "name the function that returns them, as positions.dev:fakes"),
    ],
)
def test_an_app_declares_what_basis_needs(change: dict[str, object], message: str) -> None:
    given: dict[str, object] = {"name": "positions", "title": "Positions", "settings": Settings, "pages": [OVERVIEW]} | change
    with pytest.raises(bd.DeclarationError, match=message):
        bd.App(**given)  # ty: ignore[invalid-argument-type] -- each case gives a wrong value on purpose


@pytest.mark.parametrize(("path", "message"), [("positions", "a page's path is / or /NAME"), ("/positions/", "a page's path is / or /NAME"), ("/a b", "a page's path is / or /NAME")])
def test_a_page_has_a_path_basis_can_serve(path: str, message: str) -> None:
    with pytest.raises(bd.DeclarationError, match=message):
        bd.page(path, "A page", layout=html.Div())


def test_a_prefix_starts_and_ends_with_a_slash() -> None:
    with pytest.raises(ValueError, match="a path prefix starts and ends with /"):
        Settings.given(serve={"prefix": "/apps/pos"})


def test_an_app_built_with_another_apps_settings_is_refused() -> None:
    class Other(bd.Settings):
        pass

    with pytest.raises(bd.DeclarationError, match=r"build it with those, not a .*Other"):
        bd.build(APP, settings=Other.given())


def _ids(component: object) -> list[str]:
    """Every component id in a layout, as Dash sends it."""
    found: list[str] = []
    if isinstance(component, list):
        for child in component:
            found += _ids(child)
    elif isinstance(component, dict):
        props = component.get("props", {})
        if isinstance(props, dict):
            if isinstance(identifier := props.get("id"), str):
                found.append(identifier)
            found += _ids(props.get("children"))
    return found


def _found(component: object, identifier: str) -> dict[str, object]:
    """The component in a layout whose id is `identifier`."""
    if isinstance(component, list):
        for child in component:
            if found := _found(child, identifier):
                return found
    elif isinstance(component, dict):
        props = component.get("props", {})
        if isinstance(props, dict):
            if props.get("id") == identifier:
                return {str(key): value for key, value in component.items()}
            return _found(props.get("children"), identifier)
    return {}


FAILING = """
import basis.dash as bd
from dash import Input, Output, callback, html

page = bd.page("/", "Failing", layout=html.Div([html.Button(id="failing-go"), html.Div("as it was", id="failing-shown")]))


@callback(Output("failing-shown", "children"), Input("failing-go", "n_clicks"))
def shown(clicks):
    raise RuntimeError("the warehouse did not answer")


app = bd.App(name="failing", title="Failing", settings=bd.Settings, pages=[page])
"""

STREAMING = """
import basis.dash as bd
from dash import Input, Output, callback, html

page = bd.page("/", "Streaming", layout=html.Div([html.Button(id="streaming-go"), html.Div(id="streaming-shown")]))


@callback(Output("streaming-shown", "children"), Input("streaming-go", "n_clicks"))
async def shown(clicks):
    yield "one"


app = bd.App(name="streaming", title="Streaming", settings=bd.Settings, pages=[page])
"""


def _declared(source: str, name: str, monkeypatch: pytest.MonkeyPatch) -> bd.App:
    module = types.ModuleType(name)
    monkeypatch.setitem(sys.modules, name, module)
    exec(compile(source, f"{name}.py", "exec"), module.__dict__)  # noqa: S102 -- an app declared in a module of its own, as a repo's is
    found = module.app
    assert isinstance(found, bd.App)
    return found


def test_a_callbacks_error_is_logged_once_and_its_outputs_keep_what_they_showed(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    with testing.client(_declared(FAILING, "failing_app", monkeypatch)) as client:
        assert client.fire("failing-shown.children", {"failing-go.n_clicks": 1}) == {}
    failed = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert [record.getMessage() for record in failed] == ["a callback failed: RuntimeError: the warehouse did not answer"]
    assert failed[0].exc_info is not None


def test_a_callbacks_setup_error_fails_the_build_not_the_first_request(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(Exception, match="shared_storage") as raised:
        bd.build(_declared(STREAMING, "streaming_app", monkeypatch))
    assert type(raised.value).__name__ == "StreamCallbackError"
