"""Running a model, as the command line and a model's `main.py` (`optim.api`) both do. `run_model` validates the
params, loads the model's data, decides every portfolio and saves the snapshot; `replay_run` decides a saved run again,
with the code you have now and, if given, other params. Both decide through `decide_all`, which calls the model's
hooks: `prepare_market` once in this process, then for each portfolio, in worker processes, `prepare_portfolio` and
`decide`; the core rounds the decision and reviews it (`optim.acceptance`), and the model's `check` adds its own
reasons."""

import hashlib
import json
import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from functools import cached_property
from pathlib import Path
from types import MappingProxyType
from zoneinfo import ZoneInfo

import pandas as pd

import optim
from optim import logs, models
from optim.contract import LISTING, Datasets, Params, declared
from optim.data.dataset import Dataset
from optim.data.loader import Loader, load_data
from optim.decide import Outcome, decide_all
from optim.models import LoadedModel
from optim.orders import Orders
from optim.snapshots import Code, ModelInfo, ParamsFile, Replayed, RunInfo, checkout, read_info, read_inputs, result_frames, save
from optim.storage import Root

log = logging.getLogger(__name__)
NO_LABELS: Mapping[str, str] = MappingProxyType({})
MARKET_TIME = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Run:
    """A run's result: its id, each portfolio's outcome, and the tables its snapshot holds, to look at in a debugger."""

    run_id: str
    outcomes: list[Outcome]

    @property
    def ok(self) -> bool:
        """Every portfolio was accepted: the run may trade."""
        return all(outcome.accepted for outcome in self.outcomes)

    @cached_property
    def _tables(self) -> dict[str, pd.DataFrame]:
        return result_frames(self.outcomes)

    @property
    def decisions(self) -> pd.DataFrame:
        """A row per portfolio: accepted or rejected, its trades, its value, cash and turnover, and how its solve went."""
        return self._tables["decisions"]

    @property
    def trades(self) -> pd.DataFrame:
        """A row per trade: rounded, and as the model decided it."""
        return self._tables["trades"]

    @property
    def reasons(self) -> pd.DataFrame:
        """A row per reason a portfolio was rejected."""
        return self._tables["reasons"]

    @property
    def metrics(self) -> pd.DataFrame:
        """A row per portfolio and metric the model reported."""
        return self._tables["metrics"]

    def summary(self) -> str:
        """What `optim run` prints: the run's id, a row per portfolio, and each reason a portfolio was rejected."""
        decisions = self.decisions[["portfolio_id", "status", "trades", "solved", "objective", "stop", "seconds"]]
        reasons = "".join(f"{outcome.portfolio_id}: {reason}\n" for outcome in self.outcomes for reason in outcome.reasons)
        return f"run {self.run_id}\n{decisions.to_string(index=False)}\n{reasons}"


def today() -> date:
    """The business date a run takes unless it is given one: today in New York."""
    return datetime.now(MARKET_TIME).date()


def fixture_date(directory: Path | None) -> date | None:
    """The date in `directory`'s `fixture.json`, which a run that reads that folder's files takes unless it is given
    one; None where the folder has none."""
    if directory is None or not (file := directory / "fixture.json").exists():
        return None
    return date.fromisoformat(json.loads(file.read_text(encoding="utf-8"))["as_of_date"])


def read_params(model: LoadedModel, path: Path) -> tuple[Params, ParamsFile]:
    """Validate a params file against the model's `Params`, before anything loads, and record where it came from."""
    raw = path.read_bytes()
    try:
        params = model.params.model_validate_json(raw)
    except ValueError as error:
        error.add_note(f"while reading {path} as {model.name}'s params")
        raise
    head, _ = checkout(path.resolve().parent)
    return params, ParamsFile(path=str(path), sha256=hashlib.sha256(raw).hexdigest(), git_commit=head)


def datasets_of(model: LoadedModel, data_dir: Path | None) -> dict[str, Dataset]:
    """The datasets a run of the model reads, by attribute: each that has a file in `data_dir`, `<name>.parquet` or
    `<name>.csv`, read from it instead of its source."""
    datasets = declared(model.data)
    return datasets if data_dir is None else {attribute: from_dir(dataset, data_dir) for attribute, dataset in datasets.items()}


def run_model(
    loader: Loader,
    snapshot_root: Root,
    model: LoadedModel,
    params_file: Path,
    *,
    as_of_date: date,
    portfolio_ids: Sequence[str] | None = None,
    data_dir: Path | None = None,
    workers: int = 1,
    labels: Mapping[str, str] = NO_LABELS,
    orders: Orders | None = None,
    clock: Callable[[], datetime] = logs.utc_now,
) -> Run:
    """Load the model's data for `as_of_date` and the portfolios, decide each one, save the snapshot under
    `snapshot_root`, and then write the accepted portfolios' orders to `orders`, if given. `data_dir` reads each dataset
    that has a file there instead of its source. Without `portfolio_ids`, the run decides every portfolio the model
    lists, or, for a model that lists none, every portfolio in a holdings file."""
    params, params_file_info = read_params(model, params_file)
    snapshot_root.make_dir()  # storage that cannot be written fails the run before it loads, not after it decides
    run_id = str(uuid.uuid7())
    logs.bind(run_id=run_id, model=model.name)
    log.info("starting run", extra={"optim_version": optim.__version__, "model_module": model.module, "params": str(params_file), "as_of_date": as_of_date})
    started_at = clock()
    datasets = datasets_of(model, data_dir)
    if portfolio_ids is None and LISTING not in datasets:
        portfolio_ids = portfolios_in(datasets["holdings"])
    frames, reads, portfolio_ids = load_data(loader, datasets, as_of_date, None if portfolio_ids is None else list(dict.fromkeys(portfolio_ids)))
    loaded_at = clock()
    decided = decide_all(model, params, model.data(frames, as_of_date=as_of_date), portfolio_ids, workers=workers)
    info = RunInfo(
        run_id=run_id,
        optim_version=optim.__version__,
        model=ModelInfo(name=model.name, module=model.module),
        as_of_date=as_of_date,
        portfolios=portfolio_ids,
        params=params.model_dump(mode="json"),
        params_file=params_file_info,
        labels=dict(labels),
        code=Code.of(model.directory),
        workers=workers,
        started_at=started_at,
        loaded_at=loaded_at,
        finished_at=clock(),
        datasets=reads,
        order=decided.order,
        replay=None,
    )
    save(snapshot_root, info, frames, decided.outcomes)
    if orders is not None:
        with logs.timed(log, "wrote orders", table=orders.table) as fields:
            fields["orders"] = orders.write(run_id, decided.outcomes)
    return Run(run_id, decided.outcomes)


@dataclass(frozen=True)
class Snapshot:
    """A saved run, opened with the code you have now: its record, its model, the params it ran with, and its data."""

    root: Root
    info: RunInfo
    model: LoadedModel
    params: Params
    """The run's params, validated by the model's `Params` as it is now."""
    data: Datasets
    """Each dataset as the run loaded it, validated by the model's datasets as they are now."""

    def portfolios(self, only: Sequence[str] | None = None) -> list[str] | None:
        """`only`, checked against the run's portfolios; without it, those a replay of a subset decided, or None for
        every portfolio of the run."""
        chosen = list(only) if only else self.info.replay.portfolios if self.info.replay else None
        if missing := sorted(set(chosen or ()) - set(self.info.portfolios)):
            msg = f"{len(missing)} portfolios are not in {self.root.location}, e.g. {missing[:5]}"
            raise LookupError(msg)
        return chosen


def open_snapshot(root: Root) -> Snapshot:
    """A snapshot, read alone: its model by the name the run used if it is installed, else by its module path."""
    info = read_info(root)
    model = models.load(info.model.name if info.model.name in models.installed() else info.model.module)
    params = model.params.model_validate_json(json.dumps(info.params))
    return Snapshot(root, info, model, params, model.data(read_inputs(root, declared(model.data)), as_of_date=info.as_of_date))


def replay_run(
    snapshot: Root,
    snapshot_root: Root,
    *,
    params_file: Path | None = None,
    portfolio_ids: Sequence[str] | None = None,
    workers: int = 1,
    labels: Mapping[str, str] = NO_LABELS,
    clock: Callable[[], datetime] = logs.utc_now,
) -> Run:
    """Decide a saved run again on the datasets its snapshot holds, with the code you have now and, if given, other
    params, and save it as a new run under `snapshot_root`. A replay of a subset of portfolios replays that subset; for a
    model that reserves, the portfolios before them in its order decide again too."""
    started_at = clock()
    saved = open_snapshot(snapshot)
    model, run_id = saved.model, str(uuid.uuid7())
    logs.bind(run_id=run_id, model=model.name)
    log.info("starting replay", extra={"optim_version": optim.__version__, "of": saved.info.run_id, "params": str(params_file) if params_file else "as saved"})
    params, params_file_info = read_params(model, params_file) if params_file else (saved.params, saved.info.params_file)
    only = saved.portfolios(portfolio_ids)
    loaded_at = clock()
    decided = decide_all(model, params, saved.data, saved.info.portfolios, workers=workers, only=only)
    info = RunInfo(
        run_id=run_id,
        optim_version=optim.__version__,
        model=ModelInfo(name=model.name, module=model.module),
        as_of_date=saved.info.as_of_date,
        portfolios=saved.info.portfolios,
        params=params.model_dump(mode="json"),
        params_file=params_file_info,
        labels=dict(labels),
        code=Code.of(model.directory),
        workers=workers,
        started_at=started_at,
        loaded_at=loaded_at,
        finished_at=clock(),
        datasets=None,
        order=decided.order,
        replay=Replayed(of=saved.info.run_id, portfolios=only),
    )
    save(snapshot_root, info, saved.data.frames, decided.outcomes)
    return Run(run_id, decided.outcomes)


def from_dir(dataset: Dataset, directory: Path) -> Dataset:
    """The dataset read from `directory`, as `<name>.parquet` or `<name>.csv`, where it has a file there."""
    for suffix in (".parquet", ".csv"):
        if (file := directory / f"{dataset.name}{suffix}").exists():
            return dataset.file(file)
    return dataset


def portfolios_in(holdings: Dataset) -> list[str]:
    """Every portfolio in a holdings file, in the file's order."""
    if holdings.source != "file":
        msg = "give the portfolios to decide: the model does not list its own in a `portfolios` dataset, and its holdings are not read from a file"
        raise ValueError(msg)
    frame = pd.read_csv(holdings.query, dtype=str) if str(holdings.query).endswith(".csv") else pd.read_parquet(holdings.query, columns=["portfolio_id"])
    return list(dict.fromkeys(frame["portfolio_id"]))
