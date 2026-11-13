"""basis-dash: Dash apps on Dash's FastAPI backend, which read the firm's data straight from Python. An app declares its
name, its settings, its resources and its pages, and basis builds the Dash app around them, the same in every repo:
request ids and JSON logs, OpenTelemetry traces, health and readiness, a graceful shutdown, React 19, and none of
Dash's `DASH_*` variables. An app never makes `dash.Dash` itself.

    import basis.dash as bd
    from dash import Input, Output, callback, html


    class Settings(bd.Settings):
        pass


    overview = bd.page("/", "Overview", layout=html.Div([html.Button("Go", id="go"), html.Div(id="shown")]))


    @callback(Output("shown", "children"), Input("go", "n_clicks"))
    async def show(clicks: int | None) -> str:
        return f"clicked {clicks or 0} times"


    app = bd.App(name="positions", title="Positions", settings=Settings, pages=[overview])

`basis serve` serves it, `basis dev` serves it with Dash's dev tools, and `bd.testing.client(app)` drives it in the
process. Importing `basis.dash` chooses React 19 for every app in the process and registers basis's own files; it
loads no server."""

import importlib
from typing import TYPE_CHECKING

from basis.dash import _data, _display, assets, internals
from basis.dash._data import *  # noqa: F403 -- the data lane's names, which its module lists, so the lanes never edit one file
from basis.dash._display import *  # noqa: F403 -- the display lane's names, likewise
from basis.dash.app import App
from basis.dash.errors import DeclarationError
from basis.dash.ids import NOTIFICATIONS, STATUS
from basis.dash.pages import Page, page
from basis.dash.settings import Serve, Settings
from basis.dash.version import VERSION

if TYPE_CHECKING:
    from basis.dash import testing
    from basis.dash.building import Built, build
    from basis.dash.server import serve

__version__ = VERSION
"""basis-dash's version, which Dash fingerprints the package's own files with."""

internals.choose_react()
internals.write_answers_fast()  # every callback answer in one pass of orjson, as Dash writes it (internals)
assets.register()

__all__ = ["NOTIFICATIONS", "STATUS", "VERSION", "App", "Built", "DeclarationError", "Page", "Serve", "Settings", "build", "page", "serve", "testing"]
__all__ += _data.__all__
__all__ += _display.__all__


_LAZY = {"Built": "basis.dash.building", "build": "basis.dash.building", "serve": "basis.dash.server", "testing": "basis.dash.testing"}
"""The names that load on first use, by the module that holds each: the app's machinery, the server and a test's
drive, which an app's modules never make, so declaring an app loads no server."""


def __getattr__(name: str) -> object:
    if (module := _LAZY.get(name)) is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    loaded = importlib.import_module(module)
    return loaded if name == "testing" else getattr(loaded, name)
