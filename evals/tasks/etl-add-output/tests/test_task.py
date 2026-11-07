"""The positions job's `account_totals` output: upserted by trade date and account into `account_daily_totals`, with
each account's trades and gross notional per day, from the trades the positions count."""

import subprocess
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import etl_framework as etl
from etl_framework.testing import frame, inputs
from jobs.positions import job
from jobs.positions.rows import Account, Trade

DAYS = "2026-11-05..2026-11-07"
TRADE = {
    "trade_id": "T1",
    "account_id": "A1",
    "security_id": "XS0001",
    "trade_date": date(2026, 11, 5),
    "side": "BUY",
    "quantity": 100,
    "price": Decimal("101.25"),
    "booked_at": datetime(2026, 11, 5, 9, 30),
}
TRADES = frame(
    Trade,
    TRADE,
    TRADE | {"trade_id": "T2", "side": "SELL", "quantity": 40, "price": Decimal("101.5")},
    TRADE | {"trade_id": "T3", "security_id": "XS0002", "quantity": Decimal("25.5"), "price": 99},
    TRADE | {"trade_id": "T4", "trade_date": date(2026, 11, 6), "quantity": 10, "price": 100},
    TRADE | {"trade_id": "T5", "account_id": "A3", "quantity": 1, "price": Decimal("10.25")},
)
ACCOUNTS = frame(Account, {"account_id": "A1", "name": "Alpha", "closed": False}, {"account_id": "A3", "name": "Closed", "closed": True})


def totals(params: job.Params) -> list[tuple[object, ...]]:
    outputs = job.transform(inputs(job.Inputs, interval=DAYS, trades=TRADES, accounts=ACCOUNTS), params)
    return sorted(outputs.values["account_totals"].select("trade_date", "account_id", "trades", "gross_notional").rows())


def test_it_upserts_account_daily_totals_by_trade_date_and_account() -> None:
    target = job.Outputs.declared()["account_totals"].target
    assert (target.database, target.table) == ("warehouse", "account_daily_totals")
    assert isinstance(target.write, etl.Upsert)
    assert set(target.key or ()) == {"trade_date", "account_id"}


def test_its_columns_are_the_ones_asked_for() -> None:
    row = job.Outputs.declared()["account_totals"].target.row
    assert set(row.names()) == {"trade_date", "account_id", "trades", "gross_notional"}
    assert row.column("gross_notional").kind == "decimal"
    assert row.column("gross_notional").scale == 2


def test_each_day_counts_the_accounts_trades_and_gross_notional() -> None:
    assert totals(job.Params()) == [(date(2026, 11, 5), "A1", 3, Decimal("16709.50")), (date(2026, 11, 6), "A1", 1, Decimal("1000.00"))]


def test_closed_accounts_count_only_when_the_positions_count_them() -> None:
    assert (date(2026, 11, 5), "A3", 1, Decimal("10.25")) in totals(job.Params(include_closed_accounts=True))


def test_the_other_outputs_are_unchanged() -> None:
    declared = job.Outputs.declared()
    assert isinstance(declared["positions"].target.write, etl.ReplaceWhere)
    assert isinstance(declared["activity"].target.write, etl.Upsert)


def test_etl_ddl_prints_its_table() -> None:
    ddl = subprocess.run([Path(sys.executable).parent / "etl", "ddl", "positions", "--dialect", "mssql"], capture_output=True, text=True, check=True)
    assert "account_daily_totals" in ddl.stdout
