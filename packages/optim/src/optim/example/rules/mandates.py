"""The portfolio's mandates, as caps on sectors on top of the params' own."""

from dataclasses import replace

import pandas as pd

from optim.example import model


def sector_caps(portfolio: model.Portfolio, market: model.Market, params: model.Params) -> model.Portfolio:
    """Each of the portfolio's sector caps, with the params' caps: where both cap a sector, the tighter applies."""
    caps = pd.concat([pd.Series(portfolio.sector_caps, dtype=float), portfolio.data.mandates.set_index("sector")["max_weight"]])
    return replace(portfolio, sector_caps=caps.groupby(level=0).min().to_dict())
