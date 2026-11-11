"""A toy model whose hooks edit the data they are given in place: `prepare_market` doubles every holding, and `decide`
zeroes the market's, then sells 10,000 of A, twice what the core's tests hold. The run's own frames, which acceptance
reads and the snapshot saves, must not see either edit."""

import optim


class Params(optim.Params):
    pass


class Data(optim.Datasets):
    pass


def prepare_market(data: Data, params: Params) -> Data:  # noqa: ARG001
    data.holdings["quantity"] = data.holdings["quantity"] * 2
    return data


def decide(portfolio: Data, market: Data, params: Params) -> optim.Decision:  # noqa: ARG001
    market.holdings["quantity"] = 0.0
    return optim.Decision(sell={"A": 10_000.0})
