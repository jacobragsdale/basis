"""The model: a taxable fixed income SMA rebalance, solved as an integer program. This file is its table of contents:
the params it takes, the data it reads, the numbers it reports, and its hooks, each a few lines that call `prep.py`,
the rules and the optimizer. The README beside it states what it does in business terms.

To add a param, add a field to `Params`, with a docstring under it. To read a new dataset, declare it in `datasets/` and
add it to `Data`. To add a rule, write it in `rules/` and call it in `prepare_portfolio`, in the order it applies."""

from dataclasses import dataclass

import optim
import optim.cvxpy
import pandas as pd
from optim.schema import Rating, Weight
from pydantic import Field

from optim.example import prep
from optim.example.datasets.controls import CONTROLS
from optim.example.datasets.mandates import MANDATES
from optim.example.datasets.portfolios import PORTFOLIOS
from optim.example.datasets.signals import SIGNALS
from optim.example.optimizer import solve
from optim.example.rules import controls, credit, mandates, screens


class Params(optim.Params):
    max_bond_weight: Weight = 0.05
    """The most of a portfolio's value in one bond after trading, as a fraction: 0.05 is 5%."""
    max_issuer_weight: Weight = 0.1
    """The most of a portfolio's value in one issuer's bonds together after trading."""
    sector_caps: dict[str, Weight] = Field(default_factory=dict)
    """The most of a portfolio's value in each sector named after trading, such as `{"Financials": 0.25}`. A
    portfolio's mandates may tighten them."""
    max_turnover: Weight = 0.25
    """The most a portfolio buys and sells together, as a fraction of its value."""
    cost_bps: float = Field(default=5.0, ge=0)
    """The estimated cost of trading, in basis points of the value traded, which the objective subtracts from income."""
    min_rating: Rating = "A-"
    """Buy only bonds rated this or better, or better still where the portfolio's own floor is higher."""
    exclude_sectors: list[str] = Field(default_factory=list)
    """Never buy these sectors, and hold no bond of them the portfolio does not hold already."""
    exclude_issuers: list[str] = Field(default_factory=list)
    """Never buy these issuers, as a restricted list says."""
    solve: optim.cvxpy.SolveSettings = optim.cvxpy.SolveSettings()


class Data(optim.Datasets):
    portfolios = PORTFOLIOS
    signals = SIGNALS
    controls = CONTROLS
    mandates = MANDATES


class Metrics(optim.Metrics):
    income: float
    """Expected income over a year after trading, in the portfolio's currency."""
    cost: float
    """Estimated trading cost, in the portfolio's currency."""


@dataclass(frozen=True)
class Market:
    """What every portfolio shares."""

    securities: pd.DataFrame
    """Every security, with its `unit_value`, and its `expected_return`, 0 without a signal."""


@dataclass(frozen=True)
class Portfolio:
    """What one portfolio's decision sees: `prep.portfolio` builds it, and each rule returns it changed."""

    data: Data
    """The portfolio's own rows of each dataset, which rules read: `portfolio.data.controls`."""
    universe: pd.DataFrame
    """A row per security it holds, cash aside, or may buy, with `held`, and `buyable`, `sellable` and `must_sell`,
    which rules narrow."""
    sector_caps: dict[str, float]
    """The most of its value in each sector named after trading: the params' caps, tightened by its mandates."""
    cash: float
    value: float
    """Cash and every holding at its unit value, from the holdings, so a row a rule drops still counts."""


def prepare_market(data: Data, params: Params) -> Market:
    return prep.market(data)


def prepare_portfolio(data: Data, market: Market, params: Params) -> Portfolio:
    portfolio = prep.portfolio(data, market, params)
    portfolio = controls.apply(portfolio, market, params)
    portfolio = mandates.sector_caps(portfolio, market, params)
    portfolio = credit.min_rating(portfolio, market, params)
    portfolio = screens.exclude_sectors(portfolio, market, params)
    return screens.exclude_issuers(portfolio, market, params)


def decide(portfolio: Portfolio, market: Market, params: Params) -> optim.Decision:
    return solve.solve(portfolio, params)
