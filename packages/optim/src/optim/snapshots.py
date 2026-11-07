"""A snapshot of every run: what it was asked, the datasets it loaded and every decision, so any decision can be
explained and replayed. One directory per run under the snapshot root, holding `run.json`, a Parquet file per result
table, and `inputs/`, a Parquet file per dataset exactly as loaded. What a model derived from those is not saved:
replay derives it again with the same code. `RunInfo` is `run.json`; the result tables' row models are below."""

import logging
import subprocess
import uuid
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Literal, Self

import pandas as pd
from pydantic import AwareDatetime, JsonValue
from pydantic_settings import BaseSettings, SettingsConfigDict

from optim.data.dataset import Dataset
from optim.data.frames import conform, to_columns
from optim.data.loader import DatasetRead
from optim.decide import Outcome
from optim.schema import Id, Model, Row
from optim.storage import Root

log = logging.getLogger(__name__)


class _Built(BaseSettings):
    """What an image says about itself: the commit, baked in at build (`--build-arg GIT_SHA`), and the image, which the
    scheduler sets."""

    model_config = SettingsConfigDict(env_prefix="OPTIM_", extra="ignore", frozen=True)

    git_sha: str | None = None
    image: str | None = None


class Code(Model):
    """The model's code a run ran, as `run.json` records it."""

    git_sha: str | None
    """The commit: `OPTIM_GIT_SHA`, baked into the image, or else the HEAD of the checkout the model's module is in.
    Null when neither is known."""
    image: str | None
    """The image, from `OPTIM_IMAGE`. Null outside an image."""
    dirty: bool | None
    """Whether the checkout had changes not yet committed, so `git_sha` is not the whole story. Null in an image."""

    @classmethod
    def of(cls, where: Path) -> Self:
        """The code in the checkout that holds `where`, or the image's."""
        built = _Built()
        if built.git_sha is not None:
            return cls(git_sha=built.git_sha, image=built.image, dirty=None)
        head, dirty = checkout(where)
        return cls(git_sha=head, image=built.image, dirty=dirty)


class ModelInfo(Model):
    name: str
    """As the run named it: an entry point's name, or a module path."""
    module: str


class ParamsFile(Model):
    """The file a run's params came from."""

    path: str
    """As given."""
    sha256: str
    """Of the file's bytes."""
    git_commit: str | None
    """The HEAD of the checkout the file is in, such as the templates repo's; null outside one."""


class Replayed(Model):
    of: str
    """The run a replay replayed."""
    portfolios: list[str] | None
    """The subset it decided; null for every portfolio of that run."""


class RunInfo(Model):
    """A snapshot's `run.json`: what the run was asked, with which code and data, and when. It holds nothing the
    snapshot's other files hold. A field added after 0.1.0 has a default, so older snapshots still load."""

    run_id: str
    """A UUIDv7, which names the directory and sorts by when runs started."""
    optim_version: str
    model: ModelInfo
    as_of_date: date
    portfolios: list[Id]
    """Every portfolio the run loaded and decided, in the order asked; a replay keeps its run's."""
    params: dict[str, JsonValue]
    """Every param the run decided with, defaults included, as JSON."""
    params_file: ParamsFile
    """Where they came from: for a replay with its run's params, that run's file."""
    labels: dict[str, str]
    """Set by whoever started the run (`--label key=value`), such as `purpose=production` or Airflow's DAG run."""
    code: Code
    workers: int
    """Worker processes. A solve that stops at its time limit depends on how many solves shared the machine."""
    started_at: AwareDatetime
    """When the run started, before it loaded anything. Times are UTC."""
    loaded_at: AwareDatetime
    """When every dataset had loaded; for a replay, when its snapshot had been read."""
    finished_at: AwareDatetime
    """When the last decision was made, just before the snapshot was written."""
    datasets: dict[str, DatasetRead] | None
    """Where each dataset was read from, and when. Null for a replay, whose data is the run's it replays."""
    order: list[Id] | None = None
    """For a model that reserves, the order its portfolios decided in, one at a time; null when they decided at once."""
    replay: Replayed | None
    """A replay only: the run it replayed, and the portfolios it decided."""


class DecisionRow(Row):
    """One per portfolio. A decision's numbers are null when it failed before it was decided."""

    portfolio_id: Id
    status: Literal["accepted", "rejected"]
    """`accepted`: it may trade. `rejected`: `reasons.parquet` says why not."""
    trades: int
    value: float | None
    """The portfolio's value before trading, cash included, at each security's unit value."""
    cash: float | None
    """Cash after the rounded trades."""
    turnover: float | None
    """The value bought plus the value sold."""
    solved: bool | None
    objective: float | None
    solver: str | None
    solver_status: str | None
    """The solver's own status, such as cvxpy's `optimal`."""
    stop: str | None
    """`optimal`, `limit`, `imprecise`, `infeasible`, `unbounded` or `failed`."""
    gap: float | None
    nodes: int | None
    iterations: int | None
    solve_seconds: float | None
    """Time in the solver itself."""
    seconds: float
    """To prepare, decide and check the portfolio."""


class TradeRow(Row):
    portfolio_id: Id
    security_id: Id
    side: Literal["buy", "sell"]
    quantity: float
    """Rounded to the security's increment: what trades if the portfolio is accepted; 0 where rounding left nothing."""
    decided: float | None
    """As the model decided it; null where rounding made a trade the model did not decide."""


class ReasonRow(Row):
    """Why a decision may not trade, one row per reason."""

    portfolio_id: Id
    reason: str


class MetricRow(Row):
    """A number a model reported, one row per portfolio and metric."""

    portfolio_id: Id
    metric: str
    value: float


def result_frames(outcomes: Sequence[Outcome]) -> dict[str, pd.DataFrame]:
    """The outcomes as a snapshot saves them: a row per decision, trade, reason and metric."""
    decisions, trades, reasons, metrics = [], [], [], []
    for outcome in outcomes:
        portfolio_id, decision = outcome.portfolio_id, outcome.decision
        reasons += [{"portfolio_id": portfolio_id, "reason": reason} for reason in outcome.reasons]
        row: dict[str, object] = {"portfolio_id": portfolio_id, "status": "accepted" if outcome.accepted else "rejected", "trades": 0, "seconds": outcome.seconds}
        if decision is not None:
            decided = outcome.decided or decision
            for side, rounded, raw in (("buy", decision.buy, decided.buy), ("sell", decision.sell, decided.sell)):
                trades += [{"portfolio_id": portfolio_id, "security_id": s, "side": side, "quantity": rounded.get(s, 0.0), "decided": raw.get(s)} for s in dict.fromkeys([*raw, *rounded])]
            metrics += [{"portfolio_id": portfolio_id, "metric": name, "value": value} for name, value in decision.metrics.items()]
            row |= {"trades": len(decision.buy) + len(decision.sell), "solved": decision.solved, "objective": decision.objective} | outcome.numbers
            if (solver := decision.solver) is not None:
                row |= {
                    "solver": solver.solver,
                    "solver_status": solver.status,
                    "stop": solver.stop,
                    "gap": solver.gap,
                    "nodes": solver.nodes,
                    "iterations": solver.iterations,
                    "solve_seconds": solver.seconds,
                }
        decisions.append(row)
    return {
        "decisions": conform(DecisionRow, [pd.DataFrame(decisions)], name="decisions"),
        "trades": conform(TradeRow, [pd.DataFrame(trades)], name="trades"),
        "reasons": conform(ReasonRow, [pd.DataFrame(reasons)], name="reasons"),
        "metrics": conform(MetricRow, [pd.DataFrame(metrics)], name="metrics"),
    }


def save(root: Root, info: RunInfo, inputs: Mapping[str, pd.DataFrame], outcomes: Sequence[Outcome]) -> Root:
    """Write `root/RUN_ID/`. The directory appears only once it is complete: it is written under a hidden name and
    renamed. `inputs` are the datasets the run loaded, by name."""
    # writes every dataset every run; store each once per content hash if snapshots grow too fast
    partial, path = root / f".{info.run_id}.{uuid.uuid4().hex}.partial", root / info.run_id
    (partial / "run.json").write_bytes(info.model_dump_json(indent=2).encode())
    for name, frame in ({f"inputs/{name}": frame for name, frame in inputs.items()} | result_frames(outcomes)).items():
        (partial / f"{name}.parquet").write_frame(frame)
    partial.move_to(path)
    log.info("saved snapshot %s", path.location)
    return path


def read_info(snapshot: Root) -> RunInfo:
    if not (snapshot / "run.json").exists():
        msg = f"{snapshot.location} has no run.json: it is not a snapshot"
        raise FileNotFoundError(msg)
    try:
        return RunInfo.model_validate_json((snapshot / "run.json").read_bytes())
    except ValueError as error:
        error.add_note(f"while reading {snapshot.location}/run.json, which an older optim may have written")
        raise


def read_inputs(snapshot: Root, datasets: Mapping[str, Dataset]) -> dict[str, pd.DataFrame]:
    """The datasets a run loaded, by name, each validated as a load validates it, so a snapshot saved before a model
    changed a dataset's row model fails here, naming the dataset and the column."""
    frames = {}
    for dataset in datasets.values():
        file = snapshot / "inputs" / f"{dataset.name}.parquet"
        if not file.exists():
            msg = f"{snapshot.location} has no {dataset.name} dataset: the run was saved before the model read it"
            raise FileNotFoundError(msg)
        try:
            frames[dataset.name] = dataset.to_frame(to_columns(file.read_frame()[0]))
        except ValueError as error:
            error.add_note(f"while reading {file.location}")
            raise
    return frames


def read_results(snapshot: Root) -> dict[str, pd.DataFrame]:
    """A snapshot's result tables, by name: `decisions`, `trades`, `reasons` and `metrics`."""
    return {name: (snapshot / f"{name}.parquet").read_frame()[0] for name in ("decisions", "trades", "reasons", "metrics")}


def checkout(where: Path) -> tuple[str | None, bool | None]:
    """The git checkout that holds `where`: its HEAD, and whether it has changes not yet committed. Neither outside
    one, as for an installed package."""
    try:
        head = _git(where, "rev-parse", "HEAD")
        status = _git(where, "status", "--porcelain")
    except OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired:  # no git, or not a checkout
        return None, None
    return head, bool(status)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=10).stdout.strip()  # noqa: S603, S607 -- fixed arguments; git from PATH, as a developer runs it
