"""Research on saved runs, which reads only snapshots, so an agent can try a hundred variants without touching
production data: `sweep` decides a snapshot once per params file into one comparison table, `regress` replays a
model's recent runs with today's code and lists every trade that changed, `trial` decides one portfolio from Python and
hands back what it saw, and `describe` says what a model reads, takes and reports, as JSON."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import cvxpy as cp
import pandas as pd
from pydantic import JsonValue

import optim
import optim.cvxpy
from optim.contract import LISTING, Decision, Params, declared
from optim.decide import Outcome, State, decide_all, finish, prepare_one, turn_of
from optim.models import HOOKS, LoadedModel
from optim.runs import Snapshot, open_snapshot, read_params
from optim.snapshots import read_info, read_results
from optim.storage import Root


def sweep(snapshot: Snapshot, variants: Sequence[Path], *, workers: int = 1) -> pd.DataFrame:
    """One row per variant and portfolio: whether it was accepted, its objective, turnover, trades, cash after trading,
    time in the solver, the model's metrics, and why it was rejected. Every params file is validated before any
    decides, so a misspelled key in the hundredth fails the sweep at once."""
    params = [(path.stem, read_params(snapshot.model, path)[0]) for path in variants]
    rows = []
    for variant, values in params:
        decided = decide_all(snapshot.model, values, snapshot.data, snapshot.info.portfolios, workers=workers, only=snapshot.portfolios())
        rows += [{"variant": variant, **_row(outcome)} for outcome in decided.outcomes]
    return pd.DataFrame(rows)


def _row(outcome: Outcome) -> dict[str, object]:
    decision, numbers = outcome.decision, outcome.numbers
    solver = decision.solver if decision else None
    return {
        "portfolio_id": outcome.portfolio_id,
        "accepted": outcome.accepted,
        "objective": decision.objective if decision else None,
        "turnover": numbers.get("turnover"),
        "trades": len(decision.buy) + len(decision.sell) if decision else 0,
        "cash": numbers.get("cash"),
        "solve_seconds": solver.seconds if solver else None,
        "seconds": outcome.seconds,
        **(decision.metrics if decision else {}),
        "reasons": "; ".join(outcome.reasons),
    }


def regress(model: LoadedModel, snapshots: Root, *, last: int = 20, labels: dict[str, str] | None = None, workers: int = 1) -> pd.DataFrame:
    """Replay the model's `last` most recent runs under `snapshots`, replays aside, with the code you have now and each
    run's own params, and list every change: a portfolio whose acceptance changed, and every trade whose rounded
    quantity did, with `limit` true where either decision stopped at a time limit, which may stop elsewhere on
    another machine. `labels` keeps only the runs labelled so, such as `purpose=production`."""
    rows: list[dict[str, object]] = []
    for root in _recent(model, snapshots, last=last, labels=labels or {}):
        saved, before = open_snapshot(root), read_results(root)
        status = dict(zip(before["decisions"]["portfolio_id"], before["decisions"]["status"], strict=True))
        stops = dict(zip(before["decisions"]["portfolio_id"], before["decisions"]["stop"], strict=True))
        trades: dict[str, dict[tuple[str, str], float]] = {}
        for trade in before["trades"].itertuples():
            trades.setdefault(str(trade.portfolio_id), {})[str(trade.security_id), str(trade.side)] = float(trade.quantity)  # ty: ignore[invalid-argument-type] -- a row's columns
        for outcome in decide_all(saved.model, saved.params, saved.data, saved.info.portfolios, workers=workers).outcomes:
            was, now = status[outcome.portfolio_id], "accepted" if outcome.accepted else "rejected"
            solver = outcome.decision.solver if outcome.decision else None
            common = {"run_id": saved.info.run_id, "portfolio_id": outcome.portfolio_id, "status_before": was, "status_after": now}
            common["limit"] = stops[outcome.portfolio_id] == "limit" or (solver is not None and solver.stop == "limit")
            changed = _changed_trades(trades.get(outcome.portfolio_id, {}), outcome.decision)
            rows += [common | change for change in changed] if changed else [common] if was != now else []
    return pd.DataFrame(rows, columns=pd.Index(["run_id", "portfolio_id", "status_before", "status_after", "limit", "security_id", "side", "before", "after"]))


def _recent(model: LoadedModel, snapshots: Root, *, last: int, labels: dict[str, str]) -> list[Root]:
    """The model's runs, replays aside, newest first: run ids are UUIDv7s, which sort by when their runs started."""
    found = []
    for root in reversed(snapshots.children()):
        if len(found) == last:
            break
        if not (root / "run.json").exists():  # a snapshot still being written, or not a snapshot
            continue
        info = read_info(root)
        if (model.module == info.model.module or model.name == info.model.name) and info.replay is None and labels.items() <= info.labels.items():
            found.append(root)
    return found


def _changed_trades(was: dict[tuple[str, str], float], decision: Decision | None) -> list[dict[str, object]]:
    """Each trade whose rounded quantity is not what it was; `was` is the saved quantity by security and side, 0 where
    rounding left nothing."""
    now = {(security, side): quantity for side, quantities in (("buy", decision.buy), ("sell", decision.sell)) for security, quantity in quantities.items()} if decision else {}
    was = {key: quantity for key, quantity in was.items() if quantity}
    changed = sorted(key for key in was.keys() | now.keys() if was.get(key) != now.get(key))
    return [{"security_id": security, "side": side, "before": was.get((security, side)), "after": now.get((security, side))} for security, side in changed]


@dataclass(frozen=True)
class Trial:
    """One portfolio of a saved run, decided again from Python: what it saw, the problem it solved, and what it
    decided."""

    portfolio_id: str
    portfolio: object
    """What the model's `prepare_portfolio` returned, or the portfolio's slice of the data."""
    market: object
    """What every portfolio shares: `prepare_market`'s result, after the reservations of the portfolios before it."""
    problem: cp.Problem | None
    """The cvxpy problem `optim.cvxpy` solved, if the model solves one."""
    outcome: Outcome
    """The decision as the model decided it, rounded, and why it may not trade, if it may not."""


def trial(snapshot: Snapshot, portfolio_id: str, params: Params | None = None) -> Trial:
    """Decide one portfolio of a saved run, with the run's params or others, in this process, as a run would: for a
    model that reserves, after the portfolios before it in the model's order."""
    model, values = snapshot.model, params or snapshot.params
    market = model.prepare_market(snapshot.data, values) if model.prepare_market else snapshot.data
    state = State(model.module, values, snapshot.data, market)
    if model.reserve is not None:
        market, prepared = turn_of(model, state, snapshot.info.portfolios, portfolio_id)
    else:
        prepared = prepare_one(state, portfolio_id)
    with optim.cvxpy.captured() as problems:
        outcome = finish(model, state, market, prepared)
    return Trial(portfolio_id, prepared.prepared, market, problems[-1] if problems else None, outcome)


def describe(model: LoadedModel) -> dict[str, JsonValue]:
    """What an agent needs to drive research without reading the model's code: its params' JSON schema, the datasets
    it reads with their columns, the metrics it reports, and which hooks it defines."""
    datasets: dict[str, JsonValue] = {
        attribute: {
            "name": dataset.name,
            "source": dataset.source,
            "per_portfolio": dataset.per_portfolio and attribute != LISTING,  # read in batches of portfolios
            "key": list(dataset.key),
            "cache_seconds": dataset.cache.total_seconds() if dataset.cache else None,
            "columns": dataset.row.model_json_schema()["properties"],
        }
        for attribute, dataset in declared(model.data).items()
    }
    hooks = [hook for hook in HOOKS if getattr(model, hook) is not None]
    return {
        "model": model.name,
        "module": model.module,
        "optim_version": optim.__version__,
        "hooks": list(hooks),
        "params": model.params.model_json_schema(),
        "datasets": datasets,
        "metrics": model.metrics.model_json_schema() if model.metrics else None,
    }
