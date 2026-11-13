"""The holdings' rules, as pure functions of frames: each takes the frames a view loads, and its own params, and
returns a frame of its row. Nothing here imports Dash or basis, or reads anything, so a job could run them as they
stand.

A day's P&L is explained the way a desk reads it: carry, a day's coupon; rates, the move of the Treasury curve at the
bond's tenor times its duration; spread, the rest of its yield's move times its duration; and the residual, what the
first order leaves, roll-down and convexity, so the four sum to the P&L exactly."""

from datetime import date

import pandas as pd

BASIS_POINT = 0.0001
DAYS_A_YEAR = 365.0
BUCKETS = ((2.0, "0-2y"), (5.0, "2-5y"), (10.0, "5-10y"), (20.0, "10-20y"), (float("inf"), "20y+"))
"""Each maturity bucket, by the years it ends at."""
RATINGS = ("AAA", "AA", "A", "BBB", "BB")
FACTORS = ("Carry", "Rates", "Spread", "Residual")
CLOSES = ("As of", "Prior day", "One week before", "One month before")
MOVERS = 25
"""How many of the day's largest moves a desk reads."""


def positions(holdings: pd.DataFrame, as_of: date) -> pd.DataFrame:
    """Each holding with its market value, par times its clean price per hundred, its weight of the book's, its years to
    maturity from `as_of`, its DV01, the money it moves by for a basis point of yield, and its day's P&L explained."""
    market_value = holdings["par"] * holdings["price"] / 100
    prior = holdings["par"] * holdings["prior_price"] / 100
    carry = holdings["par"] * holdings["coupon"] / DAYS_A_YEAR
    pnl = market_value - prior + carry
    rates = -holdings["duration"] * holdings["curve_change"] * prior
    spread = -holdings["duration"] * (holdings["ytw"] - holdings["prior_ytw"] - holdings["curve_change"]) * prior
    years = (pd.to_datetime(holdings["maturity"]) - pd.Timestamp(as_of)).dt.days / 365.25
    made = holdings.assign(
        market_value=market_value, years=years, dv01=market_value * holdings["duration"] * BASIS_POINT, pnl=pnl, carry=carry, rates=rates, spread=spread, residual=pnl - carry - rates - spread
    )
    return _weighted(made)


def shown(positions: pd.DataFrame, book: str, search: str) -> pd.DataFrame:
    """The positions of `book`, or every book's where it is empty, whose CUSIP or issuer holds `search`, ignoring
    case, each with its weight of their market value."""
    kept = positions
    if book:
        kept = kept[kept["book"] == book]
    if text := search.strip().casefold():
        kept = kept[kept["cusip"].str.casefold().str.contains(text, regex=False) | kept["issuer"].str.casefold().str.contains(text, regex=False)]
    return _weighted(kept)


def _weighted(positions: pd.DataFrame) -> pd.DataFrame:
    total = positions["market_value"].sum()
    return positions.assign(weight=positions["market_value"] / total if total else 0.0)


def by_sector(positions: pd.DataFrame) -> pd.DataFrame:
    """Each sector's market value, its weight, its DV01 and its day's P&L, and its yield and duration weighted by market
    value, the largest first."""
    return _grouped(positions, "sector").sort_values("market_value", ascending=False, ignore_index=True)


def by_rating(positions: pd.DataFrame) -> pd.DataFrame:
    """Each rating's market value, its weight, its DV01 and its day's P&L, from AAA down."""
    found = _grouped(positions, "rating")
    return found.assign(_order=found["rating"].map({rating: index for index, rating in enumerate(RATINGS)})).sort_values("_order", ignore_index=True).drop(columns="_order")


def by_maturity(positions: pd.DataFrame) -> pd.DataFrame:
    """Each maturity bucket's market value, its weight, its DV01 and its day's P&L, from the shortest; a bucket the
    positions leave empty is left out."""
    ends = [end for end, _ in BUCKETS]
    names = [name for _, name in BUCKETS]
    bucket = pd.cut(positions["years"], bins=[float("-inf"), *ends], labels=names, right=False).astype(str)
    found = _grouped(positions.assign(bucket=bucket), "bucket")
    return found.assign(_order=found["bucket"].map({name: index for index, name in enumerate(names)})).sort_values("_order", ignore_index=True).drop(columns="_order")


def by_issuer(positions: pd.DataFrame) -> pd.DataFrame:
    """Each issuer's market value, its weight, its DV01 and its day's P&L, its sector, and its yield and duration
    weighted by market value, the largest first."""
    sectors = positions.groupby("issuer")["sector"].first()
    found = _grouped(positions, "issuer")
    return found.assign(sector=found["issuer"].map(sectors)).sort_values("market_value", ascending=False, ignore_index=True)


def _grouped(positions: pd.DataFrame, by: str) -> pd.DataFrame:
    weighted = positions.assign(ytw_mv=positions["ytw"] * positions["market_value"], duration_mv=positions["duration"] * positions["market_value"])
    groups = weighted.groupby(by, as_index=False).agg(
        holdings=("cusip", "size"), market_value=("market_value", "sum"), dv01=("dv01", "sum"), pnl=("pnl", "sum"), ytw_mv=("ytw_mv", "sum"), duration_mv=("duration_mv", "sum")
    )
    total = groups["market_value"].sum()
    columns = {
        by: groups[by].astype(str),
        "holdings": groups["holdings"].astype(float),
        "market_value": groups["market_value"],
        "weight": groups["market_value"] / total if total else 0.0,
        "dv01": groups["dv01"],
        "pnl": groups["pnl"],
        "ytw": groups["ytw_mv"] / groups["market_value"],
        "duration": groups["duration_mv"] / groups["market_value"],
    }
    return pd.DataFrame(columns)


def summary(positions: pd.DataFrame) -> pd.DataFrame:
    """The positions' totals, in one row: how many, their market value now and at the prior close, DV01 and day's P&L
    with its explanation, and their yield, now and at the prior close, and duration, weighted by market value, or
    missing where they hold none."""
    market_value = float(positions["market_value"].sum())

    def weighted(column: str) -> float:
        return float((positions[column] * positions["market_value"]).sum()) / market_value if market_value else float("nan")

    prior = positions["par"] * positions["prior_price"] / 100
    prior_value = float(prior.sum())
    prior_ytw = float((positions["prior_ytw"] * prior).sum()) / prior_value if prior_value else float("nan")
    totals = {name: [float(positions[name].sum())] for name in ("dv01", "pnl", "carry", "rates", "spread", "residual")}
    return pd.DataFrame(
        {
            "holdings": [float(len(positions))],
            "market_value": [market_value],
            "prior_market_value": [prior_value],
            "ytw": [weighted("ytw")],
            "prior_ytw": [prior_ytw],
            "duration": [weighted("duration")],
            **totals,
        }
    )


def explained(summary: pd.DataFrame) -> pd.DataFrame:
    """The day's P&L by what made it, a row for each of `FACTORS`, which sum to it."""
    [total] = summary.to_dict("records")
    return pd.DataFrame({"factor": list(FACTORS), "pnl": [float(total[name.lower()]) for name in FACTORS]})


def movers(positions: pd.DataFrame) -> pd.DataFrame:
    """The `MOVERS` positions whose day's P&L was largest, gain or loss."""
    return positions.assign(_size=positions["pnl"].abs()).sort_values("_size", ascending=False).head(MOVERS).drop(columns="_size").reset_index(drop=True)


def curve_moves(curve: pd.DataFrame) -> pd.DataFrame:
    """Each tenor's yield at the close asked for, and how far it moved since each earlier close."""
    wide = curve.pivot_table(index="tenor", columns="close", values="ytw", sort=False)
    at = wide[CLOSES[0]]
    columns = {
        "tenor": wide.index.astype(str),
        "ytw": at.to_numpy(),
        "day": (at - wide["Prior day"]).to_numpy(),
        "week": (at - wide["One week before"]).to_numpy(),
        "month": (at - wide["One month before"]).to_numpy(),
    }
    return pd.DataFrame(columns)
