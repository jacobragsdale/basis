"""Solving a cvxpy problem the same way in every model, and reading its answer as a `Decision`. A model builds its problem,
then calls `solve` with the solve settings from its params; a problem the model solved itself goes to `read`. The core
never solves a model's problem itself.

`solve` passes the solver its time limit and gap under the solver's own option names, and `warm_start=False`, since
cvxpy's default warm start made HiGHS re-solves about six times slower. `read` reads the solver's own status, because
cvxpy's loses what acceptance needs: it reports COPT stopped by its time limit as `optimal_inaccurate` with a solution
and `infeasible_inaccurate` without one. A decision is solved only when the solver proved its answer optimal within the
gap asked, and the answer meets every constraint of the problem at the values it returned, within a millionth of the
constraint's size: a solver proves its answer for the problem as it read it, and HiGHS drops a matrix entry at or below
1e-9, such as a unit's weight in a portfolio of a billion, so a constraint written in weights can vanish. One that
stopped at its time limit, or breaks a constraint, keeps its trades, for the snapshot, and is rejected.

To read another solver's status exactly, add a reader to `_READERS`; any other solver is read from cvxpy's status.

HiGHS 1.16 prints its banner on stdout, from C, as a process makes its first solver, ahead of the JSON a caller reads
there; importing this module makes that first solver with stdout pointed at stderr."""

import logging
import math
import os
import sys
from collections import Counter
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

import cvxpy as cp
import highspy
import numpy as np
from cvxpy.atoms.affine.add_expr import AddExpression
from cvxpy.atoms.affine.unary_operators import NegExpression
from pydantic import Field, field_validator

from optim.contract import Decision, Params, SolverStats, Stop

log = logging.getLogger(__name__)
_FEASIBLE = 1e-6  # a constraint holds at the solution within this much of its size, or of 1 where it is smaller
_NOISE = 1e-6  # a trade smaller than this, in the holdings' units, is solver noise: no trade
_problems: ContextVar[list[cp.Problem] | None] = ContextVar("_problems", default=None)  # see `captured`


def _banner_to_stderr() -> None:
    """Make the process's first HiGHS solver, whose banner HiGHS prints once, with file descriptor 1 on stderr's."""
    sys.stdout.flush()
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        highspy.Highs()
    finally:
        os.dup2(saved, 1)
        os.close(saved)


_banner_to_stderr()


class SolveSettings(Params):
    """How to solve: which solver, for how long, and how close to optimal an integer answer must be."""

    solver: Literal["HIGHS", "COPT"] = "HIGHS"
    """`HIGHS`, free, solves LPs, MILPs and QPs without integers. `COPT`, licensed (the `copt` extra), also solves
    quadratic objectives with integer variables; without a license it runs size-limited."""
    time_limit_s: float = Field(default=30.0, gt=0)
    """A solve that reaches it is not solved, and its portfolio is rejected."""
    mip_gap: float = Field(default=0.001, ge=0)
    """An integer answer within this relative gap of the best possible counts as optimal, and the solver stops there."""
    options: dict[str, float | int | bool | str] = Field(default_factory=dict)
    """Passed to the solver as its docs name them, such as HiGHS's `mip_feasibility_tolerance`. They override the
    core's defaults, such as COPT's one thread, but never the time limit or the gap, which decide what counts as
    optimal: an option that sets either is refused."""

    @field_validator("options")
    @classmethod
    def _not_owned(cls, options: dict[str, float | int | bool | str]) -> dict[str, float | int | bool | str]:
        if owned := sorted(set(options) & _OWNED):
            msg = f"options {owned} set what time_limit_s and mip_gap set, and a gap set there would count a poorer answer as optimal: set time_limit_s and mip_gap instead"
            raise ValueError(msg)
        return options


_OWNED = frozenset({"time_limit", "mip_rel_gap", "mip_abs_gap", "TimeLimit", "RelGap", "AbsGap"})
"""The solvers' own names for the time limit and the gap, which `SolveSettings`' fields set."""


def solve(
    problem: cp.Problem,
    settings: SolveSettings,
    *,
    securities: Sequence[str],
    buy: cp.Expression | None = None,
    sell: cp.Expression | None = None,
    metrics: Mapping[str, cp.Expression | float] | None = None,
) -> Decision:
    """Solve `problem` with `settings`, then `read` it. `buy` and `sell` are quantities, in the holdings' units, one
    entry per security in `securities`; a model with one signed trade per security passes `cp.pos(trade)` and
    `cp.neg(trade)`. Each security is named once: two entries for one would be one order, and one of them lost."""
    _once(securities)
    options = {"time_limit": settings.time_limit_s, "mip_rel_gap": settings.mip_gap} if settings.solver == "HIGHS" else {"TimeLimit": settings.time_limit_s, "RelGap": settings.mip_gap, "Threads": 1}
    if problem.size_metrics.num_scalar_variables == 0:  # nothing to decide, as in a portfolio that may not trade: solvers refuse an empty model
        return Decision(objective=float(problem.objective.value), solver=SolverStats(solver=settings.solver, status="no variables", stop="optimal"))
    # cvxpy bounds a piecewise term, such as `cp.pos`, from its variables' bounds, and 0 times an unbounded variable's
    # infinite bound is NaN, a bound cvxpy then drops: numpy's warning about it is noise, which a model repo's
    # `filterwarnings = ["error"]` would turn into a rejected portfolio in its tests alone.
    with np.errstate(invalid="ignore"):
        problem.solve(solver=settings.solver, warm_start=False, **options | settings.options)
    return read(problem, securities=securities, buy=buy, sell=sell, metrics=metrics)


def read(
    problem: cp.Problem, *, securities: Sequence[str], buy: cp.Expression | None = None, sell: cp.Expression | None = None, metrics: Mapping[str, cp.Expression | float] | None = None
) -> Decision:
    """A solved problem as a `Decision`: its trades, whether it solved, its objective, the solver's statistics, and the
    `metrics` evaluated at the solution. Without a solution, it has no trades."""
    _once(securities)
    if (problems := _problems.get()) is not None:
        problems.append(problem)
    stats = problem.solver_stats
    stop, has_solution, gap, nodes = _READERS.get(stats.solver_name, _from_cvxpy)(problem)
    solver = SolverStats(solver=stats.solver_name, status=str(problem.status), stop=stop, gap=gap, nodes=nodes, iterations=stats.num_iters, seconds=stats.solve_time)
    if not has_solution:
        return Decision(solved=False, solver=solver)
    if (broken := _broken(problem)) is not None:
        log.warning("the solver's answer breaks a constraint of the problem: write it in larger units, such as currency rather than weights", extra={"constraint": broken})
        stop, solver = "violated", solver.model_copy(update={"stop": "violated"})
    objective = float(problem.value) if problem.value is not None and math.isfinite(problem.value) else None
    return Decision(
        buy=_quantities(buy, securities),
        sell=_quantities(sell, securities),
        solved=stop == "optimal",
        objective=objective,
        solver=solver,
        metrics={name: float(np.asarray(value.value if isinstance(value, cp.Expression) else value)) for name, value in (metrics or {}).items()},
    )


@contextmanager
def captured() -> Generator[list[cp.Problem]]:
    """The problems `read` reads in the block, solved: how deciding one portfolio from Python hands back its problem."""
    problems: list[cp.Problem] = []
    token = _problems.set(problems)
    try:
        yield problems
    finally:
        _problems.reset(token)


def _broken(problem: cp.Problem) -> str | None:
    """The first constraint the problem's values break by more than `_FEASIBLE` of its size, in a few words; None where
    every one holds. A side's size is that of the terms it adds up, so a sum that nets to little, as cash after trading
    may, is held to the tolerance of what it sums."""
    for index, constraint in enumerate(problem.constraints):
        violation = np.asarray(constraint.violation(), dtype=float)
        size = np.zeros(violation.shape)
        for side in constraint.args:
            size = np.maximum(size, _size(side))
        if np.any(over := violation > _FEASIBLE * np.maximum(size, 1.0)):
            return f"constraint {index}, {str(constraint)[:120]}, by {float(np.max(np.where(over, violation, 0.0))):.3g}"
    return None


def _size(expression: cp.Expression) -> np.ndarray:
    """The magnitude of the terms `expression` adds up, at the problem's values, elementwise or one for every element."""
    if isinstance(expression, AddExpression):
        return sum((_size(term) for term in expression.args), start=np.zeros(()))
    if isinstance(expression, NegExpression):
        return _size(expression.args[0])
    return np.abs(np.asarray(expression.value, dtype=float))


def _once(securities: Sequence[str]) -> None:
    """Refuse a security named twice, whose trades a decision, one quantity per security, would drop one of."""
    if repeated := sorted({security for security, count in Counter(securities).items() if count > 1}):
        msg = f"securities names {repeated[:5]} more than once: give one entry per security, with its buy and sell, as the universe should hold one row per security"
        raise ValueError(msg)


def _quantities(expression: cp.Expression | None, securities: Sequence[str]) -> dict[str, float]:
    if expression is None:
        return {}
    values = np.asarray(expression.value, dtype=float).ravel()
    return {security: float(value) for security, value in zip(securities, values, strict=True) if abs(value) > _NOISE}


type _Reading = tuple[Stop, bool, float | None, int | None]  # stop, has a solution, gap, nodes

_CVXPY_STOPS: Mapping[str, Stop] = {cp.OPTIMAL: "optimal", cp.USER_LIMIT: "limit", cp.OPTIMAL_INACCURATE: "imprecise", cp.INFEASIBLE: "infeasible", cp.UNBOUNDED: "unbounded"}


def _from_cvxpy(problem: cp.Problem) -> _Reading:
    stop = _CVXPY_STOPS.get(str(problem.status), "failed")
    return stop, stop in ("optimal", "imprecise") or (stop == "limit" and problem.value is not None), None, None


def _highs(problem: cp.Problem) -> _Reading:
    info = problem.solver_stats.extra_stats
    if not isinstance(info, highspy.HighsInfo):
        msg = f"expected HiGHS's solver stats, got {type(info).__name__}"
        raise TypeError(msg)
    stop = _CVXPY_STOPS.get(str(problem.status), "failed")  # cvxpy reads HiGHS's model status faithfully
    has_solution = stop in ("optimal", "imprecise") or (stop == "limit" and info.primal_solution_status == highspy.SolutionStatus.kSolutionStatusFeasible.value)
    integer = problem.is_mixed_integer()
    gap = info.mip_gap if integer and has_solution and math.isfinite(info.mip_gap) else None
    return stop, has_solution, gap, info.mip_node_count if integer else None


_COPT_STOPS: Mapping[int, Stop] = {1: "optimal", 2: "infeasible", 3: "unbounded", 4: "infeasible", 6: "limit", 7: "imprecise", 8: "limit", 10: "limit", 11: "limit"}  # its reference manual's codes


def _copt(problem: cp.Problem) -> _Reading:
    import coptpy  # noqa: PLC0415 -- an optional dependency: only a COPT solve needs it, and cvxpy has imported it by now

    if not isinstance(model := problem.solver_stats.extra_stats, coptpy.Model):
        msg = f"expected COPT's model as its solver stats, got {type(model).__name__}"
        raise TypeError(msg)
    stop = _COPT_STOPS.get(int(model.status), "failed")
    integer = problem.is_mixed_integer()
    has_solution = bool(model.hasmipsol) if integer else stop in ("optimal", "imprecise") and bool(model.haslpsol)
    return stop, has_solution, float(model.getAttr("BestGap")) if integer and has_solution else None, int(model.getAttr("NodeCnt")) if integer else None


_READERS: Mapping[str, Callable[[cp.Problem], _Reading]] = {"HIGHS": _highs, "COPT": _copt}
