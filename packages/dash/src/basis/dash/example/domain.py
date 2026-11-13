"""The holdings' rules, as pure functions of frames: each takes the frames a view loads, and its own params, and
returns a frame of its row. Nothing here imports Dash or basis, or reads anything, so a job could run them as they
stand."""

import pandas as pd

BASIS_POINT = 0.0001


def positions(holdings: pd.DataFrame) -> pd.DataFrame:
    """Each holding with its market value, par times its clean price per hundred, its weight of the book's, and its
    DV01, the money it moves by for a basis point of yield."""
    market_value = holdings["par"] * holdings["price"] / 100
    return _weighted(holdings.assign(market_value=market_value, dv01=market_value * holdings["duration"] * BASIS_POINT))


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
    """Each sector's market value, its weight, its DV01, and its yield and duration weighted by market value."""
    weighted = positions.assign(ytw_mv=positions["ytw"] * positions["market_value"], duration_mv=positions["duration"] * positions["market_value"])
    sectors = weighted.groupby("sector", as_index=False).agg(
        holdings=("cusip", "size"), market_value=("market_value", "sum"), dv01=("dv01", "sum"), ytw_mv=("ytw_mv", "sum"), duration_mv=("duration_mv", "sum")
    )
    total = sectors["market_value"].sum()
    columns = {
        "sector": sectors["sector"],
        "holdings": sectors["holdings"].astype(float),
        "market_value": sectors["market_value"],
        "weight": sectors["market_value"] / total if total else 0.0,
        "dv01": sectors["dv01"],
        "ytw": sectors["ytw_mv"] / sectors["market_value"],
        "duration": sectors["duration_mv"] / sectors["market_value"],
    }
    return pd.DataFrame(columns).sort_values("market_value", ascending=False, ignore_index=True)


def summary(positions: pd.DataFrame) -> pd.DataFrame:
    """The positions' totals, in one row: how many, their market value and DV01, and their yield and duration weighted
    by market value, or missing where they hold none."""
    market_value = float(positions["market_value"].sum())
    weighted = (lambda column: float((positions[column] * positions["market_value"]).sum()) / market_value) if market_value else (lambda _: float("nan"))
    return pd.DataFrame({"holdings": [float(len(positions))], "market_value": [market_value], "dv01": [float(positions["dv01"].sum())], "ytw": [weighted("ytw")], "duration": [weighted("duration")]})
