"""The `account_totals` output: declared as asked, upserted by trade date and account, and each account's trades and
gross notional per day, counting the trades the positions count, in the transform and in a run on the fixture."""

from datetime import date, datetime
from pathlib import Path

import basis.job as etl
import pytest
from basis.job import job as jobs
from basis.job.testing import check_fixture, frame, inputs

from jobs.positions import job
from jobs.positions.rows import Account, Trade

DAY = "2026-11-05..2026-11-07"
TRADE = {"trade_id": "T1", "account_id": "A1", "security_id": "S1", "trade_date": date(2026, 11, 5), "side": "BUY", "quantity": 100.0, "price": 101.0, "booked_at": datetime(2026, 11, 5, 9, 30)}
ACCOUNTS = frame(Account, {"account_id": "A1", "name": "Alpha", "closed": False}, {"account_id": "A2", "name": "Shut", "closed": True})
TRADES = (
    TRADE,
    TRADE | {"trade_id": "T2", "security_id": "S2", "side": "SELL", "quantity": 10.0, "price": 50.0},
    TRADE | {"trade_id": "T3", "trade_date": date(2026, 11, 6), "quantity": 1.0},
    TRADE | {"trade_id": "T4", "account_id": "A2"},
)


def test_it_is_declared_as_asked() -> None:
    target = job.Outputs.declared()["account_totals"].target
    assert target.database == "warehouse"
    assert target.table.removeprefix("dbo.") == "account_daily_totals"
    assert isinstance(target.write, etl.Upsert)
    assert set(target.key or ()) == {"trade_date", "account_id"}
    assert set(target.row.names()) == {"trade_date", "account_id", "trades", "gross_notional"}


def test_it_totals_each_account_per_day_counting_what_the_positions_count() -> None:
    outputs = job.transform(inputs(job.Inputs, interval=DAY, trades=frame(Trade, *TRADES), accounts=ACCOUNTS), job.Params())
    totals = outputs.account_totals.sort_values(["trade_date", "account_id"])
    assert totals["account_id"].tolist() == ["A1", "A1"]
    assert [float(value) for value in totals["trades"]] == [2.0, 1.0]
    assert totals["gross_notional"].tolist() == pytest.approx([10100.0 + 500.0, 101.0])


def test_closed_accounts_count_when_the_params_say_so() -> None:
    outputs = job.transform(inputs(job.Inputs, interval=DAY, trades=frame(Trade, *TRADES), accounts=ACCOUNTS), job.Params(include_closed_accounts=True))
    assert sorted(set(outputs.account_totals["account_id"])) == ["A1", "A2"]


def test_a_run_on_the_fixture_upserts_them(tmp_path: Path) -> None:
    contract = jobs.read(job)
    result = check_fixture(contract, contract.directory / "tests/fixture", contract.directory / "params/default.json", tmp_path)
    changes = result.changes("account_totals")
    assert changes is not None
    assert changes.upsert is not None
    by = changes.upsert.set_index("account_id")
    assert float(by.loc["A1", "trades"]) == 3.0
    assert by.loc["A1", "gross_notional"] == pytest.approx(100 * 101.25 + 40 * 101.5 + 25.5 * 99.0)
