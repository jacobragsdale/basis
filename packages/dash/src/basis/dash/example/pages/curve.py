"""Curve: the Treasury curve at a close against the day, the week and the month before, and the book against it: its
holdings' yields by their years to maturity, and its DV01 by maturity bucket."""

import basis.dash as bd
from dash import Input, Output, callback

from basis.dash.example.data import BOOKS, CURVE, MATURITIES, SHOWN, TENORS, Bucket, Point, Position, Tenor

TWO = bd.stat(TWO_ID := "curve-2y", "2-year", bd.format.pct(3), change=bd.format.bps(1), period="on the day", good="down", span=3, dataset=TENORS)
TEN = bd.stat(TEN_ID := "curve-10y", "10-year", bd.format.pct(3), change=bd.format.bps(1), period="on the day", good="down", span=3, dataset=TENORS)
THIRTY = bd.stat(THIRTY_ID := "curve-30y", "30-year", bd.format.pct(3), change=bd.format.bps(1), period="on the day", good="down", span=3, dataset=TENORS)
SLOPE = bd.stat(SLOPE_ID := "curve-2s10s", "2s10s", bd.format.bps(1), period="on the day", span=3, dataset=TENORS)
TENOR_GRID = bd.grid(
    TENORS_ID := "curve-tenors",
    Tenor,
    key="tenor",
    height=None,
    search=False,
    noun="tenors",
    columns={"tenor": bd.column(width=84), "ytw": bd.column(header="Yield"), "day": bd.column(header="1D"), "week": bd.column(header="1W"), "month": bd.column(header="1M")},
    dataset=TENORS,
)

page = bd.page(
    "/curve",
    "Curve",
    description="The Treasury curve, and the book against it",
    filters=[bd.as_of("curve-as-of", calendar="sifma-us"), bd.select("curve-book", "Book", options=BOOKS)],
    datasets=[CURVE, SHOWN],
    body=[
        TWO,
        TEN,
        THIRTY,
        SLOPE,
        bd.card("Treasury curve", bd.graph("curve-chart", dataset=CURVE, height=320), span=7),
        bd.card("Moves", TENOR_GRID, span=5, flush=True, description="A rise in yield is a loss for a holder"),
        bd.card("The book against the curve", bd.graph("curve-book-chart", dataset=SHOWN), span=6, description="Each holding's yield by its years to maturity"),
        bd.card("DV01 by maturity", bd.graph("curve-dv01", dataset=MATURITIES), span=6, description="The money the book moves by for a basis point"),
    ],
)


@callback(
    Output(TWO_ID, "children"),
    Output(TEN_ID, "children"),
    Output(THIRTY_ID, "children"),
    Output(SLOPE_ID, "children"),
    Output("curve-chart", "figure"),
    Output(TENORS_ID, "rowData"),
    Output("curve-book-chart", "figure"),
    Output("curve-dv01", "figure"),
    Input("curve-as-of", "value"),
    Input("curve-book", "value"),
)
async def show(as_of: str, book: str | None) -> tuple[object, ...]:
    day = bd.day(as_of)
    asked = {"as_of": day, "book": book or "", "search": ""}
    points, tenors = await bd.load(CURVE, as_of=day), await bd.load(TENORS, as_of=day)
    shown, maturities = await bd.load(SHOWN, **asked), await bd.load(MATURITIES, **asked)
    at = {str(row["tenor"]): (float(row["ytw"]), float(row["day"])) for row in tenors.to_dict("records")}
    (two, two_day), (ten, ten_day), (thirty, thirty_day) = at["2Y"], at["10Y"], at["30Y"]
    return (
        TWO.show(two, change=two_day),
        TEN.show(ten, change=ten_day),
        THIRTY.show(thirty, change=thirty_day),
        SLOPE.show(ten - two, change=ten_day - two_day),
        bd.charts.curve(points, Point, tenor="tenor", y="ytw", by="close"),
        await bd.load_rows(TENORS, as_of=day),
        bd.charts.scatter(shown[shown["sector"] != "Municipal"], Position, x="years", y="ytw", label="cusip", by="sector", fit=False),
        bd.charts.bar(maturities, Bucket, x="bucket", y="dv01"),
    )
