"""Charts: each builder a pure function of a frame and its row, tested on frames: the traces it draws, the axis ticks
and hover text written in the row's formats, the business days a daily axis keeps, the names at the lines' ends, the
empty state; and the graph's config and the template's layout, which keep a title, a legend and the mode bar apart."""

import json
import math
from datetime import date, timedelta
from typing import Annotated, Any

import pandas as pd
import plotly.graph_objects as go
import pytest

import basis.dash as bd
from basis.core.calendars import calendar
from basis.dash import charts, theme
from basis.dash.errors import DeclarationError


class Yield(bd.Row):
    as_of: date
    tenor: str
    ytw: Annotated[float, bd.format.pct(3)]


class Sector(bd.Row):
    sector: str
    book: str
    market_value: Annotated[float, bd.format.money("USD", 0)]


class Point(bd.Row):
    cusip: str
    duration: float
    oas: Annotated[float, bd.format.bps(0)]


class Driver(bd.Row):
    driver: str
    pnl: Annotated[float, bd.format.money("USD", 0)]


SIFMA = calendar("sifma-us")
DAYS = [day for day in (date(2026, 11, 2) + timedelta(days=offset) for offset in range(30)) if SIFMA.is_business_day(day)]
HISTORY = pd.DataFrame([{"as_of": day, "tenor": tenor, "ytw": base + index / 10_000} for index, day in enumerate(DAYS) for tenor, base in (("2Y", 0.04), ("10Y", 0.0425))])
SECTORS = pd.DataFrame({"sector": ["Treasury", "Treasury", "Agency", "Agency"], "book": ["Rates", "Credit", "Rates", "Credit"], "market_value": [4e8, 1e8, 2e8, -5e7]})
DRIVERS = pd.DataFrame({"driver": ["Carry", "Rates", "Spread"], "pnl": [420_000.0, -610_000.0, 260_000.0]})


def _sent(figure: go.Figure) -> Any:  # noqa: ANN401 -- a figure as JSON, of any shape
    return json.loads(figure.to_json())


def test_a_line_s_hover_text_and_ticks_are_written_in_the_row_s_format() -> None:
    shown = _sent(charts.line(HISTORY, Yield, x="as_of", y="ytw", by="tenor"))
    assert [trace["name"] for trace in shown["data"]] == ["2Y", "10Y"]
    first = shown["data"][0]
    assert first["customdata"][0] == bd.text(0.04, bd.format.pct(3)) == "4.000%"
    assert first["hovertemplate"] == "2Y: %{customdata}<extra></extra>"
    axis = shown["layout"]["yaxis"]
    assert axis["ticktext"] == ["4.0%", "4.1%", "4.2%", "4.3%", "4.4%", "4.5%"]  # a step of 0.1%, so one decimal tells them apart
    assert axis["range"] == [axis["tickvals"][0], axis["tickvals"][-1]]
    assert shown["layout"]["hovermode"] == "x unified"


def test_a_daily_axis_skips_weekends_and_the_calendar_s_holidays() -> None:
    shown = _sent(charts.line(HISTORY, Yield, x="as_of", y="ytw", by="tenor", calendar="sifma-us"))
    assert shown["layout"]["xaxis"]["rangebreaks"] == [{"bounds": ["sat", "mon"]}, {"values": ["2026-11-11", "2026-11-26"]}]  # Veterans Day, Thanksgiving


def test_up_to_four_series_are_named_at_their_lines_ends_and_more_keep_a_legend() -> None:
    shown = _sent(charts.line(HISTORY, Yield, x="as_of", y="ytw", by="tenor"))
    assert shown["layout"]["showlegend"] is False
    names = [note["text"] for note in shown["layout"]["annotations"]]
    assert names == [f'<span style="color:{theme.SERIES[0]}">\N{BOX DRAWINGS HEAVY HORIZONTAL}</span> 2Y', f'<span style="color:{theme.SERIES[1]}">\N{BOX DRAWINGS HEAVY HORIZONTAL}</span> 10Y']
    many = pd.DataFrame([{"as_of": DAYS[0], "tenor": f"{years}Y", "ytw": 0.04} for years in range(1, 7)])
    crowded = _sent(charts.line(many, Yield, x="as_of", y="ytw", by="tenor"))
    assert crowded["layout"]["showlegend"] is True
    assert "annotations" not in crowded["layout"]
    alone = _sent(charts.line(HISTORY[HISTORY.tenor == "2Y"], Yield, x="as_of", y="ytw"))
    assert alone["layout"]["showlegend"] is False  # one series: its card's title names it


def test_names_at_the_lines_ends_never_overlap() -> None:
    ends = [("2Y", 0.04), ("5Y", 0.040001), ("10Y", 0.040002), ("30Y", 0.05)]
    close = pd.DataFrame([{"as_of": day, "tenor": tenor, "ytw": value} for day in DAYS[:2] for tenor, value in ends])
    shown = _sent(charts.line(close, Yield, x="as_of", y="ytw", by="tenor"))
    shifts = {note["text"].rpartition(" ")[2]: note["yshift"] for note in shown["layout"]["annotations"]}
    assert shifts == {"2Y": 0, "5Y": 14, "10Y": 14 * 2, "30Y": 0}  # three nearly equal ends a line of text apart, the fourth where its line ends


def test_stacked_bars_are_bounded_by_their_stacks_and_say_amounts_in_millions() -> None:
    shown = _sent(charts.bar(SECTORS, Sector, x="sector", y="market_value", by="book", stacked=True))
    assert shown["layout"]["barmode"] == "relative"
    assert shown["layout"]["yaxis"]["ticktext"] == ["-$200M", "$0", "$200M", "$400M", "$600M"]  # the Treasury stack, $500M, and Agency's credit below zero
    assert shown["data"][0]["hovertemplate"] == "%{x} \N{MIDDLE DOT} Rates: %{customdata}<extra></extra>"
    assert shown["data"][0]["customdata"] == ["$400,000,000", "$200,000,000"]
    across = _sent(charts.bar(SECTORS[SECTORS.book == "Rates"], Sector, x="sector", y="market_value", orientation="h"))
    assert across["data"][0]["orientation"] == "h"
    assert across["layout"]["yaxis"]["autorange"] == "reversed"


def test_a_scatter_fits_its_points_and_names_each_in_its_hover_text() -> None:
    points = pd.DataFrame({"cusip": ["A", "B", "C"], "duration": [1.0, 2.0, 3.0], "oas": [0.01, 0.02, 0.03]})
    shown = charts.scatter(points, Point, x="duration", y="oas", label="cusip")
    assert shown.data[0].hovertext[0] == "A<br>1.00 \N{MIDDLE DOT} 100\N{NO-BREAK SPACE}bp"
    fit = shown.data[1]
    assert fit.name == "Fit"
    assert list(fit.y) == pytest.approx([0.01, 0.03])


def test_a_curve_lays_tenors_on_an_axis_of_years() -> None:
    frame = pd.DataFrame({"as_of": [DAYS[0]] * 3, "tenor": ["10Y", "3M", "2Y"], "ytw": [0.0425, 0.043, 0.0398]})
    shown = charts.curve(frame, Yield, tenor="tenor", y="ytw")
    assert list(shown.data[0].x) == [0.25, 2.0, 10.0]
    assert (shown.layout.xaxis.type, shown.layout.xaxis.ticktext) == ("log", ("3M", "2Y", "10Y"))
    assert list(shown.data[0].customdata[0]) == ["3M", "4.300%"]


@pytest.mark.parametrize(("tenor", "years"), [("3M", 0.25), ("10Y", 10.0), ("1W", 7 / 365), ("ON", 1 / 365), (5, 5.0), ("18M", 1.5)])
def test_a_tenor_is_its_years(tenor: object, years: float) -> None:
    assert charts.years(tenor) == pytest.approx(years)


def test_a_tenor_that_is_no_tenor_is_refused() -> None:
    with pytest.raises(DeclarationError, match="is no tenor"):
        charts.years("soon")


def test_a_waterfall_ends_in_its_total_and_its_axis_bounds_the_running_sum() -> None:
    shown = _sent(charts.waterfall(DRIVERS, Driver, x="driver", y="pnl"))
    trace = shown["data"][0]
    assert trace["x"] == ["Carry", "Rates", "Spread", "Total"]
    assert trace["measure"] == ["relative", "relative", "relative", "total"]
    assert trace["text"] == ["$420,000", "-$610,000", "$260,000", "$70,000"]
    assert (trace["increasing"]["marker"]["color"], trace["decreasing"]["marker"]["color"]) == (theme.SERIES[2], theme.SERIES[7])
    ticks = shown["layout"]["yaxis"]["tickvals"]
    assert ticks[0] <= -190_000 < ticks[1]
    assert ticks[-1] >= 420_000


def test_a_heatmap_takes_the_diverging_scale_about_zero_where_values_have_both_signs() -> None:
    class Cell(bd.Row):
        sector: str
        rating: str
        change: Annotated[float, bd.format.bps(1)]

    cells = pd.DataFrame({"sector": ["T", "T", "A", "A"], "rating": ["AAA", "AA", "AAA", "AA"], "change": [0.0001, -0.0002, 0.0003, math.nan]})
    shown = _sent(charts.heatmap(cells, Cell, x="rating", y="sector", z="change"))
    trace = shown["data"][0]
    assert (trace["zmid"], trace["zmin"], trace["zmax"]) == (0, -0.0003, 0.0003)
    assert trace["text"] == [["1.0\N{NO-BREAK SPACE}bp", "-2.0\N{NO-BREAK SPACE}bp"], ["3.0\N{NO-BREAK SPACE}bp", ""]]
    one_sign = _sent(charts.heatmap(cells.assign(change=[1e-4, 2e-4, 3e-4, 4e-4]), Cell, x="rating", y="sector", z="change"))["data"][0]
    assert "zmid" not in one_sign
    assert one_sign["colorscale"][0][1] == theme.srgb("seq-1", "light")


def test_a_histogram_counts_each_bin_and_names_its_range() -> None:
    points = pd.DataFrame({"cusip": list("ABCD"), "duration": [1.0] * 4, "oas": [0.0, 0.01, 0.01, 0.02]})
    shown = charts.histogram(points, Point, x="oas", bins=2)
    assert list(shown.data[0].y) == [1, 3]
    assert list(shown.data[0].hovertext) == ["0\N{NO-BREAK SPACE}bp to 100\N{NO-BREAK SPACE}bp: 1", "100\N{NO-BREAK SPACE}bp to 200\N{NO-BREAK SPACE}bp: 3"]


@pytest.mark.parametrize(
    "drawn",
    [
        lambda frame: charts.line(frame, Yield, x="as_of", y="ytw"),
        lambda frame: charts.bar(frame, Yield, x="tenor", y="ytw"),
        lambda frame: charts.curve(frame, Yield, tenor="tenor", y="ytw"),
        lambda frame: charts.histogram(frame, Yield, x="ytw"),
        lambda frame: charts.waterfall(frame, Yield, x="tenor", y="ytw"),
    ],
)
def test_an_empty_frame_draws_an_empty_state_not_empty_axes(drawn: Any) -> None:  # noqa: ANN401 -- each builder, as a case
    shown = _sent(drawn(HISTORY.iloc[0:0]))
    assert shown["data"] == []
    assert shown["layout"]["annotations"][0]["text"] == charts.EMPTY
    assert shown["layout"]["xaxis"]["visible"] is False


def test_a_chart_is_refused_a_column_its_row_does_not_have() -> None:
    with pytest.raises(DeclarationError, match="Yield has no column 'yield'"):
        charts.line(HISTORY, Yield, x="as_of", y="yield")
    with pytest.raises(DeclarationError, match="draws one column"):
        charts.line(HISTORY, Yield, x="as_of", y=["ytw", "ytw"], by="tenor")


@pytest.mark.parametrize(
    ("low", "high", "display", "shown"),
    [
        (0, 5.6e8, bd.format.money("USD", 0), ["$0", "$200M", "$400M", "$600M"]),
        (-1e4, 6e5, bd.format.money("USD", 0), ["-$200K", "$0", "$200K", "$400K", "$600K"]),
        (0.0375, 0.05, bd.format.pct(3), ["3.75%", "4.00%", "4.25%", "4.50%", "4.75%", "5.00%"]),
        (0, 0.03, bd.format.bps(0), ["0\N{NO-BREAK SPACE}bp", "100\N{NO-BREAK SPACE}bp", "200\N{NO-BREAK SPACE}bp", "300\N{NO-BREAK SPACE}bp"]),
        (98, 104, bd.format.price32(), ["98-00", "100-00", "102-00", "104-00"]),
        (5, 5, None, ["4.4", "4.6", "4.8", "5.0", "5.2", "5.4", "5.6"]),  # one value: a tenth of it either side
    ],
)
def test_ticks_are_round_values_in_the_column_s_format(low: float, high: float, display: object, shown: list[str]) -> None:
    assert charts.ticks(low, high, display)[1] == shown


def test_the_graph_shows_the_mode_bar_on_hover_alone_and_no_logo() -> None:
    made = bd.graph("graph-made").to_plotly_json()["props"]
    assert made["config"]["displayModeBar"] == "hover"
    assert made["config"]["displaylogo"] is False
    assert {"lasso2d", "select2d"} <= set(made["config"]["modeBarButtonsToRemove"])
    assert made["style"] == {"height": f"{charts.HEIGHT}px"}
    assert made["figure"]["layout"]["annotations"][0]["text"] == charts.EMPTY


def test_the_template_keeps_a_title_above_the_legend() -> None:
    layout = theme.template().layout
    assert (layout.title.x, layout.title.yref, layout.title.y, layout.title.automargin) == (0, "container", 1, True)  # at the top left, in the margin it makes room for
    assert (layout.legend.orientation, layout.legend.x, layout.legend.y, layout.legend.yanchor) == ("h", 0, 1, "bottom")  # between the title and the plot
    assert layout.legend.yref in {None, "paper"}
