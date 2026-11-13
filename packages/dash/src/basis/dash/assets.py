"""The one place basis registers the files it serves with every app, from the package's `static/` folder, through
Dash's hooks: each is served from the app's own origin, fingerprinted with basis-dash's version, and cached for a
year (spike 14.1, question 2). An app's own `assets/` still loads beside them, before them.

The tokens' CSS (`static/theme.css`, which `basis.dash.theme` writes), then basis's own (`static/basis.css`); the fonts
the tokens' CSS names, each registered as `dynamic`, since only CSS names it, and named there in Dash's fingerprint
form, so a font is cached as the CSS is; and 16.3's grid functions, which join `window.dashAgGridFunctions` under
`basis` names and never replace it. `register` runs once, when `basis.dash` is imported, and makes basis's Plotly
template the process's default, so a page's own figures get it too."""

from typing import Final

import dash
from basis.dash import theme

NAMESPACE: Final = "basis.dash"
"""The package Dash serves the files from, which must have a `__version__`."""
STYLESHEETS: Final = ("static/theme.css", "static/basis.css")
FONTS: Final = tuple(f"static/fonts/{stem}.woff2" for stem in sorted({stem for _, stem, _ in theme.FONTS}))
SCRIPTS: Final[tuple[str, ...]] = ()

_registered = False


def register() -> None:
    """Register basis's files with every app the process builds, and its Plotly template, once."""
    global _registered  # noqa: PLW0603 -- Dash's hooks are the process's, so they are registered once per process
    if _registered:
        return
    _registered = True
    dash.hooks.stylesheet([{"relative_package_path": path, "namespace": NAMESPACE} for path in STYLESHEETS])
    dash.hooks.stylesheet([{"relative_package_path": path, "namespace": NAMESPACE, "dynamic": True} for path in FONTS])
    if SCRIPTS:
        dash.hooks.script([{"relative_package_path": path, "namespace": NAMESPACE} for path in SCRIPTS])
    theme.register()
