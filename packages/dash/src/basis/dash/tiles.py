"""Stat tiles and badges: the headline numbers a page leads with, and the small labels beside them.

    MARKET_VALUE = bd.stat("market-value", "Market value", bd.format.money("USD", 0), change=bd.format.money("USD", 0), period="since yesterday")

    @callback(Output("market-value", "children"), Input("as-of", "value"))
    async def lead(as_of: str) -> bd.Shown:
        totals = await bd.load(TOTALS, as_of=bd.day(as_of))
        return MARKET_VALUE.show(totals.market_value.iloc[-1], change=..., history=totals.market_value)

- **A tile** is a card of the body's grid, three of its twelve columns by default: its label, its value in the format
  it declares, written on the server by `bd.text`, a signed change in the gain or the loss colour, with an arrow and
  its sign, which `good="down"` turns about for a number that should fall, such as risk, and a sparkline of its
  history. Until a callback shows it, it holds its label and a dash, in the place its value takes, so nothing moves.
- **A badge** is a word in a tone, `neutral`, `info`, `success`, `warning` or `danger`, each the token pair basis-ui
  gives it, text that reads at 4.5:1 on its fill in both schemes.

Pure: each reads and writes nothing."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

import pandas as pd
import plotly.graph_objects as go

from basis.core.display import Display
from basis.dash import states, texts, theme
from basis.dash.errors import DeclarationError
from basis.dash.icons import icon
from basis.dash.pages import COLUMNS
from dash import dcc, html
from dash.development.base_component import Component

type Tone = Literal["neutral", "info", "success", "warning", "danger"]
type Good = Literal["up", "down"]
TONES: Final[tuple[Tone, ...]] = ("neutral", "info", "success", "warning", "danger")
SPAN: Final = 3
"""A tile's columns of the body's twelve: four abreast."""
WAITING: Final = "\N{EM DASH}"
"""A tile's value until a callback shows one."""
SPARK_HEIGHT: Final = 32
SPARK_POINTS: Final = 400
"""The most points a sparkline draws; a longer history is thinned to them, evenly, keeping its last."""
SPARK: Final[dict[str, object]] = {"staticPlot": True, "displayModeBar": False, "responsive": True}

type Shown = list[Component]
"""What `Stat.show` gives a callback for the tile's `children`."""


@dataclass(frozen=True)
class Declared:
    label: str
    display: Display
    change: Display | None
    period: str | None
    good: Good


class Stat(html.Section):
    """A stat tile, declared with its format: `bd.stat(...)` makes one, and a callback fills it with `show`."""

    declared: Declared

    def show(self, value: float | None, *, change: float | None = None, history: Sequence[float] | pd.Series | None = None) -> Shown:
        """The tile's `children`: its label, `value` in its format, `change` signed in its colour, and a sparkline of
        `history`, oldest first. A missing value shows a dash."""
        return shown(self.declared, value, change=change, history=history)


def stat(
    id: str,  # noqa: A002 -- a component's id, as Dash names it
    label: str,
    display: Display,
    *,
    change: Display | None = None,
    period: str | None = None,
    good: Good = "up",
    span: int = SPAN,
    dataset: object = None,
) -> Stat:
    """A tile at `id`, named `label`, its value in `display`, its change in `change` (by default the value's own
    format) over `period`, such as "since yesterday", and `good` the way a rise is good; its marks say when it is stale,
    by `dataset`, or not updated. A callback answers its `children` at `id`."""
    if not label.strip():
        msg = f"stat {id} has no label: give it the words a person reads above its number"
        raise DeclarationError(msg)
    if not isinstance(display, Display):
        msg = f"stat {id}'s format is {display!r}: give one of bd.format's, such as bd.format.money('USD', 0)"
        raise DeclarationError(msg)
    if good not in {"up", "down"}:
        msg = f"stat {id}'s good is {good!r}: give 'up', where a rise is good, or 'down', where a fall is"
        raise DeclarationError(msg)
    if isinstance(span, bool) or not isinstance(span, int) or not 1 <= span <= COLUMNS:
        msg = f"stat {id} spans 1 to {COLUMNS} columns, not {span!r}: give a whole number in that range"
        raise DeclarationError(msg)
    declared = Declared(label, display, change, period, good)
    content = states.loading(html.Div(shown(declared, None), id=id, className="basis-stat-content"), id, ["children"])
    made = Stat(
        [html.Div(states.marks(id, "children", dataset), className="basis-marks basis-stat-marks"), content],
        className="basis-card basis-stat",
        style={"gridColumn": f"span {span}"},
        **{"aria-label": label},
    )
    object.__setattr__(made, "declared", declared)  # beside the component, which Dash never sends
    return made


def shown(declared: Declared, value: float | None, *, change: float | None = None, history: Sequence[float] | pd.Series | None = None) -> Shown:
    """A tile's children, for `value`, `change` and `history`."""
    missing = value is None or (isinstance(value, float) and math.isnan(value))
    parts: list[Component] = [html.Div(declared.label, className="basis-stat-label"), html.Div(WAITING if missing else texts.text(value, declared.display), className="basis-stat-value basis-num")]
    if change is not None and not math.isnan(change):
        parts.append(_change(declared, change))
    elif not missing:
        parts.append(html.Div(declared.period or "", className="basis-stat-change"))
    else:
        parts.append(html.Div(className="basis-stat-change"))
    if history is not None and (points := _points(history)):
        parts.append(dcc.Graph(figure=sparkline(points).to_plotly_json(), config=SPARK, className="basis-stat-spark", style={"height": f"{SPARK_HEIGHT}px"}))  # ty: ignore[invalid-argument-type] -- Plotly's config, whose keys Dash's type does not list
    return parts


def sparkline(points: Sequence[float]) -> go.Figure:
    """A sparkline's figure: one line of `points`, no axes, no hover, its last point marked."""
    last = len(points) - 1
    figure = go.Figure(
        [
            go.Scatter(y=list(points), mode="lines", line={"width": 1.5, "color": theme.SERIES[0]}, hoverinfo="skip"),
            go.Scatter(x=[last], y=[points[-1]], mode="markers", marker={"size": 5, "color": theme.SERIES[0]}, hoverinfo="skip"),
        ]
    )
    hidden = {"visible": False, "fixedrange": True}
    figure.update_layout(margin={"l": 2, "r": 4, "t": 2, "b": 2}, xaxis=hidden, yaxis=hidden, showlegend=False, height=SPARK_HEIGHT)
    return figure


def badge(text: str, tone: Tone = "neutral", *, icon_name: str | None = None) -> html.Span:
    """`text` as a badge in `tone`, with an icon of the set before it where `icon_name` names one."""
    if tone not in TONES:
        msg = f"a badge's tone is one of {', '.join(TONES)}, not {tone!r}: give one of them"
        raise DeclarationError(msg)
    content: list[Component | str] = [icon(icon_name), text] if icon_name is not None else [text]
    return html.Span(content, className=f"basis-badge basis-badge-{tone}")


def _change(declared: Declared, change: float) -> Component:
    display = declared.change or declared.display
    rising = change > 0
    shown_text = texts.text(change, display)
    if shown_text.startswith(("-", "(")):
        signed_text = shown_text  # its sign as the grid and every message write it
    elif texts.text(abs(change), display) == texts.text(0.0, display):
        signed_text = shown_text  # it rounds to nothing: no sign, no colour
        rising = None
    else:
        signed_text = f"+{shown_text}"
    if rising is None or change == 0:
        tone, arrow = "basis-flat", None
    else:
        good = rising if declared.good == "up" else not rising
        tone, arrow = ("basis-gain" if good else "basis-loss"), ("trending-up" if rising else "trending-down")
    words: list[Component | str] = [icon(arrow)] if arrow is not None else []
    words.append(html.Span(signed_text, className="basis-num"))
    if declared.period is not None:
        words.append(html.Span(declared.period, className="basis-stat-period"))
    return html.Div(words, className=f"basis-stat-change {tone}")


def _points(history: Sequence[float] | pd.Series) -> list[float]:
    values = [float(value) for value in history if value is not None and not (isinstance(value, float) and math.isnan(value))]
    if len(values) <= SPARK_POINTS:
        return values
    step = (len(values) - 1) / (SPARK_POINTS - 1)
    return [values[round(index * step)] for index in range(SPARK_POINTS)]
