"""`bd.App`, an app's declaration: its name, its title, its settings, its resources, its pages and its fakes. basis
builds the Dash app from it (`basis.dash.build`), as basis-api builds a service's FastAPI app, so a repo's code never
makes `dash.Dash` itself, and `basis serve`, `basis dev`, `basis check` and every test build it the same way. Declaring
it imports no server."""

import inspect
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from basis.dash.errors import DeclarationError
from basis.dash.pages import Page
from basis.dash.settings import Settings

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_FAKES = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")


@dataclass(frozen=True, kw_only=True)
class App:
    name: str
    """The app's name, lower-case letters, digits and dashes, as its spans and log records carry it."""
    title: str
    """What the browser's tab and the shell's header show."""
    settings: type[Settings]
    """The app's settings, `bd.Settings` or a subclass with fields of its own."""
    resources: Sequence[object] = ()
    """What the app reads, each declared by name: phase 15's databases, folders and services."""
    pages: Sequence[Page]
    """Each page, in the order the navigation lists them; the first is usually `/`."""
    fakes: str | None = None
    """`module:function`, which returns the fakes `basis dev` runs the app on (phase 15), or None for an app that
    reads nothing."""
    module: str = field(init=False, compare=False)
    """The module that declared the app, whose folder holds its `assets/`."""

    def __post_init__(self) -> None:
        if not _NAME.match(self.name):
            msg = f"an app's name is lower-case letters, digits and dashes, starting with a letter, not {self.name!r}: rename it"
            raise DeclarationError(msg)
        if not self.title.strip():
            msg = f"app {self.name} has no title: give it one, as its header shows it"
            raise DeclarationError(msg)
        if not (isinstance(self.settings, type) and issubclass(self.settings, Settings)):
            msg = f"app {self.name}'s settings are {self.settings!r}: give bd.Settings, or a subclass of it"
            raise DeclarationError(msg)
        if not self.pages or not all(isinstance(page, Page) for page in self.pages):
            msg = f"app {self.name} lists no pages, or something that is not one: list each, made with bd.page(PATH, TITLE, layout=...)"
            raise DeclarationError(msg)
        paths = [page.path for page in self.pages]
        if repeated := sorted({path for path in paths if paths.count(path) > 1}):
            msg = f"app {self.name} has more than one page at {', '.join(repeated)}: give each page a path of its own"
            raise DeclarationError(msg)
        if self.fakes is not None and not _FAKES.match(self.fakes):
            msg = f"app {self.name}'s fakes are {self.fakes!r}: name the function that returns them, as positions.dev:fakes"
            raise DeclarationError(msg)
        object.__setattr__(self, "pages", tuple(self.pages))
        object.__setattr__(self, "resources", tuple(self.resources))
        object.__setattr__(self, "module", _caller())


def _caller() -> str:
    """The module that called `App(...)`: past `__post_init__` and the dataclass's `__init__`."""
    frame = inspect.currentframe()
    for _ in range(3):
        frame = frame.f_back if frame is not None else None
    name = frame.f_globals.get("__name__") if frame is not None else None
    return name if isinstance(name, str) else "__main__"
