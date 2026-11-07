"""How `optim.cvxpy` solves a model's problem and reads it back: trades by security, whether it solved, and the solver's
own account of why it stopped."""

import cvxpy as cp
import numpy as np
import pytest

from optim.cvxpy import SolveSettings, read, solve

SECURITIES = ["A", "B"]


def lots(*, most: float = 10.0) -> tuple[cp.Problem, cp.Variable, cp.Variable]:
    """Buy whole lots of 1,000 of A, which returns more than B, or B; sell nothing of B, which is held."""
    buy, sell = cp.Variable(2, integer=True), cp.Variable(2, nonneg=True)
    problem = cp.Problem(cp.Maximize(np.array([0.05, 0.03]) @ buy - 0.01 * cp.sum(sell)), [buy >= 0, cp.sum(buy) <= most, sell <= np.array([0.0, 5.0])])
    return problem, buy, sell


def test_an_integer_problem_solves_to_trades_by_security_and_the_solvers_statistics() -> None:
    problem, buy, sell = lots()

    decision = solve(problem, SolveSettings(), securities=SECURITIES, buy=1000 * buy, sell=sell, metrics={"lots": cp.sum(buy), "fixed": 2.0})

    assert decision.solved
    assert (decision.buy, decision.sell) == ({"A": 10_000.0}, {})  # a sale of zero, or of solver noise, is no trade
    assert decision.objective == pytest.approx(0.5)
    assert decision.metrics == {"lots": pytest.approx(10.0), "fixed": 2.0}
    assert decision.solver is not None
    assert (decision.solver.solver, decision.solver.stop) == ("HIGHS", "optimal")
    assert decision.solver.gap is not None
    assert decision.solver.nodes is not None


def test_a_piecewise_term_over_unbounded_integers_solves_without_a_warning() -> None:
    """cvxpy bounds `cp.pos` by multiplying each coefficient by its variable's bounds, so a 0 coefficient on an
    unbounded integer is 0 times infinity: numpy warns, and cvxpy drops the NaN bound it made. The warning is noise
    that a repo's `filterwarnings = ["error"]` would turn into a rejected portfolio."""
    problem, buy, sell = lots()
    gain = np.array([0.0, 2.0]) @ buy  # a tax on the gain from B alone
    taxed = cp.Problem(cp.Maximize(problem.objective.args[0] - 0.3 * cp.pos(gain)), problem.constraints)

    decision = solve(taxed, SolveSettings(), securities=SECURITIES, buy=1000 * buy, sell=sell)

    assert decision.solved
    assert decision.buy == {"A": 10_000.0}


def test_a_problem_solved_elsewhere_reads_as_one_solve_solved() -> None:
    problem, buy, _ = lots()
    problem.solve(solver=cp.HIGHS)

    decision = read(problem, securities=SECURITIES, buy=1000 * buy)

    assert decision.solved
    assert decision.buy == {"A": 10_000.0}


def test_an_infeasible_problem_has_no_trades_and_is_not_solved() -> None:
    problem, buy, _ = lots(most=-1.0)

    decision = solve(problem, SolveSettings(), securities=SECURITIES, buy=buy)

    assert not decision.solved
    assert decision.buy == {}
    assert decision.solver is not None
    assert decision.solver.stop == "infeasible"


def test_a_problem_with_nothing_to_decide_is_solved_with_no_trades() -> None:
    decision = solve(cp.Problem(cp.Maximize(cp.Constant(0.0))), SolveSettings(), securities=[])

    assert decision.solved
    assert decision.buy == decision.sell == {}


def test_copt_solves_a_quadratic_objective_with_integer_variables() -> None:
    pytest.importorskip("coptpy")
    buy = cp.Variable(2, integer=True)
    problem = cp.Problem(cp.Maximize(np.array([0.06, 0.02]) @ buy - 0.01 * cp.sum_squares(buy)), [buy >= 0, cp.sum(buy) <= 10])

    decision = solve(problem, SolveSettings(solver="COPT"), securities=SECURITIES, buy=buy)

    assert decision.solved
    assert decision.solver is not None
    assert (decision.solver.solver, decision.solver.stop) == ("COPT", "optimal")
    assert decision.solver.gap is not None
    assert decision.buy == {"A": pytest.approx(3.0), "B": pytest.approx(1.0)}  # near-integers, as solvers return them
