from datetime import UTC, date, datetime

import pandas as pd
from basis.job.testing import frame, inputs

from basis.job.example import job
from basis.job.example.rows import Account, Trade

DAY = "2026-11-05..2026-11-06"
TRADE = {"trade_id": "T1", "account_id": "A1", "security_id": "S1", "trade_date": date(2026, 11, 5), "side": "BUY", "quantity": 100.0, "price": 101.0, "booked_at": datetime(2026, 11, 5, 9, 30)}
ACCOUNTS = frame(Account, {"account_id": "A1", "name": "Alpha", "closed": False}, {"account_id": "A2", "name": "Closed", "closed": True})


def test_a_sale_nets_against_a_purchase() -> None:
    trades = frame(Trade, TRADE, TRADE | {"trade_id": "T2", "side": "SELL", "quantity": 40.0, "price": 101.5})
    outputs = job.transform(inputs(job.Inputs, interval=DAY, trades=trades, accounts=ACCOUNTS), job.Params())
    position = outputs.positions.iloc[0]
    assert position["net_quantity"] == 60.0
    assert position["gross_notional"] == 14160.0
    assert position["trades"] == 2.0


def test_closed_accounts_count_only_when_the_params_say_so() -> None:
    trades = frame(Trade, TRADE | {"account_id": "A2"})
    excluded = job.transform(inputs(job.Inputs, interval=DAY, trades=trades, accounts=ACCOUNTS), job.Params())
    included = job.transform(inputs(job.Inputs, interval=DAY, trades=trades, accounts=ACCOUNTS), job.Params(include_closed_accounts=True))
    assert excluded.positions.empty
    assert included.positions["account_id"].tolist() == ["A2"]


def test_activity_is_the_latest_booking_in_utc() -> None:
    trades = frame(Trade, TRADE, TRADE | {"trade_id": "T2", "booked_at": datetime(2026, 11, 5, 15, 0)})
    outputs = job.transform(inputs(job.Inputs, interval=DAY, trades=trades, accounts=ACCOUNTS), job.Params())
    assert outputs.activity["last_booked_at"].tolist() == [datetime(2026, 11, 5, 20, 0, tzinfo=UTC)]


def test_the_check_names_trades_in_unlisted_accounts() -> None:
    given = inputs(job.Inputs, interval=DAY, trades=frame(Trade, TRADE | {"account_id": "A9"}), accounts=ACCOUNTS)
    reasons = job.check(given, job.transform(given, job.Params()), job.Params())
    assert reasons == ["1 trades are in accounts the accounts table does not list"]


def test_no_trades_means_no_positions() -> None:
    outputs = job.transform(inputs(job.Inputs, interval=DAY, accounts=ACCOUNTS), job.Params())
    assert outputs.positions.empty
    assert isinstance(outputs.activity, pd.DataFrame)
