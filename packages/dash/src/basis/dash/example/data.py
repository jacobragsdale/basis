"""The holdings app's data: its rows, the warehouse it reads, and every dataset its pages load. The warehouse answers
two queries, by the day asked for, the holdings and the Treasury curve; every other dataset is a view of them,
computed by `domain.py` and kept in the cache, so the pages of one day share two queries, and their numbers are one
set of numbers."""

from datetime import date, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

import basis.dash as bd

from basis.dash.example import domain

BOOKS = ("Core Rates", "Credit", "Liquidity", "Municipals")
NEW_YORK = ZoneInfo("America/New_York")
"""The desk's zone, whose yesterday `basis smoke` loads the holdings of."""

Money = Annotated[float, bd.format.money("USD", 0)]
Yield = Annotated[float, bd.format.pct(3)]
Years = Annotated[float, bd.format.number(2)]
Count = Annotated[float, bd.format.number(0)]
Weight = Annotated[float, bd.format.pct(1)]
Move = Annotated[float, bd.format.bps(1)]


class Holding(bd.Row):
    book: str
    cusip: str
    issuer: str
    sector: str
    rating: str
    coupon: Yield
    maturity: date
    par: Money
    price: Annotated[float, bd.format.price(3)]
    prior_price: Annotated[float, bd.format.price(3)]
    ytw: Yield
    prior_ytw: Yield
    curve_change: Move
    duration: Years
    oas: Annotated[float, bd.format.bps(0)] | None


class Position(bd.Row):
    book: str
    cusip: str
    issuer: str
    sector: str
    rating: str
    coupon: Yield
    maturity: date
    years: Annotated[float, bd.format.number(1)]
    par: Money
    price: Annotated[float, bd.format.price(3)]
    prior_price: Annotated[float, bd.format.price(3)]
    market_value: Money
    weight: Annotated[float, bd.format.pct(2)]
    ytw: Yield
    prior_ytw: Yield
    curve_change: Move
    duration: Years
    dv01: Money
    oas: Annotated[float, bd.format.bps(0)] | None
    pnl: Money
    carry: Money
    rates: Money
    spread: Money
    residual: Money


class Sector(bd.Row):
    sector: str
    holdings: Count
    market_value: Money
    weight: Weight
    dv01: Money
    pnl: Money
    ytw: Yield
    duration: Years


class Rating(bd.Row):
    rating: str
    holdings: Count
    market_value: Money
    weight: Weight
    dv01: Money
    pnl: Money
    ytw: Yield
    duration: Years


class Bucket(bd.Row):
    bucket: str
    holdings: Count
    market_value: Money
    weight: Weight
    dv01: Money
    pnl: Money
    ytw: Yield
    duration: Years


class Issuer(bd.Row):
    issuer: str
    sector: str
    holdings: Count
    market_value: Money
    weight: Weight
    dv01: Money
    pnl: Money
    ytw: Yield
    duration: Years


class Summary(bd.Row):
    holdings: Count
    market_value: Money
    prior_market_value: Money
    ytw: Annotated[float, bd.format.pct(2)]
    prior_ytw: Annotated[float, bd.format.pct(2)]
    duration: Years
    dv01: Money
    pnl: Money
    carry: Money
    rates: Money
    spread: Money
    residual: Money


class Factor(bd.Row):
    factor: str
    pnl: Money


class Point(bd.Row):
    close: str
    tenor: str
    ytw: Yield


class Tenor(bd.Row):
    tenor: str
    ytw: Yield
    day: Move
    week: Move
    month: Move


WAREHOUSE = bd.database("warehouse", dialect="mssql", deadline_s=30, max_concurrent=4)


def _yesterday() -> dict[str, object]:
    return {"as_of": datetime.now(NEW_YORK).date() - timedelta(days=1)}


HOLDINGS = bd.query("holdings", WAREHOUSE, "sql/holdings.sql", Holding, binds={"as_of": date}, ttl_s=900, stale_s=86_400, reload_s=60, smoke=_yesterday)
CURVE = bd.query("curve", WAREHOUSE, "sql/curve.sql", Point, binds={"as_of": date}, ttl_s=900, stale_s=86_400, reload_s=60, smoke=_yesterday)

POSITIONS = bd.view("positions", domain.positions, Position, holdings=HOLDINGS)
SHOWN = bd.view("shown", domain.shown, Position, positions=POSITIONS)
SECTORS = bd.view("sectors", domain.by_sector, Sector, positions=SHOWN)
RATINGS = bd.view("ratings", domain.by_rating, Rating, positions=SHOWN)
MATURITIES = bd.view("maturities", domain.by_maturity, Bucket, positions=SHOWN)
ISSUERS = bd.view("issuers", domain.by_issuer, Issuer, positions=SHOWN)
SUMMARY = bd.view("summary", domain.summary, Summary, positions=SHOWN)
EXPLAINED = bd.view("explained", domain.explained, Factor, summary=SUMMARY)
MOVERS = bd.view("movers", domain.movers, Position, positions=SHOWN)
TENORS = bd.view("tenors", domain.curve_moves, Tenor, curve=CURVE)
