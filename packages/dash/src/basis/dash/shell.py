"""What basis builds around every page: for now, Mantine's provider, the two components with fixed ids that the data
and display lanes build against (`basis.dash.ids`), and Dash Pages' container, where the page the URL names shows.
Step 16.2 builds the shell itself here: the header with the app's title, a badge naming the environment and the light
and dark switch, the navigation from the declared pages, and the place errors show."""

import dash_mantine_components as dmc

import dash
from basis.dash import theme
from basis.dash.app import App
from basis.dash.ids import NOTIFICATIONS, STATUS
from dash import dcc
from dash.development.base_component import Component


def chrome(app: App) -> Component:  # noqa: ARG001 -- 16.2's shell reads the app's title and pages
    """The layout every page of `app` shows inside."""
    return dmc.MantineProvider(theme=theme.mantine(), children=[dmc.NotificationContainer(id=NOTIFICATIONS), dcc.Store(id=STATUS, data={}), dash.page_container])  # ty: ignore[invalid-argument-type] -- DMC types `theme` as a sequence, where Mantine's theme is an object
