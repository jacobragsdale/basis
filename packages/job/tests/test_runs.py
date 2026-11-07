from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import polars as pl
from builders import DAY, make_job, params_file, prices

import basis.job as etl
from basis.core import snapshots
from basis.core.settings import GivenSettings
from basis.core.storage import Root
from basis.job import runs
from basis.job.job import Job


def clock() -> datetime:
    return datetime(2026, 11, 6, 6, 0, tzinfo=UTC)


def run_on_files(tmp_path: Path, job: Job, frame: pl.DataFrame, *, params: str = "{}", publish: bool = False) -> runs.Result:
    folder = tmp_path / "data"
    folder.mkdir(exist_ok=True)
    frame.write_parquet(folder / "prices.parquet")
    request = runs.Request(params_file(tmp_path, params), DAY, folder, {"purpose": "test"}, publish=publish)
    return runs.run(job, GivenSettings(), request, snapshot_root=Root.at(tmp_path / "snapshots"), clock=clock)


def test_a_run_on_files_checks_its_changes_and_saves_a_snapshot_that_holds_them(tmp_path: Path) -> None:
    result = run_on_files(tmp_path, make_job(write=etl.ReplaceWhere(interval="day")), prices({}, {"security_id": "S2"}))
    assert result.record.status == "checked"
    assert result.snapshot is not None
    saved = result.snapshot.changes()["prices"]
    assert saved.insert is not None
    assert saved.insert.equals(prices({}, {"security_id": "S2"}))
    assert saved.delete is not None
    assert saved.delete.within == etl.Range("day", DAY.start, DAY.end)
    assert result.snapshot.inputs()["prices"].equals(prices({}, {"security_id": "S2"}))


def test_a_transform_that_raises_names_the_line_of_job_code(tmp_path: Path) -> None:
    def broken(_inputs: object, _params: object) -> object:
        return {}["missing"]

    result = run_on_files(tmp_path, make_job(write=etl.Append(), transform=broken), prices({}))
    assert result.record.status == "failed"
    assert result.record.reasons == [f"the transform raised KeyError: 'missing', at tests/test_runs.py:{broken.__code__.co_firstlineno + 1} in broken"]


def test_the_jobs_own_check_stops_the_run_and_the_snapshot_keeps_its_inputs(tmp_path: Path) -> None:
    def check(_inputs: object, _outputs: object, _params: object) -> list[str]:
        return ["prices look stale"]

    result = run_on_files(tmp_path, make_job(write=etl.Append(), check=check), prices({}))
    assert result.record.reasons == ["check: prices look stale"]
    assert result.snapshot is not None
    assert result.snapshot.inputs()["prices"].height == 1


def test_a_run_that_publishes_to_an_unconfigured_database_fails_before_loading(tmp_path: Path) -> None:
    result = run_on_files(tmp_path, make_job(write=etl.Append()), prices({}), publish=True)
    assert result.record.status == "failed"
    assert result.record.inputs == {}
    assert result.record.reasons == ["database 'warehouse' is not configured, and the run publishes to it: set ETL_DATABASES__WAREHOUSE__*"]


def test_a_replay_with_other_params_reports_what_they_change(tmp_path: Path) -> None:
    job = make_job(write=etl.Upsert())
    original = run_on_files(tmp_path, job, prices({}))
    assert original.snapshot is not None
    replayed, diffs = runs.replay(job, original.snapshot, params=params_file(tmp_path / "data", '{"scale": 2}'))
    assert replayed.record.replay_of == original.record.run_id
    assert [(entry.operation, entry.changed, entry.columns) for entry in diffs] == [("upsert", 1, {"price": 1})]
    upsert = replayed.changes("prices")
    assert upsert is not None
    assert upsert.upsert is not None
    assert upsert.upsert.get_column("price").to_list() == [Decimal(200)]


def test_regress_replays_only_published_runs_and_lists_what_changed(tmp_path: Path) -> None:
    job = make_job(write=etl.Upsert())
    published = run_on_files(tmp_path, job, prices({}))
    assert published.snapshot is not None
    published.snapshot.save_record(snapshots.RunRecord.model_validate(published.record.model_dump() | {"status": "published"}))
    run_on_files(tmp_path, job, prices({}))  # checked, never published: regress leaves it out

    found = runs.regress(make_job(write=etl.Upsert(), factor=2), Root.at(tmp_path / "snapshots"), last=10, labels={"purpose": "test"})
    assert [(entry.run_id, [diff.columns for diff in entry.diffs]) for entry in found] == [(published.record.run_id, [{"price": 1}])]
