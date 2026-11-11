"""The `daily_cash` job ported from legacy/daily_cash.py, run by basis on fixtures of the test's own: it replaces the
interval's value dates with each account's net movement per currency, counts every movement but a cancelled one, reads
with a query that binds the interval, and refuses to write when a counted movement has a bad currency, as the script
did."""

import json
from datetime import UTC, date, datetime
from importlib.metadata import entry_points
from pathlib import Path

import basis.job as etl
import pandas as pd
import pytest
from basis.job import job as jobs
from basis.job.testing import ContractError, check_fixture

from jobs.daily_cash import job

HEADER = "movement_id,account_id,currency,amount,value_date,status"
MOVEMENTS = (
    "M1,A1,USD,1000.0000,2026-11-05,SETTLED",
    "M2,A1,USD,-250.5000,2026-11-05,PENDING",
    "M3,A1,EUR,300.0000,2026-11-05,SETTLED",
    "M4,A1,USD,999.0000,2026-11-05,CANCELLED",
    "M5,A2,USD,50.2500,2026-11-05,SETTLED",
)


def run(tmp_path: Path, *lines: str) -> etl.Changes:
    """The job run by basis for 2026-11-05 on a fixture of `lines`; ContractError if it refuses to write."""
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "movements.csv").write_text("\n".join([HEADER, *lines]) + "\n", encoding="utf-8")
    (fixture / "fixture.json").write_text(json.dumps({"interval": "2026-11-05..2026-11-06"}), encoding="utf-8")
    contract = jobs.read(job)
    result = check_fixture(contract, fixture, contract.directory / "params/default.json", tmp_path / "snapshots")
    changes = result.changes("daily_cash")
    assert changes is not None
    return changes


def written(changes: etl.Changes) -> list[tuple[object, ...]]:
    frames = [frame for frame in (changes.insert, changes.upsert) if frame is not None and not frame.empty]
    if not frames:
        return []
    rows = pd.concat(frames)[["value_date", "account_id", "currency", "net_amount", "movements"]]
    return sorted((pd.Timestamp(day).date(), account, currency, round(float(net), 4), int(count)) for day, account, currency, net, count in rows.itertuples(index=False))


def test_it_is_listed_as_a_job() -> None:
    listed = {point.name: point.value for point in entry_points(group="basis.jobs")}
    assert listed.get("daily_cash") == "jobs.daily_cash.job"


def test_the_script_is_gone() -> None:
    assert not (Path(job.__file__).parents[2] / "legacy" / "daily_cash.py").exists()


def test_it_reads_movements_from_ops_binding_only_the_interval() -> None:
    query = job.Inputs.declared()["movements"]
    assert isinstance(query, etl.Query)
    assert query.database == "ops"
    sql = query.text()
    assert "%(interval_start)s" in sql
    assert "%(interval_end)s" in sql
    assert "?" not in sql
    assert "CANCELLED" not in sql.upper()


def test_it_writes_daily_cash_in_the_warehouse() -> None:
    target = job.Outputs.declared()["daily_cash"].target
    assert (target.database, target.table.removeprefix("dbo.")) == ("warehouse", "daily_cash")
    assert set(target.row.names()) == {"value_date", "account_id", "currency", "net_amount", "movements"}


def test_a_run_replaces_the_interval_with_each_accounts_net_movements(tmp_path: Path) -> None:
    changes = run(tmp_path, *MOVEMENTS)
    assert written(changes) == [(date(2026, 11, 5), "A1", "EUR", 300.0, 1), (date(2026, 11, 5), "A1", "USD", 749.5, 2), (date(2026, 11, 5), "A2", "USD", 50.25, 1)]
    assert changes.delete is not None
    assert changes.delete.within == etl.Range("value_date", datetime(2026, 11, 5, tzinfo=UTC), datetime(2026, 11, 6, tzinfo=UTC))


def test_a_day_without_movements_still_replaces_the_day(tmp_path: Path) -> None:
    changes = run(tmp_path)
    assert written(changes) == []
    assert changes.delete is not None
    assert changes.delete.within is not None


def test_a_counted_movement_with_a_bad_currency_stops_the_run(tmp_path: Path) -> None:
    with pytest.raises(ContractError):
        run(tmp_path, *MOVEMENTS, "M6,A2,usd,10.0000,2026-11-05,PENDING")


def test_a_cancelled_movement_with_a_bad_currency_is_skipped_as_the_script_skips_it(tmp_path: Path) -> None:
    run(tmp_path, *MOVEMENTS, "M6,A2,usd,10.0000,2026-11-05,CANCELLED")
