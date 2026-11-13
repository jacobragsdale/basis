"""basis's look for a Dash app, from one table: basis-ui's default theme, light and dark, so a Dash app and a React
app read as one product. Everything a page's colours come from is made here, from `TOKENS`:

- the CSS variables, `--basis-NAME`, under `:root[data-mantine-color-scheme="light"]` and `...="dark"`, which the
  switch flips on `<html>` in the browser alone, with Mantine's own variables pointed at them (`stylesheet`, which
  `static/theme.css` holds, written by `python -m basis.dash.theme`);
- the Mantine theme, whose primary palette is built from the tokens, since Mantine computes some colours in
  JavaScript, where a CSS variable cannot be read (`mantine`);
- the AG Grid theme, `themeQuartz.withParams` with `var(--basis-...)` values, which the browser resolves, so the grid
  switches with no re-render (`GRID`);
- the Plotly template, transparent and small, since Plotly.js parses colours itself and reads no CSS variable; basis's
  CSS (`static/basis.css`) colours a figure's text, lines and hover labels over Plotly's inline styles, and the
  series, `SERIES`, are one palette that reads on both backgrounds (`template`).

`TOKENS` is basis-ui's generated `theme.css` (`packages/ui/src/styles/theme.css`), its `:root` and `.dark` blocks,
value for value; a test holds it to a pinned copy of that file, so the two cannot drift. Plotly and Excel read no
`oklch()`, so `srgb` gives any token as sRGB, mapped into the gamut as a browser maps it. Reads and writes nothing."""

import hashlib
import json
import math
import re
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Final, Literal

import plotly.graph_objects as go
import plotly.io as pio

type Scheme = Literal["light", "dark"]
SCHEMES: Final[tuple[Scheme, ...]] = ("light", "dark")

TOKENS: Final[dict[str, tuple[str, str | None]]] = {
    "radius": ("0.375rem", None),
    "font-num": ('"Inter Tabular", "Inter Variable", ui-sans-serif, system-ui, sans-serif', None),
    "focus-width": ("2px", None),
    "focus-offset": ("1px", None),
    "shadow-raised": ("0 1px 2px 0 oklch(0.3 0.03 255 / 0.1)", "0 1px 2px 0 oklch(0 0 0 / 0.45)"),
    "shadow-floating": ("0 8px 24px -6px oklch(0.3 0.03 255 / 0.2), 0 2px 6px -2px oklch(0 0 0 / 0.1)", "0 8px 24px -6px oklch(0 0 0 / 0.6), 0 2px 6px -2px oklch(0 0 0 / 0.45)"),
    "control-xs-gap": ("0.25rem", None),
    "control-xs-height": ("1.5rem", None),
    "control-xs-icon": ("0.75rem", None),
    "control-xs-inset": ("0.375rem", None),
    "control-xs-inset-icon": ("0.25rem", None),
    "control-xs-indent": ("1.25rem", None),
    "control-sm-gap": ("0.25rem", None),
    "control-sm-height": ("1.625rem", None),
    "control-sm-icon": ("0.75rem", None),
    "control-sm-inset": ("0.5rem", None),
    "control-sm-inset-icon": ("0.375rem", None),
    "control-sm-indent": ("1.375rem", None),
    "control-md-gap": ("0.25rem", None),
    "control-md-height": ("1.75rem", None),
    "control-md-icon": ("0.875rem", None),
    "control-md-inset": ("0.5rem", None),
    "control-md-inset-icon": ("0.375rem", None),
    "control-md-indent": ("1.5rem", None),
    "control-lg-gap": ("0.375rem", None),
    "control-lg-height": ("1.875rem", None),
    "control-lg-icon": ("1rem", None),
    "control-lg-inset": ("0.625rem", None),
    "control-lg-inset-icon": ("0.5rem", None),
    "control-lg-indent": ("1.875rem", None),
    "background": ("oklch(0.971 0.0084 255)", "oklch(0.172 0.007 255)"),
    "foreground": ("oklch(0.215 0.0126 255)", "oklch(0.955 0.0126 255)"),
    "card": ("oklch(1 0 255)", "oklch(0.198 0.0084 255)"),
    "card-foreground": ("oklch(0.215 0.0126 255)", "oklch(0.955 0.0126 255)"),
    "popover": ("oklch(1 0 255)", "oklch(0.232 0.0095 255)"),
    "popover-foreground": ("oklch(0.215 0.0126 255)", "oklch(0.955 0.0126 255)"),
    "muted": ("oklch(0.95 0.0095 255)", "oklch(0.264 0.0106 255)"),
    "muted-foreground": ("oklch(0.47 0.0168 255)", "oklch(0.79 0.0168 255)"),
    "secondary": ("oklch(0.928 0.0106 255)", "oklch(0.294 0.0118 255)"),
    "secondary-hover": ("oklch(0.904 0.0118 255)", "oklch(0.336 0.0129 255)"),
    "secondary-active": ("oklch(0.868 0.0129 255)", "oklch(0.41 0.014 255)"),
    "secondary-foreground": ("oklch(0.215 0.0126 255)", "oklch(0.955 0.0126 255)"),
    "accent": ("oklch(0.928 0.0106 255)", "oklch(0.294 0.0118 255)"),
    "accent-foreground": ("oklch(0.215 0.0126 255)", "oklch(0.955 0.0126 255)"),
    "primary": ("oklch(0.475 0.2 262)", "oklch(0.72 0.1452 262)"),
    "primary-hover": ("oklch(0.43 0.1989 262)", "oklch(0.77 0.1169 262)"),
    "primary-active": ("oklch(0.385 0.178 262)", "oklch(0.82 0.0898 262)"),
    "primary-foreground": ("oklch(1 0 0)", "oklch(0.172 0.007 255)"),
    "border": ("oklch(0.868 0.0129 255)", "oklch(0.336 0.0129 255)"),
    "edge": ("oklch(0.904 0.0118 255)", "oklch(0.294 0.0118 255)"),
    "input": ("oklch(0.615 0.0154 255)", "oklch(0.565 0.0154 255)"),
    "ring": ("oklch(0.605 0.152 262)", "oklch(0.6 0.152 262)"),
    "control-off": ("oklch(0.615 0.0154 255)", "oklch(0.565 0.0154 255)"),
    "disabled": ("oklch(0.795 0.014 255)", "oklch(0.41 0.014 255)"),
    "overlay": ("oklch(0.971 0.0084 255 / 0.7)", "oklch(0.172 0.007 255 / 0.7)"),
    "sidebar": ("oklch(1 0 255)", "oklch(0.198 0.0084 255)"),
    "sidebar-foreground": ("oklch(0.215 0.0126 255)", "oklch(0.955 0.0126 255)"),
    "sidebar-primary": ("oklch(0.475 0.2 262)", "oklch(0.72 0.1452 262)"),
    "sidebar-primary-foreground": ("oklch(1 0 0)", "oklch(0.172 0.007 255)"),
    "sidebar-accent": ("oklch(0.928 0.0106 255)", "oklch(0.264 0.0106 255)"),
    "sidebar-accent-foreground": ("oklch(0.215 0.0126 255)", "oklch(0.955 0.0126 255)"),
    "sidebar-border": ("oklch(0.868 0.0129 255)", "oklch(0.336 0.0129 255)"),
    "sidebar-ring": ("oklch(0.605 0.152 262)", "oklch(0.6 0.152 262)"),
    "destructive": ("oklch(0.47 0.189 25)", "oklch(0.8 0.1143 25)"),
    "destructive-foreground": ("oklch(1 0 0)", "oklch(0.172 0.007 255)"),
    "destructive-subtle": ("oklch(0.957 0.0212 25)", "oklch(0.26 0.042 25)"),
    "destructive-subtle-hover": ("oklch(0.979 0.0102 25)", "oklch(0.213 0.021 25)"),
    "destructive-edge": ("oklch(0.79 0.1176 25)", "oklch(0.43 0.1176 25)"),
    "success": ("oklch(0.45 0.1184 152)", "oklch(0.8 0.144 152)"),
    "success-foreground": ("oklch(1 0 0)", "oklch(0.172 0.007 255)"),
    "success-subtle": ("oklch(0.957 0.032 152)", "oklch(0.26 0.032 152)"),
    "success-subtle-hover": ("oklch(0.979 0.016 152)", "oklch(0.213 0.016 152)"),
    "success-edge": ("oklch(0.79 0.0896 152)", "oklch(0.43 0.0896 152)"),
    "warning": ("oklch(0.47 0.1031 68)", "oklch(0.81 0.1479 68)"),
    "warning-foreground": ("oklch(1 0 0)", "oklch(0.172 0.007 255)"),
    "warning-subtle": ("oklch(0.957 0.0302 68)", "oklch(0.26 0.034 68)"),
    "warning-subtle-hover": ("oklch(0.979 0.0145 68)", "oklch(0.213 0.017 68)"),
    "warning-edge": ("oklch(0.79 0.0952 68)", "oklch(0.43 0.0944 68)"),
    "info": ("oklch(0.46 0.1158 245)", "oklch(0.8 0.1082 245)"),
    "info-foreground": ("oklch(1 0 0)", "oklch(0.172 0.007 255)"),
    "info-subtle": ("oklch(0.957 0.0219 245)", "oklch(0.26 0.032 245)"),
    "info-subtle-hover": ("oklch(0.979 0.0106 245)", "oklch(0.213 0.016 245)"),
    "info-edge": ("oklch(0.79 0.0896 245)", "oklch(0.43 0.0896 245)"),
    "gain": ("oklch(0.45 0.1184 152)", "oklch(0.86 0.16 152)"),
    "loss": ("oklch(0.345 0.1398 25)", "oklch(0.75 0.1505 25)"),
    "selection": ("oklch(0.934 0.0315 262)", "oklch(0.29 0.056 262)"),
    "highlight": ("oklch(0.934 0.047 68)", "oklch(0.29 0.0476 68)"),
    "env-dev": ("oklch(0.5 0.1315 152)", "oklch(0.7 0.16 152)"),
    "env-dev-foreground": ("oklch(1 0 0)", "oklch(0.172 0.007 255)"),
    "env-qa": ("oklch(0.81 0.1479 68)", "oklch(0.8 0.1569 68)"),
    "env-qa-foreground": ("oklch(0.215 0.0126 255)", "oklch(0.172 0.007 255)"),
    "env-prod": ("oklch(0.5 0.2027 25)", "oklch(0.7 0.1909 25)"),
    "env-prod-foreground": ("oklch(1 0 0)", "oklch(0.172 0.007 255)"),
    "chart-1": ("#2a78d6", "#3c7dde"),
    "chart-2": ("#ea5e4c", "#d54f00"),
    "chart-3": ("#18a06e", "#2fa172"),
    "chart-4": ("#c57f00", "#c68610"),
    "chart-5": ("#018000", "#008300"),
    "chart-6": ("#c870a0", "#d04782"),
    "chart-7": ("#5d44d4", "#9188eb"),
    "chart-8": ("#d54344", "#e96865"),
    "seq-1": ("oklch(0.93 0.0361 245)", "oklch(0.27 0.0679 245)"),
    "seq-2": ("oklch(0.86 0.0741 245)", "oklch(0.34 0.0855 245)"),
    "seq-3": ("oklch(0.79 0.1141 245)", "oklch(0.41 0.1032 245)"),
    "seq-4": ("oklch(0.72 0.1562 245)", "oklch(0.48 0.1208 245)"),
    "seq-5": ("oklch(0.65 0.16 245)", "oklch(0.55 0.1384 245)"),
    "seq-6": ("oklch(0.58 0.146 245)", "oklch(0.62 0.1561 245)"),
    "seq-7": ("oklch(0.51 0.1284 245)", "oklch(0.69 0.16 245)"),
    "seq-8": ("oklch(0.44 0.1107 245)", "oklch(0.76 0.1319 245)"),
    "seq-9": ("oklch(0.37 0.0931 245)", "oklch(0.83 0.091 245)"),
    "div-1": ("oklch(0.42 0.1057 245)", "oklch(0.82 0.0967 245)"),
    "div-2": ("oklch(0.52 0.1309 245)", "oklch(0.72 0.1562 245)"),
    "div-3": ("oklch(0.62 0.1561 245)", "oklch(0.62 0.1561 245)"),
    "div-4": ("oklch(0.72 0.1562 245)", "oklch(0.52 0.1309 245)"),
    "div-5": ("oklch(0.82 0.0967 245)", "oklch(0.42 0.1057 245)"),
    "div-6": ("oklch(0.928 0.0106 255)", "oklch(0.336 0.0129 255)"),
    "div-7": ("oklch(0.82 0.1008 25)", "oklch(0.42 0.1703 25)"),
    "div-8": ("oklch(0.72 0.1742 25)", "oklch(0.52 0.21 25)"),
    "div-9": ("oklch(0.62 0.21 25)", "oklch(0.62 0.21 25)"),
    "div-10": ("oklch(0.52 0.21 25)", "oklch(0.72 0.1742 25)"),
    "div-11": ("oklch(0.42 0.1703 25)", "oklch(0.82 0.1008 25)"),
}
"""Every custom property of basis-ui's default theme, by name, without its `--`: its light value, and its dark value,
or None where the dark scheme keeps the light one. Copied from basis-ui's `theme.css`, which a test holds it to."""

SCALE: Final[dict[str, str]] = {
    "spacing": "0.1875rem",
    "font-sans": '"Inter Variable", ui-sans-serif, system-ui, sans-serif',
    "font-mono": '"JetBrains Mono Variable", ui-monospace, monospace',
    "text-xs": "0.75rem",
    "text-sm": "0.875rem",
    "text-base": "1rem",
    "text-lg": "1.125rem",
    "text-xl": "1.25rem",
    "text-2xl": "1.5rem",
}
"""The few values of basis-ui's `@theme` block a Dash page uses, the same in both schemes: the spacing unit, which
basis-ui's compact density sets, the font stacks and the text sizes."""

SERIES: Final[tuple[str, ...]] = ("#3c7dde", "#d54f00", "#2fa172", "#c68610", "#008300", "#d04782", "#8c83e5", "#e46361")
"""A figure's series colours, in their fixed order. Plotly cannot switch them with the scheme, so they are one palette
for both: basis-ui's dark `--chart-1` to `--chart-8`, with the seventh and eighth darkened to OKLCH lightness 0.66,
inside the dark band. The `dataviz` skill's validator passes them on both cards, white and `--card`'s dark
`#131619`: lightness 0.48 to 0.67, chroma at least 0.10, 3:1 against each, and every neighbour apart to normal
vision. Its one warning is the fourth and fifth, amber and green, 7.0 apart to protanopia, which is legal with a
second encoding, as basis's charts label series at the line's end and keep a legend. A test holds the measurable
checks."""

HEAT_POLES: Final[dict[str, str]] = {"low": "oklch(0.58 0.17 25)", "high": "oklch(0.58 0.14 245)"}
"""A heatmap's poles, red below zero and blue above, chosen for both schemes, since Plotly cannot switch a scale: drawn
at `HEAT_OPACITY` over a card, each composites inside the `dataviz` validator's band for that card, at 3:1 against it,
the two apart to every kind of vision, and under the foreground's text at 4.5:1. The scale's middle is transparent, so a
value near zero takes the card's own colour, in either scheme."""
HEAT_OPACITY: Final = 0.85


def heat_scale(*, signed: bool) -> list[list[object]]:
    """A heatmap's colour scale: from the low pole, through nothing, to the high one where values have both signs; from
    nearly nothing to the high pole where they have one."""

    def colour(pole: str, alpha: float) -> str:
        red, green, blue, _ = rgba(HEAT_POLES[pole])
        return f"rgba({round(red * 255)}, {round(green * 255)}, {round(blue * 255)}, {alpha:g})"

    if not signed:
        return [[0.0, colour("high", 0.06)], [1.0, colour("high", HEAT_OPACITY)]]
    return [[0.0, colour("low", HEAT_OPACITY)], [0.4999, colour("low", 0.0)], [0.5001, colour("high", 0.0)], [1.0, colour("high", HEAT_OPACITY)]]


FONTS: Final[tuple[tuple[str, str, str], ...]] = (("Inter Variable", "inter", ""), ("Inter Tabular", "inter", '  font-feature-settings: "tnum";\n'), ("JetBrains Mono Variable", "jetbrains-mono", ""))
"""Each font face: its family, its file in `static/fonts/`, and what it adds to the face. Inter's Latin file serves
twice, the second time as `Inter Tabular`, with tabular figures on, which `--basis-font-num` names first, so digits
line up in a column, as basis-ui does it. Both fonts are under the SIL Open Font License, beside them."""

_UNICODE_RANGE: Final = "U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD"
STATIC: Final = Path(__file__).parent / "static"

MANTINE: Final[dict[str, str]] = {
    "color-body": "card",
    "color-text": "foreground",
    "color-bright": "foreground",
    "color-dimmed": "muted-foreground",
    "color-placeholder": "muted-foreground",
    "color-anchor": "primary",
    "color-default": "card",
    "color-default-hover": "secondary",
    "color-default-color": "foreground",
    "color-default-border": "input",
    "color-disabled": "muted",
    "color-disabled-color": "disabled",
    "color-disabled-border": "border",
    "color-error": "destructive",
    "color-success": "success",
    "primary-color-filled": "primary",
    "primary-color-filled-hover": "primary-hover",
    "primary-color-contrast": "primary-foreground",
    "primary-color-light-color": "primary",
    "color-basis-filled": "primary",
    "color-basis-filled-hover": "primary-hover",
    "color-basis-light-color": "primary",
    "color-basis-text": "primary",
    "color-basis-outline": "primary",
    "shadow-xs": "shadow-raised",
    "shadow-sm": "shadow-raised",
    "shadow-md": "shadow-floating",
    "shadow-lg": "shadow-floating",
    "shadow-xl": "shadow-floating",
}
"""Each of Mantine's variables basis points at a token, by name, both without their prefixes: Mantine's components then
take basis-ui's colours, and switch with them. Mantine's body colour is the one its papers, inputs and menus take, so
it is a card's, and basis's CSS gives the page its background."""

DASH: Final[dict[str, str]] = {
    "Fill-Interactive-Strong": "var(--basis-primary)",
    "Fill-Interactive-Weak": "color-mix(in oklch, var(--basis-primary) 6%, transparent)",
    "Fill-Inverse-Strong": "var(--basis-card)",
    "Fill-Inverse-strong": "var(--basis-card)",
    "Fill-Primary-Hover": "var(--basis-muted)",
    "Fill-Primary-Active": "var(--basis-secondary)",
    "Fill-Disabled": "var(--basis-muted)",
    "Stroke-Strong": "var(--basis-input)",
    "Stroke-Weak": "var(--basis-border)",
    "Text-Strong": "var(--basis-foreground)",
    "Text-Primary": "var(--basis-foreground)",
    "Text-Weak": "var(--basis-muted-foreground)",
    "Text-Disabled": "var(--basis-disabled)",
    "Tooltip-Background-Color": "var(--basis-popover)",
    "Tooltip-Border-Color": "var(--basis-border)",
}
"""Each of Dash 4's own variables, which its `dcc` components (inputs, dropdowns, date pickers, sliders, tooltips) draw
with, without its `--Dash-` prefix, pointed at basis's tokens, so they take basis-ui's colours and switch with them.
Its spacing, shading and tooltip layering stay Dash's."""

_MIXED: Final[dict[str, str]] = {
    "primary-color-light": "color-mix(in oklch, var(--basis-primary) 12%, transparent)",
    "primary-color-light-hover": "color-mix(in oklch, var(--basis-primary) 18%, transparent)",
    "color-basis-light": "color-mix(in oklch, var(--basis-primary) 12%, transparent)",
    "color-basis-light-hover": "color-mix(in oklch, var(--basis-primary) 18%, transparent)",
    "color-basis-outline-hover": "color-mix(in oklch, var(--basis-primary) 8%, transparent)",
    "radius-xs": "calc(var(--basis-radius) * 0.4)",
    "radius-sm": "calc(var(--basis-radius) * 0.6)",
    "radius-md": "calc(var(--basis-radius) * 0.8)",
    "radius-lg": "var(--basis-radius)",
    "radius-xl": "calc(var(--basis-radius) * 1.4)",
    "radius-default": "var(--mantine-radius-md)",
    "spacing-xs": "calc(var(--basis-spacing) * 2)",
    "spacing-sm": "calc(var(--basis-spacing) * 3)",
    "spacing-md": "calc(var(--basis-spacing) * 4)",
    "spacing-lg": "calc(var(--basis-spacing) * 6)",
    "spacing-xl": "calc(var(--basis-spacing) * 8)",
}
"""Mantine's variables basis gives a value made from tokens: a light primary as basis-ui mixes one, its radii from
basis-ui's one radius, as basis-ui's own `radius-sm` to `radius-xl` are, and its spacing in basis-ui's compact unit, so
a page is as dense as basis-ui's."""

_SHADES: Final[tuple[tuple[float, float], ...]] = ((0.97, 0.02), (0.93, 0.045), (0.87, 0.08), (0.8, 0.12), (0.72, 0.1452), (0.62, 0.17), (0.475, 0.2), (0.43, 0.1989), (0.385, 0.178), (0.33, 0.15))
"""Mantine's primary palette, `basis`, as OKLCH lightness and chroma at the primary's hue, 262: the seventh is the
light primary and the fifth the dark one, which `primaryShade` names, with the light hover and active after the
seventh, so a colour Mantine computes in JavaScript from the palette is basis-ui's too."""
_PRIMARY_HUE: Final = 262.0
PRIMARY_SHADE: Final[dict[Scheme, int]] = {"light": 6, "dark": 4}

GRID_PARAMS: Final[dict[str, object]] = {
    "backgroundColor": "var(--basis-card)",
    "foregroundColor": "var(--basis-card-foreground)",
    "subtleTextColor": "var(--basis-muted-foreground)",
    "borderColor": "var(--basis-border)",
    "chromeBackgroundColor": "var(--basis-muted)",
    "headerBackgroundColor": "var(--basis-muted)",
    "headerTextColor": "var(--basis-muted-foreground)",
    "headerFontWeight": 600,
    "headerFontSize": 12,
    "accentColor": "var(--basis-primary)",
    "rowHoverColor": "var(--basis-muted)",
    "selectedRowBackgroundColor": "var(--basis-selection)",
    "rangeSelectionBorderColor": "var(--basis-ring)",
    "menuBackgroundColor": "var(--basis-popover)",
    "menuTextColor": "var(--basis-popover-foreground)",
    "inputFocusBorder": {"color": "var(--basis-ring)", "width": 2},
    "focusShadow": "0 0 0 2px var(--basis-ring)",
    "fontFamily": "var(--basis-font-num)",
    "fontSize": 13,
    "iconSize": 14,
    "spacing": 6,
    "rowHeight": 28,
    "headerHeight": 32,
    "columnBorder": False,
    "headerColumnBorder": False,
    "wrapperBorderRadius": 6,
    "borderRadius": 4,
    "browserColorScheme": "inherit",
}
"""AG Grid's theme parameters: every colour a `var(--basis-...)`, which AG Grid writes into a CSS variable of its own,
so the browser resolves it and the grid switches with the scheme, with no re-render (spike 14.1, question 3); and a
grid as dense as a desk reads one, a 28 px row in Inter's tabular figures."""

GRID: Final[dict[str, str]] = {"function": "themeQuartz.withParams(" + json.dumps(GRID_PARAMS, separators=(",", ":")) + ")"}
"""The grid's `dashGridOptions["theme"]`, which dash-ag-grid evaluates: Quartz with basis's parameters. Never one of
`dash_ag_grid.themes`, which are a CDN's URLs."""


def value(name: str, scheme: Scheme) -> str:
    """Token `name`'s CSS value in `scheme`."""
    light, dark = TOKENS[name]
    return dark if scheme == "dark" and dark is not None else light


def srgb(name: str, scheme: Scheme) -> str:
    """Token `name`, a colour, as sRGB: `#rrggbb`, or `rgba(r, g, b, a)` where it is translucent, for Plotly and Excel,
    which read no `oklch()`."""
    red, green, blue, alpha = rgba(value(name, scheme))
    channels = tuple(round(channel * 255) for channel in (red, green, blue))
    if alpha < 1:
        return f"rgba({channels[0]}, {channels[1]}, {channels[2]}, {alpha:g})"
    return "#{:02x}{:02x}{:02x}".format(*channels)


_OKLCH = re.compile(r"^oklch\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*(?:/\s*([\d.]+)\s*)?\)$")
_HEX = re.compile(r"^#([0-9a-fA-F]{6})$")


def rgba(css: str) -> tuple[float, float, float, float]:
    """A CSS colour, `oklch(L C H)`, `oklch(L C H / A)` or `#rrggbb`, as sRGB channels from 0 to 1 and its alpha. An
    OKLCH colour outside sRGB loses chroma until it is inside, as CSS Color 4 maps one."""
    if found := _HEX.match(css):
        digits = found.group(1)
        return int(digits[0:2], 16) / 255, int(digits[2:4], 16) / 255, int(digits[4:6], 16) / 255, 1.0
    if not (found := _OKLCH.match(css)):
        msg = f"{css!r} is no colour basis reads: give oklch(L C H), oklch(L C H / A) or #rrggbb"
        raise ValueError(msg)
    lightness, chroma, hue = (float(part) for part in found.groups()[:3])
    alpha = float(found.group(4)) if found.group(4) is not None else 1.0
    red, green, blue = _mapped(lightness, chroma, hue)
    return red, green, blue, alpha


def _mapped(lightness: float, chroma: float, hue: float) -> tuple[float, float, float]:
    if lightness >= 1:
        return 1.0, 1.0, 1.0
    if lightness <= 0:
        return 0.0, 0.0, 0.0
    linear = _linear(lightness, chroma, hue)
    if not _inside(linear):
        low, high = 0.0, chroma
        while high - low > 1e-5:  # noqa: PLR2004 -- a hundred-thousandth of chroma, far below what an eye sees
            middle = (low + high) / 2
            low, high = (middle, high) if _inside(_linear(lightness, middle, hue)) else (low, middle)
        linear = _linear(lightness, low, hue)
    red, green, blue = (_encoded(min(max(channel, 0.0), 1.0)) for channel in linear)
    return red, green, blue


def _linear(lightness: float, chroma: float, hue: float) -> tuple[float, float, float]:
    """OKLCH to linear sRGB, by Björn Ottosson's matrices."""
    a, b = chroma * math.cos(math.radians(hue)), chroma * math.sin(math.radians(hue))
    long = math.pow(lightness + 0.3963377774 * a + 0.2158037573 * b, 3)
    medium = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    short = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (
        4.0767416621 * long - 3.3077115913 * medium + 0.2309699292 * short,
        -1.2684380046 * long + 2.6097574011 * medium - 0.3413193965 * short,
        -0.0041960863 * long - 0.7034186147 * medium + 1.7076147010 * short,
    )


def _inside(linear: tuple[float, float, float]) -> bool:
    return all(-1e-6 <= channel <= 1 + 1e-6 for channel in linear)  # noqa: PLR2004 -- a float's rounding at the gamut's edge


def _encoded(channel: float) -> float:
    return 12.92 * channel if channel <= 0.0031308 else 1.055 * math.pow(channel, 1 / 2.4) - 0.055  # noqa: PLR2004 -- sRGB's transfer function


def contrast(foreground: str, background: str) -> float:
    """WCAG 2's contrast ratio of two opaque CSS colours, from 1 to 21."""
    lighter, darker = sorted((_luminance(rgba(foreground)), _luminance(rgba(background))), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _luminance(colour: tuple[float, float, float, float]) -> float:
    def linear(channel: float) -> float:
        return channel / 12.92 if channel <= 0.04045 else math.pow((channel + 0.055) / 1.055, 2.4)  # noqa: PLR2004 -- sRGB's transfer function

    red, green, blue, _ = colour
    return 0.2126 * linear(red) + 0.7152 * linear(green) + 0.0722 * linear(blue)


def shades() -> list[str]:
    """Mantine's ten-step `basis` palette, as sRGB."""
    found = []
    for lightness, chroma in _SHADES:
        red, green, blue = _mapped(lightness, chroma, _PRIMARY_HUE)
        found.append("#{:02x}{:02x}{:02x}".format(*(round(channel * 255) for channel in (red, green, blue))))
    return found


def mantine() -> dict[str, object]:
    """The theme `dmc.MantineProvider` takes: basis's fonts, its primary palette and its radius. The colours a page
    shows come from the CSS variables, which `stylesheet` points Mantine's at."""
    return {
        "primaryColor": "basis",
        "primaryShade": dict(PRIMARY_SHADE),
        "colors": {"basis": shades()},
        "fontFamily": "var(--basis-font-sans)",
        "fontFamilyMonospace": "var(--basis-font-mono)",
        "headings": {"fontFamily": "var(--basis-font-sans)", "fontWeight": "600"},
        "defaultRadius": "md",
        "focusRing": "auto",
        "fontSmoothing": True,
        "white": srgb("card", "light"),
        "black": srgb("foreground", "light"),
    }


def template() -> go.layout.Template:
    """basis's Plotly template: transparent, in Inter's tabular figures, with the series' colours, small margins and
    quiet axes. It holds only what differs from Plotly's own, since each figure carries its template (under 0.5 KB),
    and no colour but the series', which reads on both backgrounds: basis's CSS gives the text and lines theirs, in
    either scheme. A title sits at the top left, in the top margin it makes room for, and the legend between it and the
    plot, so the two never meet at any width; the mode bar shows on hover alone (`bd.graph`'s config)."""
    return go.layout.Template(
        layout={
            "font": {"family": "Inter Tabular, sans-serif", "size": 12},
            "paper_bgcolor": "rgba(0,0,0,0)",
            "plot_bgcolor": "rgba(0,0,0,0)",
            "colorway": list(SERIES),
            "margin": {"l": 8, "r": 8, "t": 8, "b": 8},
            "title": {"x": 0, "yref": "container", "y": 1, "automargin": True, "font": {"size": 14}},
            "xaxis": {"automargin": True, "showgrid": False, "showline": True, "ticks": "outside"},
            "yaxis": {"automargin": True},
            "legend": {"orientation": "h", "x": 0, "y": 1, "yanchor": "bottom"},
        }
    )


TEMPLATE: Final = "basis"
"""The name Plotly knows basis's template by, and the process's default, so a page's own figures get it too."""


def register() -> None:
    """Make basis's template Plotly's default for the process."""
    pio.templates[TEMPLATE] = template()
    pio.templates.default = TEMPLATE


@cache
def font_name(stem: str) -> str:
    """A font's file as the CSS names it, in Dash's fingerprint form, `fonts/inter.v0m<HASH>.woff2`, so it is cached for
    a year, as the CSS is, and a new file is a new name: Dash serves `fonts/inter.woff2` for it (spike, question 2)."""
    digest = hashlib.sha256((STATIC / "fonts" / f"{stem}.woff2").read_bytes()).hexdigest()[:12]
    return f"fonts/{stem}.v0m{digest}.woff2"


def stylesheet() -> str:
    """The CSS of the tokens, `static/theme.css`: the font faces, every token as `--basis-NAME` in each scheme, and
    Mantine's and Dash's variables pointed at them."""
    lines = [
        "/* Written by `python -m basis.dash.theme` from basis.dash.theme's table, which is basis-ui's default theme; do not edit",
        "   it by hand. A test fails when it and the table differ. */",
        "",
    ]
    for family, stem, extra in FONTS:
        lines += [
            "@font-face {",
            f'  font-family: "{family}";',
            "  font-style: normal;",
            "  font-display: swap;",
            "  font-weight: 100 900;",
            *([extra.rstrip("\n")] if extra else []),
            f'  src: url("{font_name(stem)}") format("woff2-variations");',
            f"  unicode-range: {_UNICODE_RANGE};",
            "}",
            "",
        ]
    lines += [':root,\n:root[data-mantine-color-scheme="light"] {', "  color-scheme: light;"]
    lines += [f"  --basis-{name}: {light};" for name, (light, _) in TOKENS.items()]
    lines += [f"  --basis-{name}: {given};" for name, given in SCALE.items()]
    lines += ["}", "", ':root[data-mantine-color-scheme="dark"] {', "  color-scheme: dark;"]
    lines += [f"  --basis-{name}: {dark};" for name, (_, dark) in TOKENS.items() if dark is not None]
    lines += ["}", "", "html:root[data-mantine-color-scheme] {"]
    lines += [f"  --mantine-{name}: var(--basis-{token});" for name, token in MANTINE.items()]
    lines += [f"  --mantine-{name}: {given};" for name, given in _MIXED.items()]
    lines += ["  --mantine-font-family: var(--basis-font-sans);", "  --mantine-font-family-monospace: var(--basis-font-mono);"]
    lines += [f"  --Dash-{name}: {given};" for name, given in DASH.items()]
    lines += ["}", ""]
    return "\n".join(lines)


def tokens(css: str, selector: str) -> Mapping[str, str]:
    """Each custom property a CSS block sets, by name without its `--`, from the block whose selector is `selector`,
    as basis-ui's generated `theme.css` writes one."""
    found = re.search(r"^" + re.escape(selector) + r" \{\n(.*?)^\}", css, re.DOTALL | re.MULTILINE)
    if found is None:
        msg = f"the CSS has no block {selector!r}"
        raise ValueError(msg)
    return dict(re.findall(r"^\s*--([\w-]+):\s*([^;]+);", found.group(1), re.MULTILINE))


if __name__ == "__main__":
    (STATIC / "theme.css").write_text(stylesheet(), encoding="utf-8", newline="\n")
