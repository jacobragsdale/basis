"""Deciding portfolios with a model's hooks: `prepare_market` once, then for each portfolio `prepare_portfolio` and
`decide`, with the decision rounded and reviewed (`optim.acceptance`) and the model's `check` adding its own reasons.
Pure computation, with no I/O, so it runs in worker processes; `optim.runs` loads the data and saves what it returns.

A hook's error rejects its portfolio alone. Its reason names the error and the last line of the model's code it passed
through, such as `KeyError: 'sector', at models/income/rules/screens.py:14 in exclude_sectors`, and the log holds its
whole traceback."""

import logging
import time
import traceback
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import numpy as np
from numpy.typing import ArrayLike
from pydantic import Field

from optim import engine, logs, models
from optim.acceptance import review, round_decision
from optim.contract import LISTING, Datasets, Decision, Params, declared
from optim.models import LoadedModel
from optim.schema import Model

log = logging.getLogger(__name__)


class Outcome(Model):
    """One portfolio's result: its decision, rounded, and why it may not trade, if it may not."""

    portfolio_id: str
    decision: Decision | None
    """Rounded to each security's increment: what may trade, if accepted. None when the portfolio failed before it was
    decided."""
    decided: Decision | None
    """As the model decided it, before rounding."""
    reasons: list[str]
    """Why it may not trade; empty when accepted."""
    numbers: dict[str, float] = Field(default_factory=dict)
    """The portfolio's `value` before trading, `cash` after, and `turnover`, from the rounded trades."""
    seconds: float
    """To prepare, decide and check the portfolio."""

    @property
    def accepted(self) -> bool:
        return not self.reasons


@dataclass(frozen=True)
class Decided:
    outcomes: list[Outcome]
    """In the run's portfolio order."""
    order: list[str] | None
    """The order the portfolios decided in, one at a time, for a model that reserves; None when they decided at once."""


def decide_all(model: LoadedModel, params: Params, data: Datasets, portfolio_ids: Sequence[str], *, workers: int, only: Sequence[str] | None = None) -> Decided:
    """Run `prepare_market` once here, then prepare, decide and check each portfolio in up to `workers` processes. A
    model that defines `reserve` prepares its portfolios in parallel, then decides them one at a time, here, in its
    `order`. `only` decides and returns those portfolios alone; for a model that reserves, the portfolios before them in
    its order decide too, since what those reserve is what the later ones see."""
    logs.include(model.module.partition(".")[0])
    with logs.timed(log, "prepared the market"):
        market = model.prepare_market(data, params) if model.prepare_market else data
    state = State(model.module, params, data, market)
    with logs.timed(log, "decided portfolios", portfolios=len(portfolio_ids), workers=workers) as fields:
        if model.reserve is None:
            chosen = [portfolio_id for portfolio_id in portfolio_ids if only is None or portfolio_id in only]
            decided = Decided(engine.each_portfolio(_decide, state, chosen, workers=workers, failed=_failed), None)
        else:
            decided = in_turn(model, state, portfolio_ids, workers=workers, only=only)
        rejected = [outcome.portfolio_id for outcome in decided.outcomes if not outcome.accepted]
        fields.update(rejected=len(rejected), rejected_examples=rejected[:20], in_turn=decided.order is not None)
    return decided


def in_turn(model: LoadedModel, state: State, portfolio_ids: Sequence[str], *, workers: int, only: Sequence[str] | None) -> Decided:
    """Decide one portfolio at a time, in the model's order, each seeing the market the earlier ones left: only an
    accepted decision reserves, so a rejected portfolio leaves what it would have bought to the next."""
    prepared, order = _in_order(model, state, portfolio_ids, workers=workers)
    last = max((order.index(portfolio_id) for portfolio_id in only if portfolio_id in order), default=-1) if only is not None else len(order) - 1
    outcomes, _ = _decide_in_turn(model, state, prepared, order[: last + 1])
    failed = {portfolio_id: _failed(portfolio_id, result.error) for portfolio_id, result in prepared.items() if result.error is not None}
    reported = [portfolio_id for portfolio_id in portfolio_ids if only is None or portfolio_id in only]
    return Decided([(outcomes | failed)[portfolio_id] for portfolio_id in reported], order)


def turn_of(model: LoadedModel, state: State, portfolio_ids: Sequence[str], portfolio_id: str) -> tuple[object, Prepared]:
    """For a model that reserves, the market `portfolio_id` decides with, which the portfolios before it in the model's
    order left, and its preparation."""
    prepared, order = _in_order(model, state, portfolio_ids, workers=1)
    _, market = _decide_in_turn(model, state, prepared, order[: order.index(portfolio_id)])
    return market, prepared[portfolio_id]


def _in_order(model: LoadedModel, state: State, portfolio_ids: Sequence[str], *, workers: int) -> tuple[dict[str, Prepared], list[str]]:
    """Every portfolio prepared, in parallel, and the order the prepared ones decide in."""
    prepared = {result.portfolio_id: result for result in engine.each_portfolio(prepare_one, state, portfolio_ids, workers=workers, failed=_unprepared)}
    ready = {portfolio_id: result.prepared for portfolio_id, result in prepared.items() if result.error is None}
    order = list(model.order(ready, state.market, state.params)) if model.order else list(ready)
    if sorted(order) != sorted(ready):
        msg = f"{model.module}.order must return every prepared portfolio once, in the order they decide: got {logs.brief({'order': order})['order']} for {len(ready)} portfolios"
        raise ValueError(msg)
    return prepared, order


def _decide_in_turn(model: LoadedModel, state: State, prepared: Mapping[str, Prepared], order: Sequence[str]) -> tuple[dict[str, Outcome], object]:
    """Each portfolio in `order`, and the market the last one left."""
    reserve, market = model.reserve, state.market
    outcomes: dict[str, Outcome] = {}
    for portfolio_id in order:
        outcome = outcomes[portfolio_id] = finish(model, state, market, prepared[portfolio_id])
        if outcome.accepted and outcome.decision is not None and reserve is not None:
            market = reserve(market, outcome.decision)
    return outcomes, market


@dataclass
class State:
    """What every portfolio's decision shares, shipped once to each worker process."""

    module: str
    params: Params
    data: Datasets
    market: object

    @cached_property
    def _rows(self) -> dict[str, Mapping[Hashable, ArrayLike]]:
        """Each per-portfolio dataset's row positions, by portfolio id: found once per process, not once per portfolio."""
        per_portfolio = [dataset.name for dataset in declared(type(self.data)).values() if dataset.per_portfolio]
        return {name: self.data.frames[name].groupby("portfolio_id").indices for name in per_portfolio}

    def portfolio(self, portfolio_id: str) -> Datasets:
        """The portfolio's slice of the data: its rows of each per-portfolio dataset, and every other dataset whole. Each
        frame is a shallow copy, so a model that adds a column to one changes no other portfolio's."""
        frames = {name: frame.copy(deep=False) for name, frame in self.data.frames.items()}
        for name, rows in self._rows.items():
            frames[name] = frames[name].take(np.asarray(rows.get(portfolio_id, ()), dtype=np.intp)).reset_index(drop=True)
        return type(self.data)(frames, as_of_date=self.data.as_of_date, portfolio_id=portfolio_id)


@dataclass(frozen=True)
class Prepared:
    portfolio_id: str
    prepared: object
    """The portfolio: what `prepare_portfolio` returned, or the portfolio's slice of the data without it."""
    error: str | None
    seconds: float


def prepare_one(state: State, portfolio_id: str) -> Prepared:
    """One portfolio's `prepare_portfolio`, in whichever process runs it. A failure is logged here, where its traceback
    is whole. A model that lists its portfolios rejects one it does not list."""
    start, model = time.perf_counter(), models.load(state.module)
    with logs.fields(portfolio_id=portfolio_id):
        try:
            data = state.portfolio(portfolio_id)
            if LISTING in declared(model.data) and getattr(data, LISTING).empty:
                return Prepared(portfolio_id, None, f"not one of the portfolios the model lists on {data.as_of_date}", time.perf_counter() - start)
            prepared = model.prepare_portfolio(data, state.market, state.params) if model.prepare_portfolio else data
        except Exception as error:
            log.exception("could not prepare %s", portfolio_id)
            return Prepared(portfolio_id, None, _reason(error, model), time.perf_counter() - start)
    return Prepared(portfolio_id, prepared, None, time.perf_counter() - start)


def _decide(state: State, portfolio_id: str) -> Outcome:
    """One portfolio, prepared, decided and checked, in whichever process runs it."""
    prepared = prepare_one(state, portfolio_id)
    if prepared.error is not None:
        return _failed(portfolio_id, prepared.error, prepared.seconds)
    return finish(models.load(state.module), state, state.market, prepared)


def finish(model: LoadedModel, state: State, market: object, prepared: Prepared) -> Outcome:
    """Decide a prepared portfolio with `market`, round the decision, and review it. A failure is logged here, where
    its traceback is whole, and rejects this portfolio alone."""
    start, portfolio_id = time.perf_counter() - prepared.seconds, prepared.portfolio_id
    with logs.fields(portfolio_id=portfolio_id):
        try:
            data = state.portfolio(portfolio_id)
            decided = _decision(model, model.decide(prepared.prepared, market, state.params))
            decision = round_decision(decided, data.holdings, data.securities)
            reasons, numbers = review(decision, decided, data.holdings, data.securities)
            reasons += list(model.check(prepared.prepared, decision)) if model.check else []
        except Exception as error:
            log.exception("could not decide %s", portfolio_id)
            return _failed(portfolio_id, _reason(error, model), time.perf_counter() - start)
        outcome = Outcome(portfolio_id=portfolio_id, decision=decision, decided=decided, reasons=reasons, numbers=numbers, seconds=time.perf_counter() - start)
        log.info(
            "decided %s: %s",
            portfolio_id,
            "accepted" if outcome.accepted else "rejected",
            extra={"reasons": reasons[:20], "buys": len(decision.buy), "sells": len(decision.sell), "solved": decision.solved, "solver": decision.solver, "seconds": outcome.seconds},
        )
        return outcome


def _decision(model: LoadedModel, decided: object) -> Decision:
    if not isinstance(decided, Decision):
        msg = f"{model.module}.decide returned {type(decided).__name__}, not an optim.Decision"
        raise TypeError(msg)
    return decided


def _reason(error: Exception, model: LoadedModel) -> str:
    """A hook's error as its portfolio's reason: the error, and the last line of the model's own code it passed through.
    The model's code is what sits beside its folder, as `models/shared/` sits beside `models/income/`."""
    code = model.directory.parent
    frames = [frame for frame in traceback.extract_tb(error.__traceback__) if Path(frame.filename).is_relative_to(code)]
    where = f", at {Path(frames[-1].filename).relative_to(code.parent).as_posix()}:{frames[-1].lineno} in {frames[-1].name}" if frames else ""
    return f"{type(error).__name__}: {error}{where}"


def _failed(portfolio_id: str, why: str, seconds: float = 0.0) -> Outcome:
    return Outcome(portfolio_id=portfolio_id, decision=None, decided=None, reasons=[why], seconds=seconds)


def _unprepared(portfolio_id: str, why: str) -> Prepared:
    return Prepared(portfolio_id, None, why, 0.0)
