"""A page of a Dash app: `bd.page(path, title, layout=...)`, which `bd.App(pages=[...])` lists, and whose module's
callbacks are the app's. For now a page is a path, a title and a layout; step 16.2 gives `bd.page` its filters and body, and builds its title and freshness line."""

import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from basis.dash.errors import DeclarationError
from dash.development.base_component import Component

type Layout = Component | Callable[..., Component]
"""A page's layout: a component, or a function that makes one, which Dash calls with the URL's query parameters."""

_PATH = re.compile(r"^/(?:[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*)?$")


@dataclass(frozen=True)
class Page:
    path: str
    """Where the page is, under the app's prefix: `/` for the first, `/positions` for another."""
    title: str
    """The page's title, in the browser's tab and the navigation."""
    layout: Layout
    module: str = field(default="__main__", compare=False)
    """The module that declared the page, whose callbacks are the page's: a page's module holds its layout and its
    callbacks."""


def page(path: str, title: str, *, layout: Layout) -> Page:
    """A page at `path`, titled `title`, showing `layout`, declared in the module that calls it."""
    if not _PATH.match(path):
        msg = f"a page's path is / or /NAME, each part letters, digits, dots, dashes, underscores or tildes, with no slash at its end, not {path!r}: give one, as /positions"
        raise DeclarationError(msg)
    if not title.strip():
        msg = f"the page at {path} has no title: give it one, as it shows in the browser's tab"
        raise DeclarationError(msg)
    if not (isinstance(layout, Component) or callable(layout)):
        msg = f"the page at {path}'s layout is a {type(layout).__qualname__}: give a Dash component, or a function that returns one"
        raise DeclarationError(msg)
    frame = inspect.currentframe()
    caller = frame.f_back.f_globals.get("__name__") if frame is not None and frame.f_back is not None else None
    return Page(path, title, layout, caller if isinstance(caller, str) else "__main__")
