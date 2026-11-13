"""What basis builds around every page, the same in every app: Mantine's provider, with basis's theme; the two
components with fixed ids that the data and display lanes build against (`basis.dash.ids`); and the shell, a header
with the app's title, a badge naming the environment, so nobody mistakes QA for prod, the navigation from the declared
pages and the light and dark switch, over Dash Pages' container, where the page the URL names shows. Notifications,
where a callback's failure shows, sit at the bottom right, over the page and clear of its filters.

The switch flips `data-mantine-color-scheme` on `<html>` in the browser alone, and keeps the choice where Mantine reads
it when a page loads (`static/basis.js`); every colour is a CSS variable, so nothing is drawn again. A page at no
declared path shows `not_found`, which links to the first page."""

from collections.abc import Sequence
from typing import Final

import dash_mantine_components as dmc

import dash
from basis.dash import theme
from basis.dash.app import App
from basis.dash.grids import SEARCH
from basis.dash.icons import icon
from basis.dash.ids import NOTIFICATIONS, STATUS
from basis.dash.pages import FRESHNESS, Page
from dash import ALL, MATCH, ClientsideFunction, Input, Output, State, dcc, html
from dash.development.base_component import Component

SCHEME: Final = "basis-scheme"
"""The light and dark switch."""
HEADER_HEIGHT: Final = 44
NOT_FOUND: Final = "/_basis/not-found"
"""Where the page Dash Pages shows for an unknown path is registered; Dash Pages finds it by its module's name."""
LOCAL: Final = "local"
"""The badge of an app no environment names, as `basis dev` serves one."""


def chrome(app: App, *, environment: str | None = None) -> Component:
    """The layout every page of `app` shows inside, in `environment`."""
    shell = dmc.AppShell(
        header={"height": HEADER_HEIGHT}, padding=0, children=[dmc.AppShellHeader(header(app, environment), className="basis-header"), dmc.AppShellMain(dash.page_container, className="basis-main")]
    )
    return dmc.MantineProvider(
        theme=theme.mantine(),  # ty: ignore[invalid-argument-type] -- DMC types `theme` as a sequence, where Mantine's theme is an object
        defaultColorScheme="auto",
        children=[dmc.NotificationContainer(id=NOTIFICATIONS, position="bottom-right", limit=4, className="basis-notifications"), dcc.Store(id=STATUS, data={}), shell],
    )


def header(app: App, environment: str | None) -> Component:
    """The header's one row: the title, the environment, the pages, the switch."""
    named = environment or LOCAL
    items: list[Component] = [
        dcc.Link(app.title, href=dash.get_relative_path("/"), className="basis-app-title"),
        html.Span(named.upper(), className=f"basis-env basis-env-{named}", title=f"The {named} environment"),
    ]
    if len(app.pages) > 1:
        items.append(html.Nav(links(app.pages), className="basis-nav", **{"aria-label": "Pages"}))
    items += [
        html.Div(className="basis-header-gap"),
        dmc.ActionIcon(icon("sun-moon"), id=SCHEME, variant="subtle", color="gray", size="md", className="basis-scheme", **{"aria-label": "Switch the colour scheme"}),
    ]
    return html.Div(items, className="basis-header-row")


def links(pages: Sequence[Page]) -> list[Component]:
    """A link to each page, in the order the app lists them, marked when it is the page shown."""
    return [dmc.NavLink(label=page.title, href=dash.get_relative_path(page.path), active="exact", className="basis-nav-link") for page in pages]


def wire(made: dash.Dash) -> None:
    """basis's own callbacks, on `made` itself, since they are basis's and no module's, each in the browser alone: the
    switch, every page's freshness line, from the status store, and every grid's quick filter."""
    made.clientside_callback(ClientsideFunction(namespace="basis", function_name="scheme"), Output(SCHEME, "aria-label"), Input(SCHEME, "n_clicks"))
    fresh = {"type": FRESHNESS, "page": ALL, "datasets": ALL}
    made.clientside_callback(ClientsideFunction(namespace="basis", function_name="freshness"), Output(fresh, "children"), Output(fresh, "className"), Output(fresh, "title"), Input(STATUS, "data"))
    search = {"type": SEARCH, "grid": MATCH}
    made.clientside_callback(ClientsideFunction(namespace="basis", function_name="search"), Output(search, "className"), Input(search, "value"), State(search, "id"), prevent_initial_call=True)


def not_found(app: App) -> Component:
    """What a path that names no page shows."""
    first = app.pages[0]
    return html.Div(
        [
            html.Div(html.H1("No page here", className="basis-page-title"), className="basis-page-head"),
            html.P(["This address names no page of ", app.title, ". ", dcc.Link(f"Go to {first.title}", href=dash.get_relative_path(first.path)), "."], className="basis-not-found"),
        ],
        className="basis-page",
    )
