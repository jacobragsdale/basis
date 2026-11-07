from datetime import UTC, date, datetime
from decimal import Decimal

import polars as pl
from basis.job.testing import frame, inputs

from basis.job.example import job
from basis.job.example.rows import Account, Trade

DAY = "2026-11-05..2026-11-06"
TRADE = {"trade_id": "T1", "account_id": "A1", "security_id": "S1", "trade_date": date(2026, 11, 5), "side": "BUY", "quantity": 100, "price": 101, "booked_at": datetime(2026, 11, 5, 9, 30)}
ACCOUNTS = frame(Account, {"account_id": "A1", "name": "Alpha", "closed": False}, {"account_id": "A2", "name": "Closed", "closed": True})


def test_a_sale_nets_against_a_purchase() -> None:
    trades = frame(Trade, TRADE, TRADE | {"trade_id": "T2", "side": "SELL", "quantity": 40, "price": Decimal("101.5")})
    outputs = job.transform(inputs(job.Inputs, interval=DAY, trades=trades, accounts=ACCOUNTS), job.Params())
    position = outputs.positions.row(0, named=True)
    assert position["net_quantity"] == Decimal(60)
    assert position["gross_notional"] == Decimal("14160.00")
    assert position["trades"] == 2


def test_closed_accounts_count_only_when_the_params_say_so() -> None:
    trades = frame(Trade, TRADE | {"account_id": "A2"})
    excluded = job.transform(inputs(job.Inputs, interval=DAY, trades=trades, accounts=ACCOUNTS), job.Params())
    included = job.transform(inputs(job.Inputs, interval=DAY, trades=trades, accounts=ACCOUNTS), job.Params(include_closed_accounts=True))
    assert excluded.positions.height == 0
    assert included.positions.get_column("account_id").to_list() == ["A2"]


def test_activity_is_the_latest_booking_in_utc() -> None:
    trades = frame(Trade, TRADE, TRADE | {"trade_id": "T2", "booked_at": datetime(2026, 11, 5, 15, 0)})
    outputs = job.transform(inputs(job.Inputs, interval=DAY, trades=trades, accounts=ACCOUNTS), job.Params())
    assert outputs.activity.get_column("last_booked_at").to_list() == [datetime(2026, 11, 5, 20, 0, tzinfo=UTC)]


def test_the_check_names_trades_in_unlisted_accounts() -> None:
    given = inputs(job.Inputs, interval=DAY, trades=frame(Trade, TRADE | {"account_id": "A9"}), accounts=ACCOUNTS)
    reasons = job.check(given, job.transform(given, job.Params()), job.Params())
    assert reasons == ["1 trades are in accounts the accounts table does not list"]


def test_no_trades_means_no_positions() -> None:
    outputs = job.transform(inputs(job.Inputs, interval=DAY, accounts=ACCOUNTS), job.Params())
    assert outputs.positions.height == 0
    assert isinstance(outputs.activity, pl.DataFrame)
