"""A toy model whose decision kills its process for the portfolios its params name, as the kernel kills a worker that
runs out of memory; it buys 1,000 of A in every other portfolio."""

import os

from pydantic import Field

import optim


class Params(optim.Params):
    kill: list[str] = Field(default_factory=list)
    """Portfolios whose decision kills the process deciding it."""


class Data(optim.Datasets):
    pass


def decide(portfolio: Data, market: Data, params: Params) -> optim.Decision:  # noqa: ARG001
    if portfolio.portfolio_id in params.kill:
        os._exit(1)
    return optim.Decision(buy={"A": 1000.0})
