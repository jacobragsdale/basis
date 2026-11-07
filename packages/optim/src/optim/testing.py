"""What a model repo's tests use from optim: the contract tests, and builders for behaviour tests.

`ContractTests` holds the contract tests of one model, which its `tests/test_contract.py` subclasses, and
`check_agent_files` the check of the repo's agent files, which its `tests/test_agent_files.py` calls. They are plain
tests, which run like any other, in the IDE's test panel too.

The builders make frames from a few partial rows, each filled from a valid default row, so a test states only the
fields it is about, and a model's `Data` from those frames. Every frame is validated by its dataset's row model, as a
load validates it.

    holdings = frame(HOLDINGS, {"security_id": "USD", "quantity": 1e6}, {"security_id": "A", "quantity": 50_000})
    data = optim.testing.data(Data, holdings=holdings, securities=frame(SECURITIES, CASH, {"security_id": "A"}))
"""

import json
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from types import ModuleType

import grimp
import pandas as pd

from optim import models, sync
from optim.contract import LISTING, Datasets, declared
from optim.data.dataset import Dataset
from optim.data.frames import empty_frame
from optim.data.loader import Loader, load_data
from optim.decide import decide_all
from optim.models import LoadedModel
from optim.runs import from_dir, portfolios_in, read_params
from optim.sources.file import FileSource

FORBIDDEN = (
    "optim.sources.sql", "optim.sources.oracle", "optim.sources.mssql", "optim.sources.rest", "optim.sources.file",
    "optim.data.loader", "optim.data.cache", "optim.storage", "optim.snapshots", "optim.orders", "optim.runs",
    "optim.api", "optim.research", "optim.context", "optim.settings", "optim.cli", "oracledb", "mssql_python", "pyodbc",
    "pymssql", "cx_Oracle", "sqlalchemy", "psycopg", "psycopg2", "snowflake", "httpx", "requests", "urllib3", "aiohttp",
)  # fmt: skip
"""What a model's code may not reach through its imports: a source, the loader, the cache, outputs, a database driver or
an HTTP client."""


class ContractError(AssertionError):
    """A model breaks the contract; the message says how, and what to do."""


class ContractTests:
    """The contract tests of one model. Subclass it in the model's `tests/test_contract.py`, with `model` set to the
    model's `model.py` module:

        class TestContract(ContractTests):
            model = model

    The model's default params validate; every portfolio of its fixture is accepted with them, and reports exactly the
    metrics its `Metrics` declares; and its code cannot reach I/O through its imports. `params` and `fixture` are
    relative to the model's folder."""

    model: ModuleType
    params = "params/default.json"
    fixture = "tests/fixture"

    @property
    def _loaded(self) -> LoadedModel:
        return models.read(models.name_of(self.model), self.model)

    def test_its_default_params_validate(self) -> None:
        check_params(self._loaded, self._loaded.directory / self.params)

    def test_every_portfolio_of_its_fixture_is_accepted(self) -> None:
        check_fixture(self._loaded, self._loaded.directory / self.fixture, self._loaded.directory / self.params)

    def test_its_code_cannot_reach_io_through_its_imports(self) -> None:
        check_imports(self._loaded)


def check_params(model: LoadedModel, params: Path) -> None:
    __tracebackhide__ = True
    if not params.exists():
        msg = f"{model.name} has no default params: add {params}"
        raise ContractError(msg)
    read_params(model, params)


def check_fixture(model: LoadedModel, fixture: Path, params: Path) -> None:
    """Load every dataset from the fixture, decide every portfolio it lists, or every one in its holdings, with the
    default params, and require each to be accepted, reporting only the metrics its `Metrics` declares."""
    __tracebackhide__ = True
    if not (fixture / "fixture.json").exists():
        msg = f"{model.name} has no fixture: add {fixture}, a file per dataset and fixture.json with its as_of_date"
        raise ContractError(msg)
    datasets = {attribute: from_dir(dataset, fixture) for attribute, dataset in declared(model.data).items()}
    if missing := sorted(dataset.name for dataset in datasets.values() if dataset.source != "file"):
        msg = f"{model.name}'s fixture {fixture} has no file for {missing}: add NAME.csv or NAME.parquet for each"
        raise ContractError(msg)
    as_of_date = date.fromisoformat(json.loads((fixture / "fixture.json").read_text(encoding="utf-8"))["as_of_date"])
    portfolio_ids = None if LISTING in datasets else portfolios_in(datasets["holdings"])
    frames, _, portfolio_ids = load_data(Loader({"file": FileSource()}), datasets, as_of_date, portfolio_ids)
    outcomes = decide_all(model, read_params(model, params)[0], model.data(frames, as_of_date=as_of_date), portfolio_ids, workers=1).outcomes
    if rejected := [f"{outcome.portfolio_id}: {'; '.join(outcome.reasons)}" for outcome in outcomes if not outcome.accepted]:
        msg = f"{model.name} rejects portfolios of its fixture {fixture}:\n" + "\n".join(rejected)
        raise ContractError(msg)
    declared_metrics = set(model.metrics.model_fields) if model.metrics else set()
    if wrong := sorted({name for outcome in outcomes if outcome.decision for name in outcome.decision.metrics} ^ declared_metrics):
        msg = f"{model.name}'s decisions report metrics its Metrics does not declare, or declares metrics they do not report: {wrong}"
        raise ContractError(msg)


def check_imports(model: LoadedModel) -> None:
    """No module the model's `model.py` imports, directly or not, can reach what `FORBIDDEN` lists. Its `main.py` and
    its tests are not model code: workers never import them."""
    __tracebackhide__ = True
    graph = _graph(model.module.partition(".")[0])
    chains = [chain for forbidden in FORBIDDEN if forbidden in graph.modules for chain in graph.find_shortest_chains(importer=model.module, imported=forbidden, as_packages=True)]
    if chains:
        msg = f"{model.name} can reach I/O through its imports, which model code must not do: data arrives in its Data.\n" + "\n".join(" -> ".join(chain) for chain in sorted(chains))
        raise ContractError(msg)


def _graph(package: str) -> grimp.ImportGraph:
    """The package's import graph, with optim's: built on each check, which takes milliseconds, so it is never stale."""
    graph = grimp.build_graph(package, "optim", include_external_packages=True, exclude_type_checking_imports=True)
    if graph.direct_import_exists(importer="optim", imported="optim.api"):
        graph.remove_import(importer="optim", imported="optim.api")  # `optim.run` and friends load on first use, which model code never makes
    return graph


def check_agent_files(repo: Path) -> None:
    """`AGENTS.md`'s optim block and the `optim-*` skills in the repo at `repo` match the installed optim."""
    __tracebackhide__ = True
    if stale := sync.stale(repo):
        msg = f"the agent files do not match the installed optim: run `uv run optim sync`, which rewrites {', '.join(map(str, stale))}"
        raise ContractError(msg)


AS_OF = date(2026, 10, 16)

BOND: dict[str, object] = {
    "security_id": "A",
    "cusip": None,
    "isin": None,
    "asset_class": "bond",
    "currency": "USD",
    "issuer": "Issuer A",
    "sector": "Corporate",
    "rating": "AA",
    "maturity": date(2030, 10, 15),
    "price": 100.0,
    "accrued": 0.0,
    "price_per": 100,
    "increment": 1000.0,
    "min_piece": 1000.0,
    "min_remaining": 1000.0,
}
"""A bond priced at par, traded in increments of 1,000."""
CASH: dict[str, object] = BOND | {
    "security_id": "USD",
    "asset_class": "cash",
    "issuer": None,
    "sector": None,
    "rating": None,
    "maturity": None,
    "price": 1.0,
    "price_per": 1,
    "increment": 0.01,
    "min_piece": None,
    "min_remaining": None,
}
"""The cash security: a holding of it is the portfolio's cash, valued at 1 a unit."""
HOLDING: dict[str, object] = {"portfolio_id": "P1", "security_id": "USD", "quantity": 1_000_000.0}
"""P1 holds $1mm of cash."""

DEFAULTS: dict[str, Mapping[str, object]] = {"securities": BOND, "holdings": HOLDING}
"""The default row of each shared dataset, by name."""


def frame(dataset: Dataset, *rows: Mapping[str, object], default: Mapping[str, object] | None = None) -> pd.DataFrame:
    """`rows` as `dataset`'s validated frame. Each row's missing fields come from `default`, which is the shared
    datasets' default row for holdings and securities; a field neither gives is null."""
    base = DEFAULTS.get(dataset.name, {}) if default is None else default
    full = [dict(base) | dict(row) for row in rows]
    return dataset.to_frame({name: [row.get(name) for row in full] for name in dataset.row.model_fields}) if full else empty_frame(dataset.row)


def data[D: Datasets](cls: type[D], frames: Mapping[str, pd.DataFrame] | None = None, /, *, as_of_date: date = AS_OF, portfolio_id: str | None = None, **more: pd.DataFrame) -> D:
    """A model's `Data` from frames by attribute name, in a mapping or as keywords, such as `holdings=...`; a dataset
    not given has no rows."""
    frames, datasets = {**(frames or {}), **more}, declared(cls)
    if unknown := sorted(set(frames) - set(datasets)):
        msg = f"{cls.__qualname__} reads no datasets named {unknown}; it reads {sorted(datasets)}"
        raise TypeError(msg)
    return cls({dataset.name: frames[name] if name in frames else empty_frame(dataset.row) for name, dataset in datasets.items()}, as_of_date=as_of_date, portfolio_id=portfolio_id)
