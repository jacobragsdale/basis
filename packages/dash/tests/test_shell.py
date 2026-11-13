"""The shell around every page and the page itself: the header's title, environment, navigation and switch; a page's
title, filters and cards; the page an unknown path shows; the tab's title; and the icons, drawn by CSS."""

import json
import shutil
import subprocess
from collections.abc import Callable, Iterator

import pytest
from dash import dcc, html
from fastapi.testclient import TestClient

import basis.dash as bd
from basis.dash import icons, shell, testing
from basis.dash.errors import DeclarationError
from basis.dash.theme import STATIC


class Settings(bd.Settings):
    pass


OVERVIEW = bd.page(
    "/",
    "Overview",
    description="Holdings as of the close",
    filters=[dcc.Input(id="shell-search", placeholder="Search")],
    body=[
        bd.card("By sector", html.Div("bars", id="shell-bars"), span=6, description="Market value"),
        bd.card("By rating", html.Div("bars", id="shell-ratings"), span=6, actions=[html.Button("Export", id="shell-export")]),
        bd.card("Positions", html.Div("grid", id="shell-grid"), flush=True),
    ],
)
POSITIONS = bd.page("/positions", "Positions", body=[bd.card(None, html.Div("every one", id="shell-every"))])
APP = bd.App(name="shelled", title="Holdings", settings=Settings, pages=[OVERVIEW, POSITIONS])


@pytest.fixture
def client() -> Iterator[testing.Client]:
    with testing.client(APP) as running:
        yield running


def test_the_header_names_the_app_its_environment_and_its_pages(client: testing.Client) -> None:
    chrome = client.layout()
    title = _by_class(chrome, "basis-app-title")
    assert (title["type"], _props(title)["children"], _props(title)["href"]) == ("Link", "Holdings", "/")
    badge = _by_class(chrome, "basis-env")
    assert (_props(badge)["children"], _props(badge)["className"]) == ("LOCAL", "basis-env basis-env-local")
    links = [_props(found) for found in _all(chrome, lambda found: _props(found).get("className") == "basis-nav-link")]
    assert [(link["label"], link["href"], link["active"]) for link in links] == [("Overview", "/", "exact"), ("Positions", "/positions", "exact")]
    assert _props(_by_class(chrome, "basis-nav"))["aria-label"] == "Pages"
    switch = _by_id(chrome, shell.SCHEME)
    assert _props(switch)["aria-label"] == "Switch the colour scheme"


@pytest.mark.parametrize("environment", ["dev", "qa", "prod"])
def test_the_badge_names_the_environment_the_app_serves_in(environment: str) -> None:
    built = bd.build(APP, settings=Settings.given(), environment=environment)
    with TestClient(built.asgi) as http:
        chrome = http.get("/_dash-layout").json()
    badge = _props(_by_class(chrome, "basis-env"))
    assert (badge["children"], badge["className"]) == (environment.upper(), f"basis-env basis-env-{environment}")


def test_an_app_of_one_page_has_no_navigation() -> None:
    alone = bd.App(name="alone", title="Alone", settings=Settings, pages=[POSITIONS])
    with testing.client(alone) as client:
        assert not _all(client.layout(), lambda found: _props(found).get("className") == "basis-nav")


def test_links_and_the_title_follow_the_path_prefix() -> None:
    with testing.client(APP, settings=Settings.given(serve={"prefix": "/apps/hold/", "prefix_stripped": True})) as client:
        chrome = client.layout()
    assert _props(_by_class(chrome, "basis-app-title"))["href"] == "/apps/hold/"
    assert [_props(found)["href"] for found in _all(chrome, lambda found: _props(found).get("className") == "basis-nav-link")] == ["/apps/hold/", "/apps/hold/positions"]


def test_the_switch_runs_in_the_browser_alone(client: testing.Client) -> None:
    switch = next(entry for entry in client.dependencies() if entry["output"] == f"{shell.SCHEME}.aria-label")
    assert switch["clientside_function"] == {"namespace": "basis", "function_name": "scheme"}
    assert "basis.dash/static/basis.v" in client.index()


def test_a_page_shows_its_title_filters_and_cards_in_twelve_columns(client: testing.Client) -> None:
    shown = client.page("/")
    assert _props(_by_class(shown, "basis-page-title"))["children"] == "Overview"
    assert _props(_by_class(shown, "basis-page-description"))["children"] == "Holdings as of the close"
    filters = _props(_by_class(shown, "basis-filters"))
    assert (filters["role"], filters["aria-label"]) == ("search", "Filters")
    assert "shell-search" in json.dumps(filters)
    cards = [_props(found) for found in _all(shown, lambda found: _props(found).get("className") == "basis-card")]
    assert [(card["aria-label"], card["style"]) for card in cards] == [("By sector", {"gridColumn": "span 6"}), ("By rating", {"gridColumn": "span 6"}), ("Positions", {"gridColumn": "span 12"})]
    assert _props(_by_class(shown, "basis-card-description"))["children"] == "Market value"
    assert "shell-export" in json.dumps(_by_class(shown, "basis-card-actions"))
    assert _all(shown, lambda found: _props(found).get("className") == "basis-card-body basis-card-flush")


def test_a_card_with_no_title_has_no_head(client: testing.Client) -> None:
    shown = client.page("/positions")
    assert not _all(shown, lambda found: _props(found).get("className") == "basis-card-head")
    assert not _all(shown, lambda found: _props(found).get("className") == "basis-filters")


def test_an_unknown_path_shows_basis_s_page_with_a_way_back(client: testing.Client) -> None:
    shown = client.page("/no-such-page")
    assert _props(_by_class(shown, "basis-page-title"))["children"] == "No page here"
    back = _all(shown, lambda found: found.get("type") == "Link")
    assert [(_props(link)["children"], _props(link)["href"]) for link in back] == [("Go to Overview", "/")]


def test_the_tab_names_the_page_then_the_app(client: testing.Client) -> None:
    router = next(entry for entry in client.dependencies() if testing.ROUTER in str(entry["output"]))
    keys = {name: [f"{entry['id']}.{entry['property']}" for entry in _entries(router[name])] for name in ("inputs", "state")}
    inputs = {key: "/positions" if key.endswith(".pathname") else "" for key in keys["inputs"]}
    answered = client.fire(str(router["output"]), inputs, state=dict.fromkeys(keys["state"]))
    assert answered["_pages_store"]["data"] == {"title": "Positions \N{MIDDLE DOT} Holdings"}


@pytest.mark.parametrize(
    ("given", "refused"),
    [
        ({"body": [], "layout": html.Div()}, "both a body and a layout"),
        ({}, "neither a body nor a layout"),
        ({"body": ["text"]}, "not a str"),
        ({"layout": html.Div(), "filters": [html.Div()]}, "takes no filters"),
    ],
)
def test_a_page_is_refused_where_it_cannot_be_built(given: dict[str, object], refused: str) -> None:
    with pytest.raises(DeclarationError, match=refused):
        bd.page("/", "Overview", **given)  # ty: ignore[invalid-argument-type] -- each case's arguments


@pytest.mark.parametrize("span", [0, 13, True, 2.5])
def test_a_card_spans_one_to_twelve_columns(span: object) -> None:
    with pytest.raises(DeclarationError, match="spans 1 to 12 columns"):
        bd.card("Positions", html.Div(), span=span)  # ty: ignore[invalid-argument-type] -- the refused spans


def test_an_icon_is_named_where_it_stands_alone_and_hidden_beside_a_label() -> None:
    alone = bd.icon("refresh-cw", label="Reload").to_plotly_json()["props"]
    assert (alone["className"], alone["role"], alone["aria-label"]) == ("basis-icon basis-icon-refresh-cw", "img", "Reload")
    beside = bd.icon("download").to_plotly_json()["props"]
    assert beside["aria-hidden"] == "true"
    assert "role" not in beside
    with pytest.raises(DeclarationError, match="there is no icon 'rocket'"):
        bd.icon("rocket")


def test_the_served_icons_are_the_table_s() -> None:
    written = (STATIC / "icons.css").read_text(encoding="utf-8")
    assert written == icons.stylesheet(), "static/icons.css is stale: run `uv run python -m basis.dash.icons`"
    for name in icons.NAMES:
        assert f".basis-icon-{name} {{" in written
    assert (STATIC / "lucide-LICENSE.txt").read_text(encoding="utf-8").startswith("ISC License")


@pytest.mark.skipif(shutil.which("node") is None, reason="the browser script runs under node, which this machine lacks; CI has it")
def test_the_switch_flips_the_scheme_and_keeps_the_choice() -> None:
    script = (STATIC / "basis.js").read_text(encoding="utf-8")
    probe = """
    const attributes = {"data-mantine-color-scheme": "light"}, kept = {};
    globalThis.window = {localStorage: {setItem: (key, value) => { kept[key] = value; }}};
    globalThis.document = {documentElement: {getAttribute: (name) => attributes[name], setAttribute: (name, value) => { attributes[name] = value; }}};
    eval(SCRIPT);
    const first = window.dash_clientside.basis.scheme(null);
    const flipped = window.dash_clientside.basis.scheme(1);
    console.log(JSON.stringify({first, flipped, scheme: attributes["data-mantine-color-scheme"], kept}));
    """.replace("SCRIPT", json.dumps(script))
    ran = subprocess.run(["node", "-e", probe], capture_output=True, text=True, check=True, timeout=30)  # noqa: S607 -- node, on the path
    assert json.loads(ran.stdout) == {"first": "Switch to the dark scheme", "flipped": "Switch to the light scheme", "scheme": "dark", "kept": {"mantine-color-scheme-value": "dark"}}


def _entries(given: object) -> list[dict[str, object]]:
    assert isinstance(given, list)
    return [{str(name): value for name, value in entry.items()} for entry in given if isinstance(entry, dict)]


def _props(component: dict[str, object]) -> dict[str, object]:
    props = component.get("props", {})
    return {str(name): given for name, given in props.items()} if isinstance(props, dict) else {}


def _all(component: object, matches: Callable[[dict[str, object]], bool]) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    if isinstance(component, list):
        for item in component:
            found += _all(item, matches)
    elif isinstance(component, dict):
        typed = {str(name): given for name, given in component.items()}
        if "props" in typed and matches(typed):
            found.append(typed)
        for given in _props(typed).values():
            found += _all(given, matches)
    return found


def _by_class(component: object, name: str) -> dict[str, object]:
    found = _all(component, lambda item: str(_props(item).get("className", "")).split(" ")[0] == name)
    assert found, f"nothing of class {name}"
    return found[0]


def _by_id(component: object, name: str) -> dict[str, object]:
    found = _all(component, lambda item: _props(item).get("id") == name)
    assert found, f"nothing with id {name}"
    return found[0]
