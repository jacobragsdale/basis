"""The integration tier's publish and resume scenarios (`integration/test_publish.py`), in every run, on basis-core's
fake database, which its contract suite holds to SQL Server's and Oracle's writers: every write mode reruns to the same
table, an append publishes an interval once, a failure in a database's transaction writes nothing there, and a run that
committed to one database and failed on another resumes from its snapshot."""

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, override

import pandas as pd
import pytest
from builders import Price, prices

import basis.job as etl
from basis.core import fakes, snapshots
from basis.core.databases import Counts, Writer
from basis.core.errors import Rejected, Unavailable
from basis.core.interval import Interval
from basis.core.settings import GivenSettings
from basis.core.storage import Root
from basis.job import runs
from basis.job.job import Job
from basis.job.outputs import FrameWrite, Target

KEY = ("day", "security_id")
TODAY = prices({"security_id": "S1"}, {"security_id": "S2", "price": 101.25, "note": "é"})
TABLE = "dbo.prices"


def price_job(write: FrameWrite, *, key: tuple[str, ...] | None = KEY, audit: bool = False) -> Job:
    """A job that copies its `prices` input to `dbo.prices`, and, with `audit`, appends it to `dbo.audit` too."""

    class Inputs(etl.Inputs):
        prices = etl.File(Price, path="prices.parquet", empty="fail")

    class One(etl.Outputs):
        prices = etl.Table(Price, database="warehouse", table=TABLE, write=write, key=key)

    class Two(etl.Outputs):
        prices = etl.Table(Price, database="warehouse", table=TABLE, write=write, key=key)
        audit = etl.Table(Price, database="warehouse", table="dbo.audit", write=etl.Append())

    def transform(inputs: Inputs, _params: etl.Params) -> etl.Outputs:
        return Two(prices=inputs.prices, audit=inputs.prices) if audit else One(prices=inputs.prices)

    return Job("prices", "tests.prices", etl.Params, Inputs, Two if audit else One, transform, None, Path(__file__).parent)


def two_databases() -> Job:
    """A job that appends its `prices` input to `dbo.prices` in two databases, alpha and beta, each committed on its own."""

    class Inputs(etl.Inputs):
        prices = etl.File(Price, path="prices.parquet", empty="fail")

    class Outputs(etl.Outputs):
        a = etl.Table(Price, database="alpha", table=TABLE, write=etl.Append())
        b = etl.Table(Price, database="beta", table=TABLE, write=etl.Append())

    def transform(inputs: Inputs, _params: etl.Params) -> Outputs:
        return Outputs(a=inputs.prices, b=inputs.prices)

    return Job("two_databases", "tests.two_databases", etl.Params, Inputs, Outputs, transform, None, Path(__file__).parent)


def database(name: str, *tables: tuple[str, tuple[str, ...] | None], ledger: str | None = "etl.publishes") -> fakes.Database:
    found = fakes.Database(name, ledger=ledger)
    for table, key in tables:
        found.create(Target(Price, name, table, etl.Append(), key))
    return found


def settings(*names: str) -> GivenSettings:
    """Settings that configure each database for a publish, a write login and a ledger, which the fakes stand in for."""
    return GivenSettings(
        databases={
            name: {
                "kind": "mssql", "connection_string": "Server=fake", "isolation": "read_committed", "timeout_s": 60, "max_rows": 1000,
                "write": {"connection_string": "Server=fake", "ledger": "etl.publishes"},
            }
            for name in names
        }
    )  # fmt: skip


def run(job: Job, found: Mapping[str, fakes.Database], frame: pd.DataFrame, root: Path, *, again: bool = False, interval: str = "2026-11-05..2026-11-06") -> runs.Result:  # noqa: PLR0913 -- a test's knobs, each by keyword
    folder = root / "data"
    folder.mkdir(exist_ok=True)
    frame.to_parquet(folder / "prices.parquet", index=False)
    params = root / "params.json"
    params.write_text("{}", encoding="utf-8")
    request = runs.Request(params, Interval.parse(interval), folder, publish=True, again=again)
    return runs.run(job, settings(*found), request, snapshot_root=Root.at(root / "snapshots"), databases=found.__getitem__)


def contents(found: fakes.Database, table: str = TABLE) -> list[tuple[object, ...]]:
    return sorted(((row["day"], row["security_id"], row["price"], row["note"]) for row in found.rows(table)), key=str)


@pytest.mark.parametrize("write", [etl.ReplaceWhere(interval="day"), etl.ReplaceWhere(matching=("security_id",)), etl.Replace(), etl.Upsert(), etl.InsertNew()], ids=lambda write: type(write).__name__)
def test_publishing_an_interval_twice_leaves_the_table_as_publishing_it_once(tmp_path: Path, write: FrameWrite) -> None:
    warehouse = database("warehouse", (TABLE, KEY))
    job = price_job(write)
    assert run(job, {"warehouse": warehouse}, TODAY, tmp_path).record.status == "published"
    once = contents(warehouse)
    assert run(job, {"warehouse": warehouse}, TODAY, tmp_path).record.status == "published"
    assert contents(warehouse) == once
    assert len(once) == 2
    assert len(warehouse.entries) == 2


def test_replacing_an_interval_leaves_every_other_days_rows(tmp_path: Path) -> None:
    warehouse = database("warehouse", (TABLE, KEY))
    job = price_job(etl.ReplaceWhere(interval="day"))
    run(job, {"warehouse": warehouse}, prices({"day": pd.Timestamp("2026-11-04").date()}), tmp_path, interval="2026-11-04..2026-11-05")
    run(job, {"warehouse": warehouse}, TODAY, tmp_path)
    run(job, {"warehouse": warehouse}, prices({"security_id": "S3"}), tmp_path)
    assert [(str(day), security) for day, security, _, _ in contents(warehouse)] == [("2026-11-04", "S1"), ("2026-11-05", "S3")]


def test_an_append_publishes_an_interval_once_unless_told_again(tmp_path: Path) -> None:
    warehouse = database("warehouse", (TABLE, None))
    job = price_job(etl.Append(), key=None)
    assert run(job, {"warehouse": warehouse}, TODAY, tmp_path).record.status == "published"
    refused = run(job, {"warehouse": warehouse}, TODAY, tmp_path)
    assert refused.record.status == "failed"
    assert "already published an interval that overlaps" in str(refused.record.publishes["warehouse"].error)
    assert len(contents(warehouse)) == 2
    assert run(job, {"warehouse": warehouse}, TODAY, tmp_path, again=True).record.status == "published"
    assert len(contents(warehouse)) == 4


def test_a_failure_anywhere_in_a_databases_transaction_writes_nothing_there(tmp_path: Path) -> None:
    warehouse = database("warehouse", (TABLE, KEY), ("dbo.audit", None))
    warehouse.fails(Rejected, on="commit")
    result = run(price_job(etl.Upsert(), audit=True), {"warehouse": warehouse}, TODAY, tmp_path)
    assert result.record.status == "failed"
    assert (contents(warehouse), contents(warehouse, "dbo.audit"), warehouse.entries) == ([], [], [])


def test_the_ledger_records_each_publish_with_what_the_database_counted(tmp_path: Path) -> None:
    warehouse = database("warehouse", (TABLE, KEY))
    result = run(price_job(etl.ReplaceWhere(interval="day")), {"warehouse": warehouse}, TODAY, tmp_path)
    assert result.record.publishes["warehouse"].counts == {"prices": {"deleted": 0, "upserted": 0, "inserted_new": 0, "inserted": 2}}
    assert [(entry.run_id, entry.job, entry.output, entry.table, entry.counts.inserted, entry.rerun_safe) for entry in warehouse.entries] == [
        (result.record.run_id, "prices", "prices", TABLE, 2, True)
    ]


def test_a_run_that_committed_to_one_database_resumes_on_the_other(tmp_path: Path) -> None:
    alpha, beta = database("alpha", (TABLE, None)), database("beta", (TABLE, None))
    beta.fails(Rejected, on="apply", times=1)  # beta's publish fails after alpha's commits
    job = two_databases()
    partial = run(job, {"alpha": alpha, "beta": beta}, TODAY, tmp_path)
    assert partial.record.status == "partial"
    assert [record.status for record in partial.record.publishes.values()] == ["committed", "failed"]
    assert partial.snapshot is not None
    resumed = runs.resume(job, partial.snapshot, settings("alpha", "beta"), databases={"alpha": alpha, "beta": beta}.__getitem__)
    assert resumed.status == "published"
    assert (len(contents(alpha)), len(contents(beta))) == (2, 2)
    assert snapshots.find(partial.snapshot.store.root, partial.record.run_id).record().status == "published"


def test_a_resume_of_a_run_told_again_inserts_again_on_the_database_it_failed_on(tmp_path: Path) -> None:
    alpha, beta = database("alpha", (TABLE, None)), database("beta", (TABLE, None))
    job = two_databases()
    assert run(job, {"alpha": alpha, "beta": beta}, TODAY, tmp_path).record.status == "published"
    beta.fails(Rejected, on="apply", times=1)
    partial = run(job, {"alpha": alpha, "beta": beta}, TODAY, tmp_path, again=True)
    assert (partial.record.status, partial.record.again) == ("partial", True)
    assert partial.snapshot is not None
    assert runs.resume(job, partial.snapshot, settings("alpha", "beta"), databases={"alpha": alpha, "beta": beta}.__getitem__).status == "published"
    assert (len(contents(alpha)), len(contents(beta))) == (4, 4)


class Killed(BaseException):
    """The process stopped, as a kill or a lost pod stops it: nothing after it runs, no handler records it."""


class Dying(fakes.Database):
    """A fake database whose writer the process dies in: at its first statement (`before`), or once it commits
    (`after`)."""

    def __init__(self, name: str, *, dies: Literal["before", "after"] | None = None) -> None:
        super().__init__(name)
        self.dies = dies

    @override
    @contextmanager
    def writing(self) -> Generator[Writer]:
        if self.dies == "before":
            raise Killed
        with super().writing() as writer:
            yield writer
        if self.dies == "after" and self.entries:
            raise Killed


@pytest.mark.parametrize(("alpha", "beta"), [("before", None), ("after", None), (None, "after")], ids=["before-alpha", "after-alpha", "after-beta"])
def test_a_run_killed_at_any_point_of_its_publish_resumes_with_each_row_written_once(tmp_path: Path, alpha: Literal["before", "after"] | None, beta: Literal["after"] | None) -> None:
    databases = {"alpha": Dying("alpha", dies=alpha), "beta": Dying("beta", dies=beta)}
    for found in databases.values():
        found.create(Target(Price, found.name, TABLE, etl.Append(), None))
    job = two_databases()
    with pytest.raises(Killed):
        run(job, databases, TODAY, tmp_path)
    [saved] = snapshots.runs_of(Root.at(tmp_path / "snapshots"), job.name, package="job")
    record = saved.record()
    assert (record.status, record.publish, record.publishes) == ("publishing", True, {})
    for found in databases.values():
        found.dies = None
    resumed = runs.resume(job, saved, settings("alpha", "beta"), databases=databases.__getitem__)
    assert resumed.status == "published"
    assert (len(contents(databases["alpha"])), len(contents(databases["beta"]))) == (2, 2)
    assert {name: entry.counts for name, entry in resumed.publishes.items()} == {name: {name[0]: vars(Counts(inserted=2))} for name in ("alpha", "beta")}


def test_a_resume_keeps_the_record_of_a_database_that_committed(tmp_path: Path) -> None:
    alpha, beta = database("alpha", (TABLE, None)), database("beta", (TABLE, None))
    beta.fails(Rejected, on="apply", times=1)
    job = two_databases()
    partial = run(job, {"alpha": alpha, "beta": beta}, TODAY, tmp_path)
    committed = partial.record.publishes["alpha"]
    assert partial.snapshot is not None
    resumed = runs.resume(job, partial.snapshot, settings("alpha", "beta"), databases={"alpha": alpha, "beta": beta}.__getitem__)
    assert resumed.publishes["alpha"] == committed


def test_a_run_not_asked_to_publish_is_never_resumed(tmp_path: Path) -> None:
    warehouse = database("warehouse", (TABLE, KEY))
    folder = tmp_path / "data"
    folder.mkdir()
    TODAY.to_parquet(folder / "prices.parquet", index=False)
    (tmp_path / "params.json").write_text("{}", encoding="utf-8")
    request = runs.Request(tmp_path / "params.json", Interval.parse("2026-11-05..2026-11-06"), folder, publish=False)
    job = price_job(etl.Upsert())
    checked = runs.run(job, settings("warehouse"), request, snapshot_root=Root.at(tmp_path / "snapshots"), databases={"warehouse": warehouse}.__getitem__)
    assert (checked.record.status, checked.record.publish) == ("checked", False)
    assert checked.snapshot is not None
    with pytest.raises(ValueError, match="was not asked to publish: run it again with --publish"):
        runs.resume(job, checked.snapshot, settings("warehouse"), databases={"warehouse": warehouse}.__getitem__)
    assert (contents(warehouse), warehouse.entries) == ([], [])


def test_a_missing_ledger_stops_the_run_before_it_loads_and_no_database_commits(tmp_path: Path) -> None:
    alpha, beta = database("alpha", (TABLE, None)), database("beta", (TABLE, None), ledger=None)
    result = run(two_databases(), {"alpha": alpha, "beta": beta}, TODAY, tmp_path)
    assert result.record.status == "failed"
    assert result.record.reasons == ["database 'beta' has no ledger table etl.publishes, which a publish writes in its transaction: `basis ddl --ledger etl.publishes --dialect mssql` prints it"]
    assert (contents(alpha), alpha.entries, result.record.inputs) == ([], [], {})


def test_a_destination_that_cannot_be_reached_in_the_preflight_is_a_failed_run_with_a_record(tmp_path: Path) -> None:
    warehouse = database("warehouse", (TABLE, KEY))
    warehouse.fails(Unavailable, on="describe")
    result = run(price_job(etl.Upsert()), {"warehouse": warehouse}, TODAY, tmp_path)
    assert result.record.status == "failed"
    assert result.record.error is not None
    assert result.record.error.startswith("Unavailable: database warehouse could not be reached")
    assert result.snapshot is not None


def test_an_append_in_a_run_with_no_interval_publishes_once_unless_told_again(tmp_path: Path) -> None:
    warehouse = database("warehouse", (TABLE, None))
    job = price_job(etl.Append(), key=None)
    folder = tmp_path / "data"
    folder.mkdir()
    TODAY.to_parquet(folder / "prices.parquet", index=False)
    (tmp_path / "params.json").write_text("{}", encoding="utf-8")

    def plain(*, again: bool = False) -> runs.Result:
        request = runs.Request(tmp_path / "params.json", None, folder, publish=True, again=again)
        return runs.run(job, settings("warehouse"), request, snapshot_root=Root.at(tmp_path / "snapshots"), databases={"warehouse": warehouse}.__getitem__)

    assert plain().record.status == "published"
    refused = plain()
    assert refused.record.status == "failed"
    assert "already published it, and this run has no interval to tell theirs apart" in str(refused.record.publishes["warehouse"].error)
    assert len(contents(warehouse)) == 2
    assert plain(again=True).record.status == "published"
    assert len(contents(warehouse)) == 4
