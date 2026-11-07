"""What a job repo's tests use: the contract tests, and builders for a transform's own tests.

`ContractTests` holds the contract tests of one job, which its `tests/test_contract.py` subclasses. They are plain
tests, which run like any other, in an IDE's test panel too:

    class TestContract(ContractTests):
        job = job

The builders make frames from a few partial rows, each conformed to its row as a load would, so a test states only
the columns it is about:

    trades = frame(Trade, {"trade_id": "T1", "quantity": Decimal("100")})
    outputs = job.transform(inputs(job.Inputs, trades=trades, interval="2026-11-05..2026-11-06"), job.Params())
"""

from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from types import ModuleType

import grimp
import polars as pl

from etl_framework import api, runs, sync
from etl_framework import job as jobs
from etl_framework.inputs import Inputs, Query
from etl_framework.interval import Interval
from etl_framework.rows import Row, conform
from etl_framework.settings import GivenSettings
from etl_framework.storage import Root

FORBIDDEN = (
    "etl_framework.api", "etl_framework.cli", "etl_framework.databases", "etl_framework.loader", "etl_framework.new",
    "etl_framework.publish", "etl_framework.runs", "etl_framework.settings", "etl_framework.snapshots",
    "etl_framework.storage", "etl_framework.sync", "etl_framework.testing", "mssql_python", "oracledb", "pyodbc",
    "pymssql", "cx_Oracle", "sqlalchemy", "psycopg", "psycopg2", "snowflake", "httpx", "requests", "urllib3", "aiohttp",
    "pyarrow.fs", "boto3", "azure",
)  # fmt: skip
"""What a job's code may not reach through its imports: the run loop, a database driver, an HTTP client, or storage."""


class ContractError(AssertionError):
    """A job breaks the contract; the message says how, and what to do."""


class ContractTests:
    """The contract tests of one job: its default params validate; its transform runs on its fixture and every check
    passes; and its code cannot reach a database, a file or the network through its imports. `params` and `fixture`
    are relative to the job's folder."""

    job: ModuleType
    params = "params/default.json"
    fixture = "tests/fixture"

    @property
    def _job(self) -> jobs.Job:
        return jobs.read(self.job)

    def test_its_default_params_validate(self) -> None:
        check_params(self._job, self._job.directory / self.params)

    def test_its_fixture_passes_every_check(self, tmp_path: Path) -> None:
        check_fixture(self._job, self._job.directory / self.fixture, self._job.directory / self.params, tmp_path)

    def test_its_code_cannot_reach_io_through_its_imports(self) -> None:
        check_imports(self._job)


def check_params(job: jobs.Job, params: Path) -> None:
    __tracebackhide__ = True
    if not params.exists():
        msg = f"{job.name} has no default params: add {params}"
        raise ContractError(msg)
    runs.read_params(job, params)


def check_fixture(job: jobs.Job, fixture: Path, params: Path, snapshot_root: Path) -> runs.Result:
    """Run the job on its fixture, which must hold a file for every dataset, and require every check to pass."""
    __tracebackhide__ = True
    datasets = job.inputs.declared()
    if missing := sorted(name for name, dataset in datasets.items() if isinstance(dataset, Query) and not any((fixture / f"{name}{suffix}").exists() for suffix in (".csv", ".parquet"))):
        msg = f"{job.name}'s fixture {fixture} has no file for {missing}: add NAME.csv or NAME.parquet for each"
        raise ContractError(msg)
    interval = api.fixture_interval(fixture)
    request = runs.Request(params, Interval.parse(interval) if interval is not None else None, fixture)
    result = runs.run(job, GivenSettings(), request, snapshot_root=Root.at(snapshot_root))
    if not result.ok:
        problems = result.record.reasons + ([result.record.error] if result.record.error is not None else [])
        msg = f"{job.name} fails on its fixture {fixture}:\n" + "\n".join(f"  - {problem}" for problem in problems)
        raise ContractError(msg)
    return result


def check_imports(job: jobs.Job) -> None:
    """No module the job's `job.py` imports, directly or not, can reach what `FORBIDDEN` lists. Its `main.py` and its
    tests are not job code."""
    __tracebackhide__ = True
    graph = _graph(job.module.partition(".")[0])
    chains = [
        chain
        for forbidden in FORBIDDEN
        if forbidden in graph.modules or any(module.startswith(f"{forbidden}.") for module in graph.modules)
        for chain in graph.find_shortest_chains(importer=job.module, imported=forbidden, as_packages=True)
    ]
    if chains:
        msg = f"{job.name} can reach I/O through its imports, which job code must not do: its data arrives in its Inputs.\n" + "\n".join(" -> ".join(chain) for chain in sorted(chains))
        raise ContractError(msg)


def _graph(package: str) -> grimp.ImportGraph:
    """The package's import graph, with etl_framework's, built on each check, so it is never stale."""
    packages = dict.fromkeys([package, "etl_framework"])
    graph = grimp.build_graph(*packages, include_external_packages=True, exclude_type_checking_imports=True)
    if graph.direct_import_exists(importer="etl_framework", imported="etl_framework.api"):
        graph.remove_import(importer="etl_framework", imported="etl_framework.api")  # `etl.run` loads on first use, which job code never makes
    return graph


def check_agent_files(repo: Path) -> None:
    """`AGENTS.md`'s block and the `etl-*` skills in the repo at `repo` match the installed etl-framework."""
    __tracebackhide__ = True
    if stale := sync.stale(repo):
        msg = f"the agent files do not match the installed etl-framework: run `uv run etl sync`, which rewrites {', '.join(map(str, stale))}"
        raise ContractError(msg)


def frame(row: type[Row], *rows: Mapping[str, object]) -> pl.DataFrame:
    """`rows` as `row`'s frame, conformed as a load conforms it: a column a row leaves out is null, a naive datetime is
    in its column's zone, and a `Decimal`, `date` or `str` converts as it would from a database or a CSV."""
    if unknown := sorted({name for values in rows for name in values} - set(row.names())):
        msg = f"{row.__qualname__} has no columns {unknown}; it has {row.names()}"
        raise TypeError(msg)
    if not rows:
        return row.empty()
    columns = {name: pl.Series(name, [values.get(name) for values in rows], dtype=_loose(row, name), strict=False) for name in row.names()}
    return conform(pl.DataFrame(list(columns.values())), row, what=f"a {row.__qualname__} frame", naive="in_zone")


def _loose(row: type[Row], name: str) -> pl.DataType | None:
    """The type a builder's Python values are read as, before conforming: decimals at their row's scale, so a value
    given as an int or a str is exact; datetimes naive, for the column's zone; the rest as polars infers them."""
    column = row.column(name)
    if column.kind == "decimal":
        return column.dtype
    return None


def inputs[I: Inputs](cls: type[I], *, interval: Interval | str | None = None, **frames: pl.DataFrame) -> I:
    """A job's `Inputs` from frames by dataset name; a dataset not given has no rows."""
    declared = cls.declared()
    if unknown := sorted(set(frames) - set(declared)):
        msg = f"{cls.__qualname__} reads no datasets named {unknown}; it reads {sorted(declared)}"
        raise TypeError(msg)
    parsed = Interval.parse(interval) if isinstance(interval, str) else interval
    return cls({name: frames[name] if name in frames else dataset.row.empty() for name, dataset in declared.items()}, interval=parsed)


def at(day: date) -> datetime:
    """A date's midnight UTC, as an interval's ends are."""
    return datetime(day.year, day.month, day.day, tzinfo=UTC)
