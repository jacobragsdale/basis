"""A snapshot of every run: what it was asked, the datasets it loaded and every decision, so any decision can be
explained and replayed. Runs are kept in basis-core's run store (`basis.core.storage.Store`), under the package
`optim`: each run a manifest, `runs/optim/REPO.MODEL/RUN_ID.json`, that holds its record (`RunInfo`) and names each dataset
it saved by the hash of its object, `objects/HASH.parquet`, which the store holds once however many runs save it:

    inputs/NAME       each dataset exactly as loaded
    results/NAME      the result tables: decisions, trades, reasons and metrics
    outputs/orders    the accepted portfolios' orders, as `basis run --publish` writes them (`optim.orders`)

`MODEL` is the model's name as the run gave it. What a model derived from its inputs is not saved: replay derives it
again with the same code. The objects are written first and the manifest last, so a reader never sees a run whose
datasets are missing. A run that publishes is saved before it writes, and its manifest saved again with each
database's outcome, its datasets unchanged, so `basis resume` writes the same orders. The result tables' row models
are below."""

import logging
import subprocess
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Literal, Self, cast

import pandas as pd
import pyarrow as pa
from pydantic import AwareDatetime, Field, JsonValue
from pydantic_settings import BaseSettings, SettingsConfigDict

from basis.core.snapshots import PublishRecord
from basis.core.storage import DIGEST, Root, Store
from optim.data.dataset import Dataset
from optim.data.frames import conform, to_columns
from optim.data.loader import DatasetRead
from optim.decide import Outcome
from optim.orders import OUTPUT, OrderRow, Publishing
from optim.schema import Id, Model, Row

log = logging.getLogger(__name__)
PACKAGE = "optim"
"""The part whose runs these are, which names their folder in the store."""


class _Built(BaseSettings):
    """What an image says about itself: the commit, baked in at build (`--build-arg GIT_SHA`), and the image, which the
    scheduler sets."""

    model_config = SettingsConfigDict(env_prefix="BASIS_", extra="ignore", frozen=True)

    git_sha: str | None = None
    image: str | None = None


class Code(Model):
    """The model's code a run ran, as its manifest records it."""

    git_sha: str | None
    """The commit: `BASIS_GIT_SHA`, baked into the image, or else the HEAD of the checkout the model's module is in.
    Null when neither is known."""
    image: str | None
    """The image, from `BASIS_IMAGE`. Null outside an image."""
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
    identity: str | None = None
    """The model as no other repo's is, `REPO.NAME` (`optim.models.LoadedModel.identity`): the run's folder in the
    store and its name in the ledger. None in a run saved before it was recorded, which is in its name's folder and
    known to the ledger by its module."""

    @property
    def folder(self) -> str:
        """The folder of the store that holds the run's manifest."""
        return self.identity or self.name

    @property
    def ledgered(self) -> str:
        """The name the ledger knows the run's orders by."""
        return self.identity or self.module


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
    """A run's record, in its manifest: what the run was asked, with which code and data, and when. It holds nothing
    the run's datasets hold. A field added from here on has a default, so older runs still load."""

    run_id: str
    """A UUIDv7, which names the manifest and sorts by when runs started."""
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
    reads: dict[str, DatasetRead] | None = None
    """Where each dataset was read from, and when. Null for a replay, whose data is the run's it replays."""
    order: list[Id] | None = None
    """For a model that reserves, the order its portfolios decided in, one at a time; null when they decided at once."""
    replay: Replayed | None
    """A replay only: the run it replayed, and the portfolios it decided."""
    publishing: Publishing | None = None
    """Where the run publishes its orders, as `basis run --publish` asked; null for a run that does not publish."""
    publishes: dict[str, PublishRecord] = Field(default_factory=dict)
    """Each database the run published its orders to, once it has: `committed`, with what the database counted, or
    `failed`, with the error, which `basis resume` finishes. Empty before then, and for a run with no orders."""


class DecisionRow(Row):
    """One per portfolio. A decision's numbers are null when it failed before it was decided."""

    portfolio_id: Id
    status: Literal["accepted", "rejected"]
    """`accepted`: it may trade. `rejected`: the `reasons` table says why not."""
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
    """`optimal`, `limit`, `imprecise`, `violated`, `infeasible`, `unbounded` or `failed`."""
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


RESULTS: dict[str, type[Row]] = {"decisions": DecisionRow, "trades": TradeRow, "reasons": ReasonRow, "metrics": MetricRow}
ORDERS = f"outputs/{OUTPUT}"


class Manifest(RunInfo):
    """A run as the store keeps it: its record, and the datasets it saved."""

    datasets: dict[str, Annotated[str, Field(pattern=f"^{DIGEST.pattern}$")]]
    """Each dataset the run saved, `inputs/NAME`, `results/NAME` and `outputs/orders`, by its object's hash."""


@dataclass(frozen=True)
class Saved:
    """A saved run, its manifest read once."""

    store: Store
    path: Root
    """Where its manifest is."""
    info: Manifest

    def inputs(self, datasets: Mapping[str, Dataset]) -> dict[str, pd.DataFrame]:
        """The datasets the run loaded, by name, each validated as a load validates it, so a run saved before a model
        changed a dataset's row model fails here, naming the dataset and the column."""
        frames = {}
        for dataset in datasets.values():
            if (found := self.info.datasets.get(f"inputs/{dataset.name}")) is None:
                msg = f"run {self.info.run_id} has no {dataset.name} dataset: it was saved before the model read it"
                raise FileNotFoundError(msg)
            try:
                frames[dataset.name] = dataset.to_frame(to_columns(self._frame(found)))
            except ValueError as error:
                error.add_note(f"while reading run {self.info.run_id}'s {dataset.name}, {self.store.object(found).location}")
                raise
        return frames

    def results(self) -> dict[str, pd.DataFrame]:
        """The run's result tables, by name, `decisions`, `trades`, `reasons` and `metrics`, typed as the run wrote them."""
        return {name: conform(row, [self._frame(self.info.datasets[f"results/{name}"])], name=name) for name, row in RESULTS.items()}

    def orders(self) -> pd.DataFrame:
        """The accepted portfolios' orders, as the run saved them to publish: `optim.orders.OrderRow`s."""
        if (found := self.info.datasets.get(ORDERS)) is None:
            msg = f"run {self.info.run_id} has no orders: it was saved before optim saved them, and cannot publish"
            raise LookupError(msg)
        return conform(OrderRow, [self._frame(found)], name=OUTPUT)

    def save_publishes(self, publishes: Mapping[str, PublishRecord]) -> Saved:
        """Save the run's manifest again with each database's outcome of publishing its orders, and nothing else
        changed; and return the run as saved."""
        manifest = Manifest.model_validate(self.info.model_dump() | {"publishes": dict(publishes)})
        self.path.write_bytes(manifest.model_dump_json(indent=2).encode())
        return Saved(self.store, self.path, manifest)

    def _frame(self, digest: str) -> pd.DataFrame:
        return cast("pd.DataFrame", self.store.object(digest).read_table().to_pandas())  # pyarrow is untyped


def save(root: Root, info: RunInfo, inputs: Mapping[str, pd.DataFrame], outcomes: Sequence[Outcome], orders: pd.DataFrame) -> Saved:
    """Save a new run in the store at `root`: each dataset the store does not hold yet, then its manifest. `inputs` are
    the datasets the run loaded, by name, and `orders` its outcomes' orders, as `optim.orders.orders_of` builds them."""
    store = Store(root)
    path = store.manifest(PACKAGE, info.model.folder, info.run_id)
    if path.exists():
        msg = f"run {info.run_id} is in the store already, at {path.location}: a new run takes a new id"
        raise FileExistsError(msg)
    frames = {f"inputs/{name}": frame for name, frame in inputs.items()} | {f"results/{name}": frame for name, frame in result_frames(outcomes).items()}
    frames[ORDERS] = orders
    manifest = Manifest.model_validate(info.model_dump() | {"datasets": {key: store.put(_table(frame)) for key, frame in frames.items()}})
    path.write_bytes(manifest.model_dump_json(indent=2).encode())
    log.info("saved run %s", path.location)
    return Saved(store, path, manifest)


def _table(frame: pd.DataFrame) -> pa.Table:
    """A frame as the store hashes it: its columns alone, without pandas' own metadata, which names pandas' version and
    would store the same data once per version. Every frame is validated again as it is read back."""
    return pa.Table.from_pandas(frame, preserve_index=False).replace_schema_metadata(None)


def read(path: Root) -> Saved:
    """The run whose manifest is at `path`, `ROOT/runs/optim/REPO.MODEL/RUN_ID.json`, in the store at `ROOT`."""
    package = path.parent.parent
    if not path.name.endswith(".json") or package.name != PACKAGE or package.parent.name != "runs":
        msg = f"{path.location} is not a run of optim's: give a run's id, or the path of its manifest, ROOT/runs/{PACKAGE}/REPO.MODEL/RUN_ID.json"
        raise LookupError(msg)
    try:
        manifest = Manifest.model_validate_json(path.read_bytes())
    except ValueError as error:
        error.add_note(f"while reading the manifest {path.location}, which another version of optim may have written")
        raise
    return Saved(Store(package.parent.parent), path, manifest)


def find(roots: Sequence[Root], run_id: str) -> Saved:
    """The run with id `run_id`, from the first store of `roots` that holds it."""
    for root in roots:
        try:
            path = Store(root).find(run_id)
        except LookupError:
            continue
        return read(path)
    msg = f"no run {run_id} in {' or '.join(root.location for root in roots)}: give the id of a run there, or the path of its manifest, or set BASIS_SNAPSHOT_ROOT to the run store that holds it"
    raise LookupError(msg)


def runs_of(root: Root, names: Iterable[str]) -> Iterator[Saved]:
    """The runs saved under any of `names`, a model's name and its module path, newest first, each manifest read only
    when it is reached: run ids sort by when their runs started."""
    store = Store(root)
    paths = sorted((path for name in dict.fromkeys(names) for path in store.manifests(PACKAGE, name)), key=lambda path: path.name, reverse=True)
    return (read(path) for path in paths)


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
