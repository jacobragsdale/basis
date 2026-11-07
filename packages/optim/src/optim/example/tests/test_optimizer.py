"""The optimizer, on a few bonds: what each constraint holds a solve to."""

from dataclasses import replace

import optim
import pytest

from optim.example import model
from optim.example.optimizer import solve
from optim.example.tests.builders import params, portfolio

ROOM = {"max_bond_weight": 1.0, "max_issuer_weight": 1.0, "max_turnover": 1.0}
"""Room to buy anything: no limit of the params binds unless a test sets it."""


def decide(found: model.Portfolio, **limits: float) -> optim.Decision:
    return solve.solve(found, params(**ROOM | limits))


def test_an_issuer_cap_caps_each_issuer() -> None:
    """Each bond is worth 1 a unit, and the portfolio 1,000,000."""
    found = portfolio({"security_id": "A", "issuer": "X"}, {"security_id": "B", "issuer": "X"}, {"security_id": "C", "issuer": "Y"})

    decision = decide(found, max_issuer_weight=0.1)

    assert decision.solved
    assert decision.buy["A"] + decision.buy.get("B", 0.0) == pytest.approx(100_000.0)
    assert decision.buy["C"] == pytest.approx(100_000.0)


def test_a_sector_cap_caps_that_sectors_bonds_together() -> None:
    found = portfolio({"security_id": "A", "sector": "Financials"}, {"security_id": "B", "sector": "Financials"}, {"security_id": "C", "sector": "Utilities"})

    decision = decide(replace(found, sector_caps={"Financials": 0.04}), max_bond_weight=0.3)

    assert decision.buy.get("A", 0.0) + decision.buy.get("B", 0.0) == pytest.approx(40_000.0)
    assert decision.buy["C"] == pytest.approx(300_000.0)


def test_turnover_caps_what_is_bought_and_sold_together() -> None:
    found = portfolio({"security_id": "A"}, {"security_id": "B"})

    decision = decide(found, max_turnover=0.25)

    assert sum(decision.buy.values()) == pytest.approx(250_000.0)


def test_a_cap_leaves_the_portfolio_a_holding_it_may_not_sell() -> None:
    """A is 20% of the portfolio, over its cap of 10%, but may not be sold: it stays, and nothing more is bought."""
    found = portfolio({"security_id": "A", "held": 250_000.0}, cash=1_000_000.0)
    found = replace(found, universe=found.universe.assign(sellable=False))

    decision = decide(found, max_bond_weight=0.1)

    assert decision.solved
    assert decision.buy == {}
    assert decision.sell == {}
