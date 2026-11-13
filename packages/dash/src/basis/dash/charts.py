"""Charts: Plotly, under basis's template, built by pure functions, a frame and its row in and a figure out, for the
charts a fixed income desk draws. Each reads its columns' display formats from the row, so an axis, a label and hover
text say a number as the grid says it:

    bd.charts.line(curve_history, Yield, x="as_of", y="ytw", by="tenor", calendar="sifma-us")
    bd.charts.bar(sectors, Sector, x="sector", y="weight")
    bd.charts.waterfall(attribution, Attribution, x="driver", y="pnl")

- **Text stays level.** A long category name wraps onto two lines, and a narrow axis has fewer ticks, before any is
  turned.
- **Axes** have ticks of round values, worked out here and written by `bd.text` in the column's format, with as many
  decimals as tell them apart, an amount in thousands, millions or billions where it is large: "$25M", "4.5%", "120
  bp", "99-16".
- **Hover text** is written here too, by the column's own format, so it rounds as the grid does.
- **Series** take the template's colours in their fixed order. A chart of up to four series names each at its line's
  end, and of more keeps a legend, under the title.
- **A daily axis skips** weekends and the declared calendar's holidays (`calendar=`), so a week of prices is five
  points abreast.
- **An empty frame draws an empty state**, a sentence where the plot would be, not empty axes.

`bd.graph(id)` is the component a page lays out, with basis's config: the mode bar on hover alone, no logo, no lasso,
the figure as wide as its card. A page may still build any Plotly figure; the template applies to it too. Pure: reads
and writes nothing."""

import math
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Final, Literal

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from basis.core.calendars import calendar as named_calendar
from basis.core.display import SYMBOLS, Display, Format
from basis.core.rows import Row
from basis.core.tables import Column
from basis.dash import states, texts, theme
from basis.dash.errors import DeclarationError
from dash import dcc, html

CONFIG: Final[dcc.Graph.Config] = {
    "displayModeBar": "hover",
    "displaylogo": False,
    "responsive": True,
    "modeBarButtonsToRemove": ["select2d", "lasso2d", "autoScale2d", "zoomIn2d", "zoomOut2d"],
    "toImageButtonOptions": {"format": "png", "scale": 2},
}
"""Every graph's config: the mode bar on hover alone, so it never sits over a title or legend at rest; no Plotly
logo; none of the selection tools a desk's chart has no use for; and a figure as wide as its card."""
HEIGHT: Final = 280
"""A graph's height in pixels, unless the page gives another."""
EMPTY: Final = "No data for this view"
LABELLED: Final = 4
"""The most series a chart names at their lines' ends; more keep a legend."""
TICKS: Final = 5
"""About how many ticks a value axis has."""
HISTOGRAM_TICKS: Final = 3
"""About how many a histogram's has, so its ticks stay level in a narrow card."""
WRAP: Final = 7
"""The longest category name a bar's axis writes on one line; a longer one breaks at its middle space or hyphen."""
FIT: Final = "#7d8796"
"""A fitted line's colour, a grey that reads at 3:1 on both cards."""
_STEPS: Final = (1.0, 2.0, 2.5, 5.0, 10.0)
_ABBREVIATIONS: Final = (("B", 1e9), ("M", 1e6), ("K", 1e3))
_TENOR_UNITS: Final = {"D": 1 / 365, "W": 7 / 365, "M": 1 / 12, "Y": 1.0}
_MOST_DECIMALS: Final = 6
_LABEL_GAP: Final = 14
"""The least distance between two names at the lines' ends, in pixels."""
_PLOT_HEIGHT: Final = HEIGHT - 60
"""About how tall a graph's plot is, inside its margins and ticks, which spaces the names at the lines' ends."""
_CELL_LABELS: Final = 200
"""The most cells a heatmap writes its values in."""
type Orientation = Literal["v", "h"]


def graph(id: str, *, figure: go.Figure | None = None, height: int = HEIGHT, dataset: object = None) -> html.Div:  # noqa: A002 -- a component's id, as Dash names it
    """A graph at `id`, `height` pixels tall, showing `figure`, or nothing until a callback gives one, with its marks
    at its top right, stale by `dataset` or not updated (`basis.dash.states`)."""
    shown = dcc.Graph(id=id, figure=(figure if figure is not None else empty("")).to_plotly_json(), config=CONFIG, style={"height": f"{height}px"}, className="basis-graph")
    marks = html.Div(states.marks(id, "figure", dataset), className="basis-marks basis-graph-marks")
    return html.Div([marks, states.loading(shown, id, ["figure"]), states.failed_body(id, "figure", shown=figure is not None)], className="basis-graph-frame basis-state-frame")


def empty(message: str = EMPTY) -> go.Figure:
    """A figure with no data: `message` where the plot would be, and no axes."""
    hidden = {"visible": False, "showgrid": False, "zeroline": False}
    shown = go.Figure()
    shown.update_layout(xaxis=hidden, yaxis=hidden, annotations=[{"text": message, "showarrow": False, "x": 0.5, "y": 0.5, "xref": "paper", "yref": "paper"}])
    return shown


def line(frame: pd.DataFrame, row: type[Row], *, x: str, y: str | Sequence[str], by: str | None = None, calendar: str | None = None) -> go.Figure:
    """A time series: `y`, one column or several, against `x`, a date, a time or a number; with `by`, one line for each
    of its values. `calendar` names the business days a daily axis keeps."""
    names = [y] if isinstance(y, str) else list(y)
    _columns(row, x, *names, *([by] if by is not None else []))
    if by is not None and len(names) != 1:
        msg = f"a line chart by {by!r} draws one column, not {names}: give y one column, or leave by out"
        raise DeclarationError(msg)
    series = _series(frame, names, by)
    if not series:
        return empty()
    display = _display(row, names[0])
    shown = go.Figure()
    for name, part, column in series:
        values = part[column].astype("float64")
        shown.add_trace(
            go.Scatter(
                x=part[x],
                y=values,
                name=name,
                mode="lines",
                line={"width": 2},
                customdata=[texts.text(value, display) for value in values],
                hovertemplate=f"{_escaped(name)}: %{{customdata}}<extra></extra>",
            )
        )
    everything = pd.concat([part[column] for _, part, column in series]).astype("float64")
    shown.update_layout(hovermode="x unified", yaxis=_value_axis(everything, display))
    _x_axis(shown, frame[x], row.column(x), calendar)
    _name_ends(shown, series, x, len(series))
    return shown


def bar(frame: pd.DataFrame, row: type[Row], *, x: str, y: str, by: str | None = None, stacked: bool = False, orientation: Orientation = "v") -> go.Figure:
    """Bars of `y` for each `x`, in the frame's order; with `by`, a bar for each of its values, side by side, or stacked
    where `stacked`. `orientation="h"` lays them across, the categories down the side, first at the top."""
    _columns(row, x, y, *([by] if by is not None else []))
    series = _series(frame, [y], by)
    if not series:
        return empty()
    display = _display(row, y)
    shown = go.Figure()
    across = orientation == "h"
    for name, part, column in series:
        values = part[column].astype("float64")
        categories = part[x].astype(str) if across else part[x].astype(str).map(wrapped)
        texts_shown = [texts.text(value, display) for value in values]
        shown.add_trace(
            go.Bar(
                x=values if across else categories,
                y=categories if across else values,
                name=name,
                orientation=orientation,
                customdata=texts_shown,
                hovertemplate=(f"%{{{'y' if across else 'x'}}}" + (f" \N{MIDDLE DOT} {_escaped(name)}" if by is not None else "") + ": %{customdata}<extra></extra>"),
            )
        )
    totals = pd.concat([part[column] for _, part, column in series]).astype("float64")
    if stacked and by is not None:
        grouped = frame.assign(_value=frame[y].astype("float64")).groupby(x, sort=False)["_value"]
        totals = pd.concat([grouped.apply(lambda values: values.clip(lower=0).sum()), grouped.apply(lambda values: values.clip(upper=0).sum())])
    value_axis = _value_axis(totals, display, zero=True)
    shown.update_layout(barmode="relative" if stacked else "group", bargap=0.3, bargroupgap=0.08, barcornerradius=4, showlegend=by is not None)
    if across:
        shown.update_layout(xaxis=value_axis | {"showgrid": True, "showline": False, "ticks": ""}, yaxis={"autorange": "reversed", "showgrid": False, "showline": True, "ticks": "outside"})
    else:
        shown.update_layout(xaxis={"tickangle": 0}, yaxis=value_axis)
    return shown


def scatter(frame: pd.DataFrame, row: type[Row], *, x: str, y: str, label: str | None = None, by: str | None = None, fit: bool = True) -> go.Figure:
    """A point for each row, `y` against `x`, named by `label` in its hover text, coloured by `by`; `fit` draws the
    least-squares line through every point."""
    _columns(row, x, y, *([label] if label is not None else []), *([by] if by is not None else []))
    given = frame.dropna(subset=[x, y])
    if given.empty:
        return empty()
    across, down = _display(row, x), _display(row, y)
    shown = go.Figure()
    groups = [(None, given)] if by is None else [(str(name), part) for name, part in given.groupby(by, sort=False)]
    for name, part in groups:
        hover = [
            f"{_escaped(str(named)) + '<br>' if label is not None else ''}{texts.text(a, across)} \N{MIDDLE DOT} {texts.text(b, down)}"
            for named, a, b in zip(part[label] if label is not None else [""] * len(part), part[x], part[y], strict=True)
        ]
        shown.add_trace(go.Scatter(x=part[x], y=part[y], mode="markers", name=name or y, marker={"size": 8}, hovertext=hover, hovertemplate="%{hovertext}<extra></extra>"))
    if fit and given[x].min() != given[x].max():
        slope, intercept = np.polyfit(given[x].astype("float64"), given[y].astype("float64"), 1)
        ends = np.array([given[x].min(), given[x].max()], dtype="float64")
        shown.add_trace(go.Scatter(x=ends, y=slope * ends + intercept, mode="lines", name="Fit", line={"width": 1.5, "dash": "dash", "color": FIT}, hoverinfo="skip"))
    shown.update_layout(showlegend=by is not None, xaxis=_value_axis(given[x].astype("float64"), across) | {"showgrid": True}, yaxis=_value_axis(given[y].astype("float64"), down))
    return shown


def curve(frame: pd.DataFrame, row: type[Row], *, tenor: str, y: str, by: str | None = None) -> go.Figure:
    """A yield curve: `y` at each `tenor`, text such as `3M`, `2Y` or `30Y`, on an axis of years that widens the short
    end, a point at each tenor; with `by`, one curve for each of its values, such as two dates."""
    _columns(row, tenor, y, *([by] if by is not None else []))
    given = frame.dropna(subset=[y]).assign(_years=frame[tenor].map(years))
    if given.empty:
        return empty()
    display = _display(row, y)
    shown = go.Figure()
    series = [(y, given)] if by is None else [(str(name), part) for name, part in given.groupby(by, sort=False)]
    for name, part in series:
        ordered = part.sort_values("_years")
        values = ordered[y].astype("float64")
        shown.add_trace(
            go.Scatter(
                x=ordered["_years"],
                y=values,
                name=name,
                mode="lines+markers",
                line={"width": 2},
                marker={"size": 8},
                customdata=np.stack([ordered[tenor].astype(str), [texts.text(value, display) for value in values]], axis=-1),
                hovertemplate=f"%{{customdata[0]}}{' ' + _escaped(name) if by is not None else ''}: %{{customdata[1]}}<extra></extra>",
            )
        )
    tenors = given.drop_duplicates(tenor).sort_values("_years")
    shown.update_layout(
        hovermode="closest",
        showlegend=by is not None,
        xaxis={"type": "log", "tickmode": "array", "tickvals": tenors["_years"].tolist(), "ticktext": tenors[tenor].astype(str).tolist(), "showgrid": True},
        yaxis=_value_axis(given[y].astype("float64"), display),
    )
    return shown


def waterfall(frame: pd.DataFrame, row: type[Row], *, x: str, y: str, total: str | None = "Total") -> go.Figure:
    """A waterfall: each `x`'s change of `y` in turn, gains in green and losses in red, ending in a bar of their total
    named `total`, or none where it is None."""
    _columns(row, x, y)
    given = frame.dropna(subset=[y])
    if given.empty:
        return empty()
    display = _display(row, y)
    values = given[y].astype("float64").tolist()
    labels = [wrapped(name) for name in given[x].astype(str)]
    measures = ["relative"] * len(values)
    if total is not None:
        labels.append(total)
        values.append(math.fsum(values))
        measures.append("total")
    shown_text = [texts.text(value, display) for value in values]
    shown = go.Figure(
        go.Waterfall(
            x=labels,
            y=values,
            measure=measures,
            text=shown_text,
            textposition="outside",
            customdata=shown_text,
            hovertemplate="%{x}: %{customdata}<extra></extra>",
            increasing={"marker": {"color": theme.SERIES[2]}},
            decreasing={"marker": {"color": theme.SERIES[7]}},
            totals={"marker": {"color": theme.SERIES[0]}},
            connector={"line": {"width": 1, "color": FIT}},
        )
    )
    running = np.cumsum([value for value, measure in zip(values, measures, strict=True) if measure == "relative"])
    shown.update_traces(textfont={"size": 11}, cliponaxis=False)
    shown.update_layout(showlegend=False, xaxis={"tickangle": 0}, yaxis=_value_axis(pd.Series([0.0, *running.tolist()]), display, zero=True))
    return shown


def heatmap(frame: pd.DataFrame, row: type[Row], *, x: str, y: str, z: str) -> go.Figure:
    """A heatmap of `z` by `x` across and `y` down, in the frame's order: the diverging scale about zero where `z` has
    both signs, else the sequential one, each one scale for both schemes (`theme.heat_scale`), with each cell's value
    written in it, in the foreground's colour, where there are few."""
    _columns(row, x, y, z)
    given = frame.dropna(subset=[z])
    if given.empty:
        return empty()
    display = _display(row, z)
    table = given.pivot_table(index=y, columns=x, values=z, aggfunc="sum", sort=False)
    cells = table.to_numpy(dtype="float64")
    written = [[texts.text(value, display) for value in line] for line in cells]
    signed = bool(np.nanmin(cells) < 0 < np.nanmax(cells))
    scale = theme.heat_scale(signed=signed)
    largest = float(np.nanmax(np.abs(cells)))
    shown = go.Figure(
        go.Heatmap(
            z=cells,
            x=[str(name) for name in table.columns],
            y=[str(name) for name in table.index],
            colorscale=scale,
            zmid=0 if signed else None,
            zmin=-largest if signed else None,
            zmax=largest if signed else None,
            text=written,
            texttemplate="%{text}" if cells.size <= _CELL_LABELS else None,
            textfont={"color": theme.srgb("foreground", "light")},  # basis's CSS gives it the scheme's foreground
            customdata=written,
            hovertemplate="%{y} \N{MIDDLE DOT} %{x}: %{customdata}<extra></extra>",
            xgap=2,
            ygap=2,
            showscale=False,
        )
    )
    shown.update_layout(xaxis={"showline": False, "ticks": "", "side": "top"}, yaxis={"autorange": "reversed", "showgrid": False})
    return shown


def histogram(frame: pd.DataFrame, row: type[Row], *, x: str, bins: int = 20) -> go.Figure:
    """How `x`'s values fall, in `bins` bins of one width, each bar's hover text its range and its count."""
    _columns(row, x)
    values = frame[x].astype("float64").dropna()
    if values.empty:
        return empty()
    display = _display(row, x)
    counts, edges = np.histogram(values.to_numpy(), bins=max(1, bins))
    centres = (edges[:-1] + edges[1:]) / 2
    hover = [f"{texts.text(low, display)} to {texts.text(high, display)}: {count:,}" for low, high, count in zip(edges[:-1], edges[1:], counts, strict=True)]
    shown = go.Figure(go.Bar(x=centres, y=counts, width=np.diff(edges) * 0.92, hovertext=hover, hovertemplate="%{hovertext}<extra></extra>", name=x))
    across = _value_axis(pd.Series(edges), display, count=HISTOGRAM_TICKS)
    shown.update_layout(showlegend=False, bargap=0, xaxis=across | {"showgrid": False, "tickangle": 0}, yaxis={"rangemode": "tozero"})
    return shown


def wrapped(name: str) -> str:
    """A category's name on one line, or on two where it is longer than `WRAP` and has a space or hyphen to break at,
    the one nearest its middle, so names stay level rather than turn."""
    if len(name) <= WRAP:
        return name
    breaks = [index for index, letter in enumerate(name) if letter in " -"]
    if not breaks:
        return name
    at = min(breaks, key=lambda index: abs(index - len(name) / 2))
    return f"{_escaped(name[: at + 1].rstrip())}<br>{_escaped(name[at + 1 :])}"


def years(tenor: object) -> float:
    """A tenor's length in years: `3M` is 0.25, `10Y` is 10, `1W` a week, `ON` a day; a number is already years."""
    if isinstance(tenor, int | float):
        return float(tenor)
    text = str(tenor).strip().upper()
    if text in {"ON", "O/N"}:
        return 1 / 365
    unit, count = text[-1:], text[:-1]
    if unit not in _TENOR_UNITS or not count.replace(".", "", 1).isdigit():
        msg = f"{tenor!r} is no tenor: give a count and a unit, D, W, M or Y, such as 3M or 10Y"
        raise DeclarationError(msg)
    return float(count) * _TENOR_UNITS[unit]


def ticks(low: float, high: float, display: Display | None, *, count: int = TICKS, zero: bool = False) -> tuple[list[float], list[str]]:
    """About `count` round values from `low` to `high`, and their text in `display` with as many decimals as tell them
    apart: a value axis's ticks, which also bound it."""
    if zero:
        low, high = min(low, 0.0), max(high, 0.0)
    if not (math.isfinite(low) and math.isfinite(high)):
        return [], []
    if low == high:
        spread = abs(low) * 0.1 or 1.0
        low, high = low - spread, high + spread
    raw = (high - low) / count
    magnitude = math.pow(10, math.floor(math.log10(raw)))
    step = next(factor * magnitude for factor in _STEPS if factor * magnitude >= raw * (1 - 1e-9))
    first, last = math.floor(low / step + 1e-9), math.ceil(high / step - 1e-9)
    values = [round(index * step, 12) for index in range(first, last + 1)]
    largest = max(abs(value) for value in values)
    return values, [_tick_text(value, step, largest, display) for value in values]


def _tick_text(value: float, step: float, largest: float, display: Display | None) -> str:
    """A tick's text: in `display`, with the decimals `step` needs, an amount abbreviated by the axis's `largest` tick,
    so an axis to $600M reads "$200M", "$400M", "$600M"."""
    kind = display.kind if display is not None else "number"
    if kind == "price32" and display is not None:
        return display.text(value)
    if kind in {"pct", "bps", "price"} and display is not None:
        scaled = step * (100 if kind == "pct" else 10_000 if kind == "bps" else 1)
        return texts.text(value, Display(kind=kind, decimals=_decimals(scaled)))
    abbreviation, size = next(((name, size) for name, size in _ABBREVIATIONS if largest >= size), ("", 1.0))
    if value == 0:
        shown = "0"
    elif abbreviation:
        shown = texts.text(value, Format(decimals=_decimals(step / size), abbreviate=abbreviation))
    else:
        shown = texts.text(value, Format(decimals=_decimals(step)))
    if kind == "money" and display is not None and display.currency is not None:
        symbol = SYMBOLS.get(display.currency, f"{display.currency}\N{NO-BREAK SPACE}")
        sign = "-" if shown.startswith("-") else ""
        return f"{sign}{symbol}{shown.removeprefix('-')}"
    return shown


def _decimals(step: float) -> int:
    """The fewest decimals that write every multiple of `step` exactly: 2 for a step of 0.25, 0 for one of 50."""
    for decimals in range(_MOST_DECIMALS + 1):
        scaled = step * math.pow(10, decimals)
        if abs(scaled - round(scaled)) < 1e-6 * max(1.0, scaled):
            return decimals
    return _MOST_DECIMALS


def _value_axis(values: pd.Series, display: Display | None, *, zero: bool = False, count: int = TICKS) -> dict[str, object]:
    given = values.dropna()
    if given.empty:
        return {}
    tickvals, ticktext = ticks(float(given.min()), float(given.max()), display, zero=zero, count=count)
    if not tickvals:
        return {}
    return {"tickmode": "array", "tickvals": tickvals, "ticktext": ticktext, "range": [tickvals[0], tickvals[-1]], "zeroline": zero}


def _x_axis(shown: go.Figure, values: pd.Series, column: Column, calendar: str | None) -> None:
    if column.kind not in {"date", "datetime"}:
        if column.kind == "float":
            shown.update_layout(xaxis=_value_axis(values.astype("float64"), column.display))
        return
    shown.update_layout(xaxis={"hoverformat": "%Y-%m-%d" if column.kind == "date" else "%Y-%m-%d %H:%M"})
    if calendar is None:
        return
    days = sorted({_day(value) for value in values.dropna()})
    if not days:
        return
    known = named_calendar(calendar)
    closed = [day.isoformat() for day in _span(days[0], days[-1]) if day.weekday() < 5 and not known.is_business_day(day)]  # noqa: PLR2004 -- Monday to Friday
    breaks: list[dict[str, object]] = [{"bounds": ["sat", "mon"]}]
    if closed:
        breaks.append({"values": closed})
    shown.update_xaxes(rangebreaks=breaks)


def _name_ends(shown: go.Figure, series: list[tuple[str, pd.DataFrame, str]], x: str, count: int) -> None:
    """Name up to `LABELLED` series at their lines' ends, with room for the names; more keep the legend."""
    if count < 2 or count > LABELLED:  # noqa: PLR2004 -- one series needs no name; its card's title names it
        shown.update_layout(showlegend=count > 1)
        return
    ends = []
    for name, part, column in series:
        last = part.dropna(subset=[column]).tail(1)
        if not last.empty:
            ends.append((float(last[column].iloc[0]), last[x].iloc[0], name))
    axis = shown.layout.yaxis.range
    span = float(axis[1] - axis[0]) if axis is not None and axis[1] != axis[0] else 1.0
    placed: list[float] = []
    notes = []
    for value, at, name in sorted(ends, key=lambda end: end[0]):
        wanted = (value - (axis[0] if axis is not None else 0.0)) / span * _PLOT_HEIGHT
        shown_at = max(wanted, placed[-1] + _LABEL_GAP) if placed else wanted
        placed.append(shown_at)
        colour = theme.SERIES[[named for named, _, _ in series].index(name) % len(theme.SERIES)]
        notes.append(
            {
                "x": at,
                "y": value,
                "text": f'<span style="color:{colour}">\N{BOX DRAWINGS HEAVY HORIZONTAL}</span> {_escaped(name)}',
                "showarrow": False,
                "xanchor": "left",
                "xshift": 6,
                "yshift": round(shown_at - wanted),
                "yanchor": "middle",
            }
        )
    widest = max((len(name) for name, _, _ in series), default=0)
    shown.update_layout(showlegend=False, annotations=notes, margin={"r": 28 + round(widest * 7.2)})


def _series(frame: pd.DataFrame, names: list[str], by: str | None) -> list[tuple[str, pd.DataFrame, str]]:
    """Each series to draw: its name, its rows in the frame's order, and its column; none where nothing is given."""
    given = frame.dropna(subset=names, how="all")
    if given.empty:
        return []
    if by is None:
        return [(name, given, name) for name in names if given[name].notna().any()]
    return [(str(value), part, names[0]) for value, part in given.groupby(by, sort=False) if part[names[0]].notna().any()]


def _columns(row: type[Row], *names: str) -> None:
    if not (isinstance(row, type) and issubclass(row, Row)):
        msg = f"a chart's row is {row!r}: give a bd.Row subclass, whose columns it draws"
        raise DeclarationError(msg)
    known = row.names()
    if missing := [name for name in names if name not in known]:
        msg = f"{row.__qualname__} has no column {', '.join(map(repr, missing))}: draw one of {', '.join(known)}"
        raise DeclarationError(msg)


def _display(row: type[Row], name: str) -> Display | None:
    found = row.column(name)
    return found.display if found.kind == "float" and found.display is not None else (Display(kind="number") if found.kind == "float" else None)


def _escaped(text: str) -> str:
    """Text Plotly shows as it is: its HTML-like tags and template braces escaped."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("%{", "%&#123;")


def _day(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(str(value)).date()


def _span(first: date, last: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]
