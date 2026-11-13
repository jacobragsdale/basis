"""Icons, a small vendored set of lucide's (lucide 1.70.0, as basis-ui vendors it; ISC and MIT, in
`static/lucide-LICENSE.txt`), drawn as basis-ui draws them: a 24-unit SVG stroked at 1.75 units. Dash's `html` has no
SVG elements, so each icon is a CSS mask over a data URI, `static/icons.css` (written by `python -m basis.dash.icons`),
whose fill is the text's colour: an icon makes no request, and takes the colour and size of the text it sits in.

    bd.icon("download")                     # beside a label, which names it, so it is hidden from assistive technology
    bd.icon("refresh-cw", label="Reload")   # alone, so it is named

`NAMES` lists the set. Reads and writes nothing."""

from typing import Final
from urllib.parse import quote

from basis.dash.errors import DeclarationError
from basis.dash.theme import STATIC
from dash import html

SHAPES: Final[dict[str, tuple[str, ...]]] = {
    "arrow-down": ('<path d="M12 5v14" />', '<path d="m19 12-7 7-7-7" />'),
    "arrow-up": ('<path d="m5 12 7-7 7 7" />', '<path d="M12 19V5" />'),
    "calendar": ('<path d="M8 2v3" />', '<path d="M16 2v3" />', '<rect x="3" y="3" width="18" height="18" rx="2" />', '<path d="M3 9h18" />'),
    "chart-column": ('<path d="M3 3v16a2 2 0 0 0 2 2h16" />', '<path d="M18 17V9" />', '<path d="M13 17V5" />', '<path d="M8 17v-3" />'),
    "chart-line": ('<path d="M3 3v16a2 2 0 0 0 2 2h16" />', '<path d="m19 9-5 5-4-4-3 3" />'),
    "check": ('<path d="M20 6 9 17l-5-5" />',),
    "chevron-down": ('<path d="m6 9 6 6 6-6" />',),
    "chevron-left": ('<path d="m15 18-6-6 6-6" />',),
    "chevron-right": ('<path d="m9 18 6-6-6-6" />',),
    "chevron-up": ('<path d="m18 15-6-6-6 6" />',),
    "circle-check": ('<circle cx="12" cy="12" r="10" />', '<path d="m16 9-5.5 5.5L8 12" />'),
    "circle-exclamation-point": ('<circle cx="12" cy="12" r="10" />', '<line x1="12" x2="12" y1="8" y2="12" />', '<line x1="12" x2="12.01" y1="16" y2="16" />'),
    "circle-x": ('<circle cx="12" cy="12" r="10" />', '<path d="m15 9-6 6" />', '<path d="m9 9 6 6" />'),
    "clock": ('<circle cx="12" cy="12" r="10" />', '<path d="M12 6v6l4 2" />'),
    "copy": ('<rect width="14" height="14" x="8" y="8" rx="2" ry="2" />', '<path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2" />'),
    "download": ('<path d="M12 15V3" />', '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />', '<path d="m7 10 5 5 5-5" />'),
    "external-link": ('<path d="M15 3h6v6" />', '<path d="M10 14 21 3" />', '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />'),
    "funnel": ('<path d="M10 20a1 1 0 0 0 .553.895l2 1A1 1 0 0 0 14 21v-7a2 2 0 0 1 .517-1.341L21.74 4.67A1 1 0 0 0 21 3H3a1 1 0 0 0-.742 1.67l7.225 7.989A2 2 0 0 1 10 14z" />',),
    "info": ('<circle cx="12" cy="12" r="10" />', '<path d="M12 16v-4" />', '<path d="M12 8h.01" />'),
    "loader-circle": ('<path d="M21 12a9 9 0 1 1-6.219-8.56" />',),
    "menu": ('<path d="M4 5h16" />', '<path d="M4 12h16" />', '<path d="M4 19h16" />'),
    "minus": ('<path d="M5 12h14" />',),
    "plus": ('<path d="M5 12h14" />', '<path d="M12 5v14" />'),
    "refresh-cw": (
        '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8" />',
        '<path d="M21 3v5h-5" />',
        '<path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16" />',
        '<path d="M8 16H3v5" />',
    ),
    "search": ('<path d="m21 21-4.34-4.34" />', '<circle cx="11" cy="11" r="8" />'),
    "sun-moon": (
        '<path d="M12 2v2" />',
        '<path d="M14.837 16.385a6 6 0 1 1-7.223-7.222c.624-.147.97.66.715 1.248a4 4 0 0 0 5.26 5.259c.589-.255 1.396.09 1.248.715" />',
        '<path d="M16 12a4 4 0 0 0-4-4" />',
        '<path d="m19 5-1.256 1.256" />',
        '<path d="M20 12h2" />',
    ),
    "table": ('<path d="M12 3v18" />', '<rect width="18" height="18" x="3" y="3" rx="2" />', '<path d="M3 9h18" />', '<path d="M3 15h18" />'),
    "trending-down": ('<path d="M16 17h6v-6" />', '<path d="m22 17-8.5-8.5-5 5L2 7" />'),
    "trending-up": ('<path d="M16 7h6v6" />', '<path d="m22 7-8.5 8.5-5-5L2 17" />'),
    "triangle-exclamation-point": ('<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3" />', '<path d="M12 9v4" />', '<path d="M12 17h.01" />'),
    "x": ('<path d="M18 6 6 18" />', '<path d="m6 6 12 12" />'),
}
"""Each icon's shapes, as lucide draws them on its 24 by 24 grid, copied from basis-ui's `src/vendor/lucide`."""

NAMES: Final = tuple(SHAPES)
STROKE: Final = 1.75
"""basis-ui's stroke, where lucide's is 2, which reads heavier than Inter's regular weight beside it."""


def icon(name: str, *, label: str | None = None, size: str = "1em") -> html.Span:
    """Icon `name`, `size` square, in the colour of the text around it; `label` names it for assistive technology
    where it stands alone, and without one it is hidden from it."""
    if name not in SHAPES:
        msg = f"there is no icon {name!r}: use one of {', '.join(NAMES)}"
        raise DeclarationError(msg)
    style = None if size == "1em" else {"width": size, "height": size}
    if label is not None:
        return html.Span(className=f"basis-icon basis-icon-{name}", style=style, role="img", **{"aria-label": label})
    return html.Span(className=f"basis-icon basis-icon-{name}", style=style, **{"aria-hidden": "true"})


def svg(name: str) -> str:
    """Icon `name` as an SVG document."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="black" stroke-width="{STROKE}" stroke-linecap="round" stroke-linejoin="round">{"".join(SHAPES[name])}</svg>'
    )


def stylesheet() -> str:
    """The icons' CSS, `static/icons.css`: one mask for each."""
    lines = [
        "/* Written by `python -m basis.dash.icons` from basis.dash.icons' table, lucide's shapes; do not edit it by hand. */",
        "",
        ".basis-icon {",
        "  display: inline-block;",
        "  flex: none;",
        "  width: 1em;",
        "  height: 1em;",
        "  vertical-align: -0.125em;",
        "  background-color: currentColor;",
        "  -webkit-mask: var(--basis-icon) center / contain no-repeat;",
        "  mask: var(--basis-icon) center / contain no-repeat;",
        "}",
        "",
    ]
    for name in NAMES:
        lines += [f".basis-icon-{name} {{", f'  --basis-icon: url("data:image/svg+xml,{quote(svg(name), safe=" =:/,.-")}");', "}"]
    return "\n".join([*lines, ""])


if __name__ == "__main__":
    (STATIC / "icons.css").write_text(stylesheet(), encoding="utf-8", newline="\n")
