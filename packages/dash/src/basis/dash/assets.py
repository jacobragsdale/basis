"""The one place basis registers the files it serves with every app, from the package's `static/` folder, through
Dash's hooks: each is served from the app's own origin, fingerprinted with basis-dash's version, and cached for a
year (spike 14.1, question 2). An app's own `assets/` still loads beside them, before them.

Step 16.1 adds the theme's CSS and fonts here, a font registered as `dynamic` since CSS names it, and 16.3 the grid's
functions, which join `window.dashAgGridFunctions` under `basis` names and never replace it. `register` runs once,
when `basis.dash` is imported."""

from typing import Final

import dash

NAMESPACE: Final = "basis.dash"
"""The package Dash serves the files from, which must have a `__version__`."""
STYLESHEETS: Final = ("static/basis.css",)
SCRIPTS: Final[tuple[str, ...]] = ()

_registered = False


def register() -> None:
    """Register basis's files with every app the process builds, once."""
    global _registered  # noqa: PLW0603 -- Dash's hooks are the process's, so they are registered once per process
    if _registered:
        return
    _registered = True
    dash.hooks.stylesheet([{"relative_package_path": path, "namespace": NAMESPACE} for path in STYLESHEETS])
    if SCRIPTS:
        dash.hooks.script([{"relative_package_path": path, "namespace": NAMESPACE} for path in SCRIPTS])
