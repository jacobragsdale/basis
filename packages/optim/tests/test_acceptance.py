"""The core's rounding and acceptance, which every model's decisions pass through: trades made exact, and rejected
when they could not trade, whatever the solver said."""

import pytest

import optim
from optim.acceptance import review, round_decision
from optim.contract import SolverStats
from optim.datasets import HOLDINGS, SECURITIES
from optim.testing import CASH, frame

SECURITIES_ = frame(SECURITIES, CASH, {"security_id": "A"}, {"security_id": "B", "price": 50.0})
HOLDINGS_ = frame(HOLDINGS, {"quantity": 1_000_000.0}, {"security_id": "B", "quantity": 100_500.0})  # $1mm cash and $50,250 of B


@pytest.mark.parametrize(
    ("decision", "rounded"),
    [
        (optim.Decision(buy={"A": 2999.9999999}), optim.Decision(buy={"A": 3000.0})),  # a solver's near-integer lots
        (optim.Decision(buy={"A": 1499.0}), optim.Decision(buy={"A": 1000.0})),
        (optim.Decision(buy={"A": 400.0}), optim.Decision()),  # rounds to nothing: no trade
        (optim.Decision(sell={"B": 100_499.9}), optim.Decision(sell={"B": 100_500.0})),  # an odd lot sells whole
        (optim.Decision(sell={"B": 100_000.0}), optim.Decision(sell={"B": 100_000.0})),  # half an increment short: a partial sale
        (optim.Decision(buy={"X": 1.5}), optim.Decision(buy={"X": 1.5})),  # unknown: left for review to reject
    ],
    ids=["near-integer", "nearest", "nothing", "whole-holding", "partial", "unknown"],
)
def test_rounding_makes_each_quantity_whole_increments(decision: optim.Decision, rounded: optim.Decision) -> None:
    assert round_decision(decision, HOLDINGS_, SECURITIES_) == rounded


LIMIT = SolverStats(solver="HIGHS", status="user_limit", stop="limit", gap=0.05)


@pytest.mark.parametrize(
    ("decision", "reason"),
    [
        (optim.Decision(buy={"A": 990_000.0}), None),  # $990k of A from $1mm cash
        (optim.Decision(buy={"A": 1_000_001.0}), None),  # $1 short is within $1.05, the one tolerance in weights of value
        (optim.Decision(buy={"A": 1_000_002.0}), "leaves cash of -2.00"),
        (optim.Decision(sell={"B": 100_503.0}), "more than its holding of 100,500.00"),  # $1.50 over
        (optim.Decision(sell={"A": 1000.0}), "more than its holding of 0.00"),
        (optim.Decision(buy={"X": 1000.0}), "trades securities that are not loaded: ['X']"),
        (optim.Decision(buy={"A": -1000.0}), "buys quantities that are not positive: ['A']"),
        (optim.Decision(buy={"A": 1000.0}, solved=False, solver=LIMIT), "not solved: limit"),  # its trades are fine; its solve is not
    ],
    ids=["fine", "cash-within-tolerance", "cash", "oversold", "sells-what-it-lacks", "unknown", "negative", "not-solved"],
)
def test_review_rejects_what_could_not_trade(decision: optim.Decision, reason: str | None) -> None:
    reasons, _ = review(decision, decision, HOLDINGS_, SECURITIES_)

    if reason is None:
        assert reasons == []
    else:
        assert any(reason in found for found in reasons), reasons


def test_review_rebuilds_the_portfolio_after_the_trades_from_the_holdings_and_prices_alone() -> None:
    decision = optim.Decision(buy={"A": 10_000.0}, sell={"B": 500.0})

    _, numbers = review(decision, decision, HOLDINGS_, SECURITIES_)

    assert numbers == {"value": 1_050_250.0, "cash": 1_000_000.0 + 250.0 - 10_000.0, "turnover": 10_250.0}


def test_a_portfolio_with_no_holdings_is_rejected_whatever_it_decides() -> None:
    reasons, _ = review(optim.Decision(), optim.Decision(), frame(HOLDINGS), SECURITIES_)

    assert reasons == ["it has no holdings: is its id right?"]


@pytest.mark.parametrize(
    ("decided", "reason"),
    [
        (optim.Decision(buy={"A": 2999.9999999}), None),  # a solver's near-integer lots: rounding moves it $0.0000001
        (optim.Decision(buy={"A": 1499.0}), "rounding moved its buy of A from 1,499.00 to 1,000.00"),
        (optim.Decision(buy={"A": 400.0}), "rounding moved its buy of A from 400.00 to 0.00"),
        (optim.Decision(sell={"B": 100_499.9}), None),  # an odd lot sells whole: $0.05 is within $1.05
    ],
    ids=["near-integer", "nearest", "to-nothing", "whole-holding"],
)
def test_review_rejects_a_decision_that_rounding_moves(decided: optim.Decision, reason: str | None) -> None:
    """The solver proved the decision meets its constraints before rounding; a decision rounding moves might not."""
    reasons, _ = review(round_decision(decided, HOLDINGS_, SECURITIES_), decided, HOLDINGS_, SECURITIES_)

    assert reasons == ([] if reason is None else [f"{reason}: decide whole increments"])
