"""A toy model for the core's tests of a model that lists its portfolios, in `listed.csv` beside it: it buys 1,000 of
A in each portfolio it decides."""

from pathlib import Path

import optim
from optim.schema import Id


class Listed(optim.Row):
    portfolio_id: Id


class Params(optim.Params):
    pass


class Data(optim.Datasets):
    portfolios = optim.Dataset("portfolios", Listed, source="file", query=Path(__file__).with_name("listed.csv"), key=("portfolio_id",))


def decide(portfolio: Data, market: Data, params: Params) -> optim.Decision:  # noqa: ARG001
    return optim.Decision(buy={"A": 1000.0})
