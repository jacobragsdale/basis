"""Credit quality: what the portfolio may buy, by rating."""

from dataclasses import replace

import pandas as pd

from optim.example import model


def min_rating(portfolio: model.Portfolio, market: model.Market, params: model.Params) -> model.Portfolio:
    """Buy only bonds rated `params.min_rating` or better, or the portfolio's own floor where it is stricter. Ratings
    are an ordered category, best first, so the stricter of two floors is the smaller."""
    own = portfolio.data.portfolios["min_rating"]
    floor = pd.concat([own, pd.Series([params.min_rating], dtype=own.dtype)]).min()
    universe = portfolio.universe
    return replace(portfolio, universe=universe.assign(buyable=universe["buyable"] & (universe["rating"] <= floor).fillna(value=False)))
