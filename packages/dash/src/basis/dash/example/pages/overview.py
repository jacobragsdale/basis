"""Overview: the book at a glance, as of a close: its size, its risk and its day's P&L, where it is invested by sector,
rating and maturity, and its largest issuers."""

import basis.dash as bd
from dash import Input, Output, callback

from basis.dash.example.data import BOOKS, ISSUERS, MATURITIES, RATINGS, SECTORS, SUMMARY, Bucket, Issuer, Rating, Sector

MARKET_VALUE = bd.stat(MARKET_VALUE_ID := "overview-market-value", "Market value", bd.format.money("USD", 0), period="since the prior close", span=3, dataset=SUMMARY)
DV01 = bd.stat(DV01_ID := "overview-dv01", "DV01", bd.format.money("USD", 0), span=3, dataset=SUMMARY)
YIELD = bd.stat(YIELD_ID := "overview-yield", "Yield to worst", bd.format.pct(2), change=bd.format.bps(1), period="on the day", good="down", span=3, dataset=SUMMARY)
PNL = bd.stat(PNL_ID := "overview-pnl", "Day's P&L", bd.format.money("USD", 0), change=bd.format.pct(2), period="on the book", span=3, dataset=SUMMARY)
ISSUER_GRID = bd.grid(
    ISSUERS_ID := "overview-issuers",
    Issuer,
    key="issuer",
    noun="issuers",
    height=None,
    totals={"holdings": "sum", "market_value": "sum", "weight": "sum", "dv01": "sum", "pnl": "sum", "ytw": bd.weighted("market_value"), "duration": bd.weighted("market_value")},
    columns={"issuer": bd.column(width=200), "ytw": bd.column(header="YTW"), "dv01": bd.column(header="DV01"), "pnl": bd.column(header="Day P&L", signed=True), "weight": bd.column(bar=True)},
    dataset=ISSUERS,
)

page = bd.page(
    "/",
    "Overview",
    description="The book at the close",
    filters=[bd.as_of("overview-as-of", calendar="sifma-us"), bd.select("overview-book", "Book", options=BOOKS)],
    datasets=[SUMMARY],
    body=[
        MARKET_VALUE,
        DV01,
        YIELD,
        PNL,
        bd.card("By sector", bd.graph("overview-sectors", dataset=SECTORS), span=4, description="Weight of market value"),
        bd.card("By rating", bd.graph("overview-ratings", dataset=RATINGS), span=4, description="Weight of market value"),
        bd.card("Maturity profile", bd.graph("overview-maturities", dataset=MATURITIES), span=4, description="Market value by years to maturity"),
        bd.card("Issuers", ISSUER_GRID, flush=True),
    ],
)


@callback(
    Output(MARKET_VALUE_ID, "children"),
    Output(DV01_ID, "children"),
    Output(YIELD_ID, "children"),
    Output(PNL_ID, "children"),
    Output("overview-sectors", "figure"),
    Output("overview-ratings", "figure"),
    Output("overview-maturities", "figure"),
    Output(ISSUERS_ID, "rowData"),
    Output(ISSUERS_ID, "dashGridOptions"),
    Input("overview-as-of", "value"),
    Input("overview-book", "value"),
)
async def show(as_of: str, book: str | None) -> tuple[object, ...]:
    asked = {"as_of": bd.day(as_of), "book": book or "", "search": ""}
    summary, sectors, ratings, maturities = [await bd.load(dataset, **asked) for dataset in (SUMMARY, SECTORS, RATINGS, MATURITIES)]
    total = summary.iloc[0]
    return (
        MARKET_VALUE.show(total["market_value"], change=total["market_value"] - total["prior_market_value"]),
        DV01.show(total["dv01"]),
        YIELD.show(total["ytw"], change=total["ytw"] - total["prior_ytw"]),
        PNL.show(total["pnl"], change=total["pnl"] / total["prior_market_value"] if total["prior_market_value"] else None),
        bd.charts.bar(sectors, Sector, x="sector", y="weight", orientation="h"),
        bd.charts.bar(ratings, Rating, x="rating", y="weight"),
        bd.charts.bar(maturities, Bucket, x="bucket", y="market_value"),
        await bd.load_rows(ISSUERS, **asked),
        await ISSUER_GRID.load_totals(ISSUERS, **asked),
    )
