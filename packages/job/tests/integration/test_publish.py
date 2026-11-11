"""Publishing against real databases: every write mode reruns to the same table, an append publishes an interval
once, a failure anywhere in a database's transaction writes nothing there, a drifted table stops a run before it
loads, and a run that committed to one database and failed on another resumes from its snapshot."""

import dataclasses
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated

import pytest
from builders import Price, prices
from integration.scratch import Scratch, configured, oracle, run

import basis.job as etl
from basis.core import command, environments, snapshots
from basis.core.settings import GivenSettings
from basis.core.storage import Root
from basis.job import cli, ddl, runs
from basis.job.job import Job
from basis.job.outputs import FrameWrite, Target

pytestmark = pytest.mark.integration

KEY = ("day", "security_id")


def price_job(table: str, write: FrameWrite, *, database: str = "warehouse", key: tuple[str, ...] | None = KEY, second: str | None = None) -> Job:
    """A job that copies its `prices` input to `table`, and, given `second`, to that table too, in the same database."""

    class Inputs(etl.Inputs):
        prices = etl.File(Price, path="prices.parquet", empty="fail")

    class One(etl.Outputs):
        prices = etl.Table(Price, database=database, table=table, write=write, key=key)

    class Two(etl.Outputs):
        prices = etl.Table(Price, database=database, table=table, write=write, key=key)
        audit = etl.Table(Price, database=database, table=second or "unused", write=etl.Append())

    def transform(inputs: Inputs, _params: etl.Params) -> etl.Outputs:
        return Two(prices=inputs.prices, audit=inputs.prices) if second is not None else One(prices=inputs.prices)

    return Job("prices_it", "tests.prices_it", etl.Params, Inputs, Two if second is not None else One, transform, None, Path(__file__).parent)


def create(scratch: Scratch, table: str, key: tuple[str, ...] | None = KEY) -> None:
    scratch.execute(ddl.create_table(Target(Price, "warehouse", table, etl.Append(), key), scratch.dialect))


def contents(scratch: Scratch, table: str) -> list[tuple[object, ...]]:
    return sorted(scratch.rows(f"SELECT day, security_id, price, note FROM {table}"), key=str)


TODAY = prices({"security_id": "S1"}, {"security_id": "S2", "price": 101.25, "note": "é"})


@pytest.mark.parametrize("write", [etl.ReplaceWhere(interval="day"), etl.ReplaceWhere(matching=("security_id",)), etl.Replace(), etl.Upsert(), etl.InsertNew()], ids=lambda write: type(write).__name__)
def test_publishing_an_interval_twice_leaves_the_table_as_publishing_it_once(scratch: Scratch, tmp_path: Path, write: FrameWrite) -> None:
    table = f"prices_{scratch.suffix}"
    create(scratch, table)
    job = price_job(table, write)
    first = run(job, {"warehouse": scratch}, TODAY, tmp_path)
    assert first.record.status == "published", first.record
    once = contents(scratch, table)
    second = run(job, {"warehouse": scratch}, TODAY, tmp_path)
    assert second.record.status == "published", second.record
    assert contents(scratch, table) == once
    assert len(once) == 2
    assert scratch.rows(f"SELECT COUNT(*) FROM {scratch.ledger()}") == [(2,)]


def test_a_publish_to_a_write_login_without_a_ledger_names_the_variable_to_set(scratch: Scratch, tmp_path: Path) -> None:
    table = f"prices_{scratch.suffix}"
    create(scratch, table)
    write = scratch.settings["write"]
    assert isinstance(write, dict)
    unledgered = dataclasses.replace(scratch, settings=scratch.settings | {"write": {key: value for key, value in write.items() if key != "ledger"}})
    result = run(price_job(table, etl.Append()), {"warehouse": unledgered}, TODAY, tmp_path)
    assert (result.record.status, result.record.reasons) == (
        "failed",
        ["database 'warehouse' has a write login but no ledger, which a publish writes in its transaction, and the run publishes to it: set BASIS_DATABASES__WAREHOUSE__WRITE__LEDGER"],
    )
    assert contents(scratch, table) == []


def test_replacing_an_interval_leaves_every_other_days_rows(scratch: Scratch, tmp_path: Path) -> None:
    table = f"prices_{scratch.suffix}"
    create(scratch, table)
    job = price_job(table, etl.ReplaceWhere(interval="day"))
    run(job, {"warehouse": scratch}, prices({"day": date(2026, 11, 4)}), tmp_path, interval="2026-11-04..2026-11-05")
    run(job, {"warehouse": scratch}, TODAY, tmp_path)
    run(job, {"warehouse": scratch}, prices({"security_id": "S3"}), tmp_path)
    found = [(str(day)[:10], security) for day, security, _, _ in contents(scratch, table)]
    assert found == [("2026-11-04", "S1"), ("2026-11-05", "S3")]


def test_an_append_publishes_an_interval_once_unless_told_again(scratch: Scratch, tmp_path: Path) -> None:
    table = f"prices_{scratch.suffix}"
    create(scratch, table, key=None)
    job = price_job(table, etl.Append(), key=None)
    assert run(job, {"warehouse": scratch}, TODAY, tmp_path).record.status == "published"
    refused = run(job, {"warehouse": scratch}, TODAY, tmp_path)
    assert refused.record.status == "failed"
    assert "already published an interval that overlaps" in str(refused.record.publishes["warehouse"].error)
    assert len(contents(scratch, table)) == 2
    assert run(job, {"warehouse": scratch}, TODAY, tmp_path, again=True).record.status == "published"
    assert len(contents(scratch, table)) == 4


def test_a_failure_anywhere_in_a_databases_transaction_writes_nothing_there(scratch: Scratch, tmp_path: Path) -> None:
    table, audit = f"prices_{scratch.suffix}", f"audit_{scratch.suffix}"
    create(scratch, table)
    create(scratch, audit, key=None)
    scratch.execute(f"ALTER TABLE {audit} ADD CONSTRAINT ck_{scratch.suffix} CHECK (price > 1000)")
    result = run(price_job(table, etl.Upsert(), second=audit), {"warehouse": scratch}, TODAY, tmp_path)
    assert result.record.status == "failed"
    assert contents(scratch, table) == []
    assert scratch.rows(f"SELECT COUNT(*) FROM {scratch.ledger()}") == [(0,)]


def test_a_table_that_has_drifted_from_its_row_stops_the_run_before_it_loads(scratch: Scratch, tmp_path: Path) -> None:
    table = f"prices_{scratch.suffix}"
    timestamp = "DATETIME2" if scratch.dialect == "mssql" else "TIMESTAMP"
    scratch.execute(f"CREATE TABLE {table} (day DATE NOT NULL, security_id VARCHAR(8) NOT NULL, price VARCHAR(20) NOT NULL, booked_at {timestamp})")
    result = run(price_job(table, etl.Upsert()), {"warehouse": scratch}, TODAY, tmp_path)
    assert result.record.status == "failed"
    assert result.record.inputs == {}
    text = "VARCHAR" if scratch.dialect == "mssql" else "VARCHAR2"
    assert sorted(result.record.reasons) == [f"prices: note: {table} has no such column", f"prices: price: a {text} column cannot hold a row's float values exactly"]


def test_an_oracle_timestamp_with_local_time_zone_is_refused_since_its_instants_follow_the_session(tmp_path: Path) -> None:
    scratch = oracle(configured(), uuid.uuid4().hex[:8]).with_ledger()
    table = f"prices_{scratch.suffix}"
    try:
        scratch.execute(
            f"CREATE TABLE {table} (day DATE NOT NULL, security_id VARCHAR2(8 CHAR) NOT NULL, price BINARY_DOUBLE NOT NULL, note VARCHAR2(40 CHAR), booked_at TIMESTAMP WITH LOCAL TIME ZONE)"
        )
        result = run(price_job(table, etl.Upsert()), {"warehouse": scratch}, TODAY, tmp_path)
        assert result.record.status == "failed"
        assert result.record.reasons == ["prices: booked_at: a TIMESTAMP WITH LOCAL TIME ZONE column cannot hold a row's datetime values exactly"]
    finally:
        scratch.drop(scratch.ledger(), table)


def test_a_value_that_does_not_fit_its_column_is_refused_before_any_write(scratch: Scratch, tmp_path: Path) -> None:
    table = f"prices_{scratch.suffix}"

    class NarrowNote(Price):
        note: Annotated[str, etl.MaxLength(2)] | None  # the table's narrower column, for its DDL

    scratch.execute(ddl.create_table(Target(NarrowNote, "warehouse", table, etl.Upsert(), KEY), scratch.dialect))
    result = run(price_job(table, etl.Upsert()), {"warehouse": scratch}, prices({"note": "too long"}), tmp_path)
    unit = "UTF-16 units" if scratch.dialect == "mssql" else "characters"  # an NVARCHAR(2), and a VARCHAR2(2 CHAR)
    assert result.record.reasons == [f"prices: upsert: note: values up to 8 {unit}, and the column holds 2"]
    assert contents(scratch, table) == []


def test_a_datetime_is_stored_naive_in_its_columns_zone(scratch: Scratch, tmp_path: Path) -> None:
    table = f"prices_{scratch.suffix}"
    create(scratch, table)
    run(price_job(table, etl.Upsert()), {"warehouse": scratch}, prices({"booked_at": datetime(2026, 11, 5, 9, 30)}), tmp_path)
    assert scratch.rows(f"SELECT booked_at FROM {table}") == [(datetime(2026, 11, 5, 9, 30),)]
    assert prices({"booked_at": datetime(2026, 11, 5, 9, 30)})["booked_at"].tolist() == [datetime(2026, 11, 5, 14, 30, tzinfo=UTC)]


def two_databases(table: str) -> Job:
    """A job that appends its `prices` input to `table` in two databases, alpha and beta, each committed on its own."""

    class Inputs(etl.Inputs):
        prices = etl.File(Price, path="prices.parquet", empty="fail")

    class Outputs(etl.Outputs):
        a = etl.Table(Price, database="alpha", table=table, write=etl.Append())
        b = etl.Table(Price, database="beta", table=table, write=etl.Append())

    def transform(inputs: Inputs, _params: etl.Params) -> Outputs:
        return Outputs(a=inputs.prices, b=inputs.prices)

    return Job("two_databases", "tests.two_databases", etl.Params, Inputs, Outputs, transform, None, Path(__file__).parent)


def test_a_run_that_committed_to_one_database_resumes_on_the_other(both: tuple[Scratch, Scratch], tmp_path: Path) -> None:
    alpha, beta = both
    table = f"prices_{alpha.suffix}"
    create(alpha, table, key=None)
    create(beta, table, key=None)
    alpha.with_ledger()
    beta.with_ledger()
    refuse(beta, table)  # so its publish fails after alpha's commits

    job = two_databases(table)
    partial = run(job, {"alpha": alpha, "beta": beta}, TODAY, tmp_path)
    assert partial.record.status == "partial"
    assert [record.status for record in partial.record.publishes.values()] == ["committed", "failed"]
    allow(beta, table)
    assert partial.snapshot is not None
    settings = GivenSettings(databases={"alpha": alpha.settings, "beta": beta.settings})
    resumed = runs.resume(job, partial.snapshot, settings)
    assert resumed.status == "published"
    assert len(contents(alpha, table)) == 2
    assert len(contents(beta, table)) == 2
    assert snapshots.find(partial.snapshot.store.root, partial.record.run_id).record().status == "published"


def test_the_ledger_records_each_publish_with_what_the_database_counted(scratch: Scratch, tmp_path: Path) -> None:
    table = f"prices_{scratch.suffix}"
    create(scratch, table)
    result = run(price_job(table, etl.ReplaceWhere(interval="day")), {"warehouse": scratch}, TODAY, tmp_path)
    assert result.record.publishes["warehouse"].counts == {"prices": {"deleted": 0, "upserted": 0, "inserted_new": 0, "inserted": 2}}
    ledger = scratch.rows(f"SELECT run_id, job, output, table_name, inserted, rerun_safe FROM {scratch.ledger()}")
    assert [(row[0], row[1], row[2], row[3], row[4], bool(row[5])) for row in ledger] == [(result.record.run_id, "prices_it", "prices", table, 2, True)]


def test_frames_shared_by_both_dialects_round_trip_exactly() -> None:
    assert TODAY.copy().equals(TODAY)


def test_a_run_told_again_that_committed_to_one_database_resumes_again_on_the_other(
    both: tuple[Scratch, Scratch], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both ledgers hold the interval, from the run before; the run told `--again` commits to alpha, and fails on beta,
    whose table refuses its rows. `basis resume` inserts beta's rows again, as the run was told to."""
    alpha, beta = both
    table = f"prices_{alpha.suffix}"
    create(alpha, table, key=None)
    create(beta, table, key=None)
    alpha.with_ledger()
    beta.with_ledger()
    job = two_databases(table)
    assert run(job, {"alpha": alpha, "beta": beta}, TODAY, tmp_path).record.status == "published"
    refuse(beta, table)
    partial = run(job, {"alpha": alpha, "beta": beta}, TODAY, tmp_path, again=True)
    assert (partial.record.status, partial.record.again) == ("partial", True)
    allow(beta, table)
    settings = GivenSettings(databases={"alpha": alpha.settings, "beta": beta.settings}, snapshot_root=str(tmp_path / "snapshots"))
    monkeypatch.setattr(environments, "load", lambda _given, _kind: environments.Environment("dev", settings, None, writes=True))  # the containers' databases, as dev's settings
    monkeypatch.setattr("basis.job.job.load", lambda _: job)  # the test's own job, which no module holds
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "repo"\n\n[tool.basis]\nparts = ["job"]\n', encoding="utf-8")

    assert command.run(["resume", partial.record.run_id], cwd=repo, installed={"job": cli.part}) == 0, capsys.readouterr().err
    assert (len(contents(alpha, table)), len(contents(beta, table))) == (4, 4)


def test_a_resume_told_again_is_saved_so_its_next_attempt_is_too(both: tuple[Scratch, Scratch], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The run is not told `--again`. Beta's ledger holds the interval, from the run before, and alpha's, a fresh one,
    does not: the run commits to alpha and beta's interval guard refuses it. A plain resume is refused by the same
    guard, and its fix says to resume with `--again`, which publishes, and is saved."""
    alpha, beta = both
    table, fresh = f"prices_{alpha.suffix}", f"fresh_ledger_{alpha.suffix}"
    create(alpha, table, key=None)
    create(beta, table, key=None)
    alpha.with_ledger()
    beta.with_ledger()
    job = two_databases(table)
    try:
        assert run(job, {"alpha": alpha, "beta": beta}, TODAY, tmp_path).record.status == "published"
        alpha.execute(ddl.ledger(fresh, alpha.dialect))
        partial = run(job, {"alpha": ledgered(alpha, fresh), "beta": beta}, TODAY, tmp_path)
        assert (partial.record.status, partial.record.again) == ("partial", False)
        settings = GivenSettings(databases={"alpha": ledgered(alpha, fresh).settings, "beta": beta.settings}, snapshot_root=str(tmp_path / "snapshots"))
        monkeypatch.setattr(environments, "load", lambda _given, _kind: environments.Environment("dev", settings, None, writes=True))  # the containers' databases, as dev's settings
        monkeypatch.setattr("basis.job.job.load", lambda _: job)  # the test's own job, which no module holds
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "pyproject.toml").write_text('[project]\nname = "repo"\n\n[tool.basis]\nparts = ["job"]\n', encoding="utf-8")
        run_id = partial.record.run_id

        assert command.run(["resume", run_id], cwd=repo, installed={"job": cli.part}) == 1
        assert capsys.readouterr().err.splitlines()[-1].endswith(f"fix what its publishes record, then `basis resume two_databases {run_id} --again` again")
        assert command.run(["resume", run_id, "--again"], cwd=repo, installed={"job": cli.part}) == 0, capsys.readouterr().err
        assert snapshots.find(Root.at(tmp_path / "snapshots"), run_id).record().again
        assert (len(contents(alpha, table)), len(contents(beta, table))) == (4, 4)
    finally:
        alpha.drop(fresh)


def ledgered(scratch: Scratch, ledger: str) -> Scratch:
    """`scratch` with its write login's ledger named `ledger`."""
    write = scratch.settings["write"]
    assert isinstance(write, dict)
    return dataclasses.replace(scratch, settings=scratch.settings | {"write": write | {"ledger": ledger}})


def refuse(scratch: Scratch, table: str) -> None:
    """Make `table` refuse every row the tests write, whose prices are all under 1,000, leaving the rows it has."""
    if scratch.dialect == "mssql":
        scratch.execute(f"ALTER TABLE {table} WITH NOCHECK ADD CONSTRAINT ck_{scratch.suffix} CHECK (price > 1000)")
    else:
        scratch.execute(f"ALTER TABLE {table} ADD CONSTRAINT ck_{scratch.suffix} CHECK (price > 1000) ENABLE NOVALIDATE")


def allow(scratch: Scratch, table: str) -> None:
    scratch.execute(f"ALTER TABLE {table} DROP CONSTRAINT ck_{scratch.suffix}")
