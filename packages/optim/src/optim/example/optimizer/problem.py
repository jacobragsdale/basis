"""The cvxpy problem one portfolio solves: a `buy` and a `sell` variable per row of the universe, the objective, and
every constraint in `constraints.py`. To add a constraint, write it there and add it to `build`; to add a term to the
objective, add it to `terms` and to the objective in `build`."""

from dataclasses import dataclass

import cvxpy as cp

from optim.example import model
from optim.example.optimizer import constraints


@dataclass(frozen=True)
class Built:
    """An unsolved problem, and the expressions that read its answer."""

    problem: cp.Problem
    buy: cp.Variable
    """Quantity bought of each row of the universe, in the holdings' units."""
    sell: cp.Variable
    """Quantity sold of each row."""
    terms: dict[str, cp.Expression]
    """Each objective term's amount, in the portfolio's currency, by the term's name."""


def build(portfolio: model.Portfolio, params: model.Params) -> Built:
    """The portfolio's problem, for a universe of at least one row: it maximizes income less cost, as a fraction of the
    portfolio's value."""
    universe, value = portfolio.universe, portfolio.value
    buy, sell = cp.Variable(len(universe), nonneg=True), cp.Variable(len(universe), nonneg=True)
    found = terms(portfolio, buy, sell, params)
    limits = [
        *constraints.sizes(universe, buy, sell, value=value),
        *constraints.cash(portfolio, buy, sell),
        *constraints.turnover(universe, buy, sell, params.max_turnover, value=value),
        *constraints.max_weight(universe, buy, sell, "security_id", params.max_bond_weight, value=value),
        *constraints.max_weight(universe, buy, sell, "issuer", params.max_issuer_weight, value=value),
        *constraints.max_weight(universe, buy, sell, "sector", portfolio.sector_caps, value=value),
    ]
    return Built(cp.Problem(cp.Maximize((found["income"] - found["cost"]) / value), limits), buy, sell, found)


def terms(portfolio: model.Portfolio, buy: cp.Variable, sell: cp.Variable, params: model.Params) -> dict[str, cp.Expression]:
    """Each term of the objective, in the portfolio's currency: `income`, expected over a year after trading, and
    `cost`, the estimated cost of trading, `params.cost_bps` of the value traded. The decision reports each as a
    metric."""
    universe = portfolio.universe
    unit, held = (universe[column].to_numpy(dtype=float) for column in ("unit_value", "held"))
    return {"income": (universe["expected_return"].to_numpy(dtype=float) * unit) @ (held + buy - sell), "cost": (unit * params.cost_bps / 1e4) @ (buy + sell)}
