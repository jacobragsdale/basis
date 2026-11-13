"""P&L: the day's P&L of the book asked for, explained by carry, rates, spread and the residual, by sector, and its
largest moves."""

import basis.dash as bd
from dash import Input, Output, callback

from basis.dash.example.data import BOOKS, EXPLAINED, MOVERS, SECTORS, SUMMARY, Factor, Position, Sector

PNL = bd.stat(PNL_ID := "pnl-total", "Day's P&L", bd.format.money("USD", 0), change=bd.format.pct(2), period="on the book", span=3, dataset=SUMMARY)
CARRY = bd.stat(CARRY_ID := "pnl-carry", "Carry", bd.format.money("USD", 0), span=3, dataset=SUMMARY)
RATES = bd.stat(RATES_ID := "pnl-rates", "Rates", bd.format.money("USD", 0), span=3, dataset=SUMMARY)
SPREAD = bd.stat(SPREAD_ID := "pnl-spread", "Spread", bd.format.money("USD", 0), span=3, dataset=SUMMARY)
SHOWN_COLUMNS = ("cusip", "book", "issuer", "par", "price", "prior_price", "pnl", "carry", "rates", "spread", "residual")
MOVER_GRID = bd.grid(
    MOVERS_ID := "pnl-movers",
    Position,
    key="cusip",
    noun="moves",
    height=None,
    totals={"par": "sum", "pnl": "sum", "carry": "sum", "rates": "sum", "spread": "sum", "residual": "sum"},
    columns={
        "cusip": bd.column(header="CUSIP", pin="left", width=118),
        "issuer": bd.column(width=180),
        "prior_price": bd.column(header="Prior"),
        "pnl": bd.column(header="Day P&L", signed=True),
        "carry": bd.column(signed=True),
        "rates": bd.column(signed=True),
        "spread": bd.column(signed=True),
        "residual": bd.column(signed=True),
        **{column.name: bd.column(hide=True) for column in Position.columns if column.name not in SHOWN_COLUMNS},
    },
    dataset=MOVERS,
)

page = bd.page(
    "/pnl",
    "P&L",
    description="The day's P&L, explained",
    filters=[bd.as_of("pnl-as-of", calendar="sifma-us"), bd.select("pnl-book", "Book", options=BOOKS)],
    datasets=[SUMMARY],
    body=[
        PNL,
        CARRY,
        RATES,
        SPREAD,
        bd.card("What made the day", bd.graph("pnl-explained", dataset=EXPLAINED, height=300), span=6),
        bd.card("By sector", bd.graph("pnl-sectors", dataset=SECTORS, height=300), span=6),
        bd.card("Largest moves", MOVER_GRID, flush=True, description="The 25 holdings whose P&L moved most, either way"),
    ],
)


@callback(
    Output(PNL_ID, "children"),
    Output(CARRY_ID, "children"),
    Output(RATES_ID, "children"),
    Output(SPREAD_ID, "children"),
    Output("pnl-explained", "figure"),
    Output("pnl-sectors", "figure"),
    Output(MOVERS_ID, "rowData"),
    Output(MOVERS_ID, "dashGridOptions"),
    Input("pnl-as-of", "value"),
    Input("pnl-book", "value"),
)
async def show(as_of: str, book: str | None) -> tuple[object, ...]:
    asked = {"as_of": bd.day(as_of), "book": book or "", "search": ""}
    summary, explained, sectors = [await bd.load(dataset, **asked) for dataset in (SUMMARY, EXPLAINED, SECTORS)]
    total = summary.iloc[0]
    day, prior = float(total["pnl"]), float(total["prior_market_value"])

    def share(factor: str) -> str | None:
        """A factor's share of the day, which is no change, so a caption, with no sign or arrow."""
        return f"{bd.text(abs(float(total[factor]) / day), bd.format.pct(0))} of the day" if day else None

    return (
        PNL.show(day, change=day / prior if prior else None),
        CARRY.show(total["carry"], caption=share("carry")),
        RATES.show(total["rates"], caption=share("rates")),
        SPREAD.show(total["spread"], caption=share("spread")),
        bd.charts.waterfall(explained, Factor, x="factor", y="pnl", total="Day's P&L"),
        bd.charts.bar(sectors, Sector, x="sector", y="pnl"),
        await bd.load_rows(MOVERS, **asked),
        await MOVER_GRID.load_totals(MOVERS, **asked),
    )
