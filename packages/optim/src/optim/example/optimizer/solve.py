"""Solving one portfolio: build its problem, and solve it with the params' solve settings, through `optim.cvxpy`, which
reads the answer as a decision."""

import optim
import optim.cvxpy

from optim.example import model
from optim.example.optimizer import problem


def solve(portfolio: model.Portfolio, params: model.Params) -> optim.Decision:
    """The portfolio's decision, with each objective term reported as a metric. A portfolio with nothing it may hold or
    buy has nothing to decide."""
    universe = portfolio.universe
    if universe.empty:
        return optim.Decision()
    built = problem.build(portfolio, params)
    return optim.cvxpy.solve(built.problem, params.solve, securities=universe["security_id"].tolist(), buy=built.buy, sell=built.sell, metrics=built.terms)
