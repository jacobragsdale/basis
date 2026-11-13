"""Positions: every holding of the book asked for, as of a close, with its totals, its sectors and its yield against
duration, filtered by book and by a CUSIP or an issuer."""

import basis.dash as bd
from dash import Input, Output, State, callback

from basis.dash.example.data import BOOKS, SECTORS, SHOWN, SUMMARY, Position, Sector

MARKET_VALUE = bd.stat(MARKET_VALUE_ID := "positions-market-value", "Market value", bd.format.money("USD", 0), period="since the prior close", span=3, dataset=SUMMARY)
DV01 = bd.stat(DV01_ID := "positions-dv01", "DV01", bd.format.money("USD", 0), span=3, dataset=SUMMARY)
YIELD = bd.stat(YIELD_ID := "positions-yield", "Yield to worst", bd.format.pct(2), change=bd.format.bps(1), period="on the day", good="down", span=3, dataset=SUMMARY)
DURATION = bd.stat(DURATION_ID := "positions-duration", "Duration", bd.format.number(2), span=3, dataset=SUMMARY)
HIDDEN = ("years", "prior_price", "prior_ytw", "curve_change", "carry", "rates", "spread", "residual")
"""The columns the grid leaves out, which the P&L page explains and the export still holds."""
GRID = bd.grid(
    GRID_ID := "positions-grid",
    Position,
    key="cusip",
    noun="holdings",
    totals={
        "par": "sum",
        "market_value": "sum",
        "weight": "sum",
        "dv01": "sum",
        "pnl": "sum",
        "price": bd.weighted("par"),
        "ytw": bd.weighted("market_value"),
        "duration": bd.weighted("market_value"),
        "oas": bd.weighted("market_value"),
    },
    columns={
        "cusip": bd.column(header="CUSIP", pin="left", width=118),
        "issuer": bd.column(width=190),
        "ytw": bd.column(header="YTW"),
        "dv01": bd.column(header="DV01"),
        "oas": bd.column(header="OAS", heat=True),
        "market_value": bd.column(bar=True),
        "pnl": bd.column(header="Day P&L", signed=True),
        **{name: bd.column(hide=True) for name in HIDDEN},
    },
    exports=True,
    dataset=SHOWN,
)

page = bd.page(
    "/positions",
    "Positions",
    description="Every holding, priced at the close",
    filters=[bd.as_of("as-of", calendar="sifma-us"), bd.select("book", "Book", options=BOOKS), bd.search("search")],
    datasets=[SHOWN],
    body=[
        MARKET_VALUE,
        DV01,
        YIELD,
        DURATION,
        bd.card("Market value by sector", bd.graph("positions-sectors", dataset=SECTORS), span=6),
        bd.card("Yield against duration", bd.graph("positions-scatter", dataset=SHOWN), span=6),
        bd.card("Holdings", GRID, flush=True),
    ],
)


@callback(
    Output(GRID_ID, "rowData"),
    Output(GRID_ID, "dashGridOptions"),
    Output(MARKET_VALUE_ID, "children"),
    Output(DV01_ID, "children"),
    Output(YIELD_ID, "children"),
    Output(DURATION_ID, "children"),
    Output("positions-sectors", "figure"),
    Output("positions-scatter", "figure"),
    Input("as-of", "value"),
    Input("book", "value"),
    Input("search", "value"),
)
async def show(as_of: str, book: str | None, search: str | None) -> tuple[object, ...]:
    asked = {"as_of": bd.day(as_of), "book": book or "", "search": search or ""}
    shown, sectors, summary = await bd.load(SHOWN, **asked), await bd.load(SECTORS, **asked), await bd.load(SUMMARY, **asked)
    total = summary.iloc[0]
    return (
        await bd.load_rows(SHOWN, **asked),  # written once per value, so another caller's answer costs the loop nothing for them
        await GRID.load_totals(SHOWN, **asked),
        MARKET_VALUE.show(total["market_value"], change=total["market_value"] - total["prior_market_value"]),
        DV01.show(total["dv01"]),
        YIELD.show(total["ytw"], change=total["ytw"] - total["prior_ytw"]),
        DURATION.show(total["duration"]),
        bd.charts.bar(sectors, Sector, x="sector", y="market_value"),
        bd.charts.scatter(shown, Position, x="duration", y="ytw", label="cusip", by="sector", fit=False),
    )


@callback(Output(GRID.download, "data"), Input(GRID.export, "n_clicks"), State("as-of", "value"), State("book", "value"), State("search", "value"), prevent_initial_call=True)
async def export(_: list[int | None], as_of: str, book: str | None, search: str | None) -> dict[str, object]:
    asked = {"as_of": bd.day(as_of), "book": book or "", "search": search or ""}
    shown = await bd.load(SHOWN, **asked)
    return GRID.exported(shown, asked={"As of": as_of, "Book": book or "All books", "Search": search or ""}, loaded_at=bd.loaded(SHOWN, **asked), name=f"positions-{as_of}", title="Positions")
