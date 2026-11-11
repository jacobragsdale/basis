"""The `average_price` column: declared on the positions' row as a float, and each position's notional over the
quantity traded, buys and sells alike, in the transform and in a run on the job's own fixture."""

from datetime import date, datetime
from pathlib import Path

import pytest
from basis.job import job as jobs
from basis.job.testing import check_fixture, frame, inputs

from jobs.positions import job
from jobs.positions.rows import Account, Trade

DAY = "2026-11-05..2026-11-06"
TRADE = {"trade_id": "T1", "account_id": "A1", "security_id": "S1", "trade_date": date(2026, 11, 5), "side": "BUY", "quantity": 100.0, "price": 101.0, "booked_at": datetime(2026, 11, 5, 9, 30)}
ACCOUNTS = frame(Account, {"account_id": "A1", "name": "Alpha", "closed": False})


def test_the_positions_row_declares_it() -> None:
    target = job.Outputs.declared()["positions"].target
    columns = {column.name: column for column in target.row.columns}
    assert "average_price" in columns
    assert columns["average_price"].kind == "float"


def test_it_weighs_each_trade_by_its_quantity_buys_and_sells_alike() -> None:
    trades = frame(Trade, TRADE, TRADE | {"trade_id": "T2", "side": "SELL", "quantity": 300.0, "price": 99.0}, TRADE | {"trade_id": "T3", "security_id": "S2", "price": 50.0})
    positions = job.transform(inputs(job.Inputs, interval=DAY, trades=trades, accounts=ACCOUNTS), job.Params()).positions
    by = dict(zip(positions["security_id"], positions["average_price"], strict=True))
    assert by["S1"] == pytest.approx((100 * 101.0 + 300 * 99.0) / 400)
    assert by["S2"] == pytest.approx(50.0)


def test_a_run_on_the_fixture_writes_it(tmp_path: Path) -> None:
    contract = jobs.read(job)
    result = check_fixture(contract, contract.directory / "tests/fixture", contract.directory / "params/default.json", tmp_path)
    changes = result.changes("positions")
    assert changes is not None
    assert changes.insert is not None
    written = changes.insert.set_index(["account_id", "security_id"])["average_price"]
    assert written.loc[("A1", "XS0001")] == pytest.approx((100 * 101.25 + 40 * 101.5) / 140)
