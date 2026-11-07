"""Screens from the params: sectors and issuers the portfolio does not buy."""

from dataclasses import replace

from optim.example import model


def exclude_sectors(portfolio: model.Portfolio, market: model.Market, params: model.Params) -> model.Portfolio:
    """Never buy `params.exclude_sectors`. A bond of one the portfolio does not hold leaves the universe; one it holds
    stays, so that it may still be sold."""
    universe = portfolio.universe
    excluded = universe["sector"].isin(params.exclude_sectors)
    kept = universe[~excluded | (universe["held"] > 0)]
    return replace(portfolio, universe=kept.assign(buyable=kept["buyable"] & ~excluded[kept.index]))


def exclude_issuers(portfolio: model.Portfolio, market: model.Market, params: model.Params) -> model.Portfolio:
    """Never buy `params.exclude_issuers`, as a restricted list says."""
    universe = portfolio.universe
    return replace(portfolio, universe=universe.assign(buyable=universe["buyable"] & ~universe["issuer"].isin(params.exclude_issuers)))
