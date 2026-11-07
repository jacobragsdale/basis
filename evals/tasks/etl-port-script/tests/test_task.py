"""The `daily_cash` job ported from /legacy/daily_cash.py, run by the framework on fixtures of its own: it replaces the
interval's value dates with each account's net movement per currency, counts every movement but a cancelled one, reads
with a query that binds the interval, and refuses to write when a counted movement has a bad currency, as the script
did."""

import importlib.util
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from importlib.metadata import entry_points
from pathlib import Path

import etl_framework as etl
import pytest
from etl_framework import job as jobs
from etl_framework.testing import ContractError, ContractTests, check_fixture
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
    """The job run by the framework for 2026-11-05 on a fixture of `lines`; ContractError if it refuses to write."""
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
    assert changes.insert is not None
    return sorted(changes.insert.select("value_date", "account_id", "currency", "net_amount", "movements").rows())


def test_it_is_listed_as_a_job() -> None:
    listed = {point.name: point.value for point in entry_points(group="etl_framework.jobs")}
    assert listed.get("daily_cash") == "jobs.daily_cash.job"


def test_it_reads_movements_from_ops_binding_only_the_interval() -> None:
    query = job.Inputs.declared()["movements"]
    assert isinstance(query, etl.Query)
    assert query.database == "ops"
    sql = (query.base / query.sql).read_text(encoding="utf-8")
    assert "%(interval_start)s" in sql
    assert "%(interval_end)s" in sql
    assert "?" not in sql


def test_it_writes_daily_cash_in_the_warehouse() -> None:
    target = job.Outputs.declared()["daily_cash"].target
    assert (target.database, target.table.removeprefix("dbo.")) == ("warehouse", "daily_cash")
    assert set(target.row.names()) == {"value_date", "account_id", "currency", "net_amount", "movements"}


def test_it_has_contract_tests() -> None:
    found = []
    for path in (Path(job.__file__).parent / "tests").glob("test_*.py"):
        spec = importlib.util.spec_from_file_location(f"eval_{path.stem}", path)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        found += [value for value in vars(module).values() if isinstance(value, type) and issubclass(value, ContractTests) and getattr(value, "job", None) is job]
    assert found


def test_a_run_replaces_the_interval_with_each_accounts_net_movements(tmp_path: Path) -> None:
    changes = run(tmp_path, *MOVEMENTS)
    assert written(changes) == [(date(2026, 11, 5), "A1", "EUR", Decimal(300), 1), (date(2026, 11, 5), "A1", "USD", Decimal("749.5"), 2), (date(2026, 11, 5), "A2", "USD", Decimal("50.25"), 1)]
    assert changes.delete is not None
    assert changes.delete.within == etl.Range("value_date", datetime(2026, 11, 5, tzinfo=UTC), datetime(2026, 11, 6, tzinfo=UTC))
    assert changes.delete.matching is None
    assert changes.upsert is None or changes.upsert.is_empty()
    assert changes.insert_new is None or changes.insert_new.is_empty()


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
