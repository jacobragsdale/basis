"""A toy model for the core's tests of `reserve`: portfolios compete for a new issue, `NEW`, whose supply is scarce. Each
buys as much as its cash allows, from what the portfolios before it left, largest cash first."""

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import Field

import optim


class Params(optim.Params):
    supply: float = 150_000.0
    unsolved: list[str] = Field(default_factory=list)
    """Portfolios whose decision is not solved, and so is rejected and reserves nothing."""
    order: list[str] | None = None
    """The order portfolios decide in, as given; null: largest cash first."""


class Data(optim.Datasets):
    pass


@dataclass(frozen=True)
class Supply:
    left: float


@dataclass(frozen=True)
class Portfolio:
    portfolio_id: str
    cash: float


def prepare_market(data: Data, params: Params) -> Supply:  # noqa: ARG001
    return Supply(params.supply)


def prepare_portfolio(data: Data, market: Supply, params: Params) -> Portfolio:  # noqa: ARG001
    holdings = data.holdings
    return Portfolio(str(data.portfolio_id), float(holdings.loc[holdings["security_id"] == "USD", "quantity"].sum()))


def order(portfolios: Mapping[str, Portfolio], market: Supply, params: Params) -> list[str]:  # noqa: ARG001
    return params.order if params.order is not None else sorted(portfolios, key=lambda portfolio_id: (-portfolios[portfolio_id].cash, portfolio_id))


def decide(portfolio: Portfolio, market: Supply, params: Params) -> optim.Decision:
    quantity = min(portfolio.cash, market.left) // 1000 * 1000
    return optim.Decision(buy={"NEW": quantity} if quantity else {}, solved=portfolio.portfolio_id not in params.unsolved)


def reserve(market: Supply, decision: optim.Decision) -> Supply:
    return Supply(market.left - decision.buy.get("NEW", 0.0))
