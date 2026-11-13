"""The holdings app's data: its rows, the warehouse it reads, and every dataset its pages load. The warehouse answers
one query, by the day asked for; every other dataset is a view of it, computed by `domain.py` and kept in the cache, so
the pages of one day share one query."""

from datetime import date, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

import basis.dash as bd

from basis.dash.example import domain

BOOKS = ("Core Rates", "Credit", "Liquidity", "Municipals")
NEW_YORK = ZoneInfo("America/New_York")
"""The desk's zone, whose yesterday `basis smoke` loads the holdings of."""


class Holding(bd.Row):
    book: str
    cusip: str
    issuer: str
    sector: str
    rating: str
    coupon: Annotated[float, bd.format.pct(3)]
    maturity: date
    par: Annotated[float, bd.format.money("USD", 0)]
    price: Annotated[float, bd.format.price(3)]
    ytw: Annotated[float, bd.format.pct(3)]
    duration: Annotated[float, bd.format.number(2)]
    oas: Annotated[float, bd.format.bps(0)] | None


class Position(bd.Row):
    book: str
    cusip: str
    issuer: str
    sector: str
    rating: str
    coupon: Annotated[float, bd.format.pct(3)]
    maturity: date
    par: Annotated[float, bd.format.money("USD", 0)]
    price: Annotated[float, bd.format.price(3)]
    market_value: Annotated[float, bd.format.money("USD", 0)]
    weight: Annotated[float, bd.format.pct(2)]
    ytw: Annotated[float, bd.format.pct(3)]
    duration: Annotated[float, bd.format.number(2)]
    dv01: Annotated[float, bd.format.money("USD", 0)]
    oas: Annotated[float, bd.format.bps(0)] | None


class Sector(bd.Row):
    sector: str
    holdings: Annotated[float, bd.format.number(0)]
    market_value: Annotated[float, bd.format.money("USD", 0)]
    weight: Annotated[float, bd.format.pct(1)]
    dv01: Annotated[float, bd.format.money("USD", 0)]
    ytw: Annotated[float, bd.format.pct(3)]
    duration: Annotated[float, bd.format.number(2)]


class Summary(bd.Row):
    holdings: Annotated[float, bd.format.number(0)]
    market_value: Annotated[float, bd.format.money("USD", 0)]
    dv01: Annotated[float, bd.format.money("USD", 0)]
    ytw: Annotated[float, bd.format.pct(2)]
    duration: Annotated[float, bd.format.number(2)]


WAREHOUSE = bd.database("warehouse", dialect="mssql", deadline_s=30, max_concurrent=4)

HOLDINGS = bd.query(
    "holdings", WAREHOUSE, "sql/holdings.sql", Holding, binds={"as_of": date}, ttl_s=900, stale_s=86_400, reload_s=60, smoke=lambda: {"as_of": datetime.now(NEW_YORK).date() - timedelta(days=1)}
)
POSITIONS = bd.view("positions", domain.positions, Position, holdings=HOLDINGS)
SHOWN = bd.view("shown", domain.shown, Position, positions=POSITIONS)
SECTORS = bd.view("sectors", domain.by_sector, Sector, positions=SHOWN)
SUMMARY = bd.view("summary", domain.summary, Summary, positions=SHOWN)
