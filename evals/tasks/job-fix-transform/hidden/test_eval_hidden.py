"""The positions job's net quantity, fixed in the transform: a purchase adds to it and a sale takes from it, over
several securities and accounts."""

from datetime import date, datetime

from basis.job.testing import frame, inputs

from jobs.positions import job
from jobs.positions.rows import Account, Trade

DAY = "2026-11-05..2026-11-06"
TRADE = {"trade_id": "T1", "account_id": "A1", "security_id": "S1", "trade_date": date(2026, 11, 5), "side": "BUY", "quantity": 100.0, "price": 101.0, "booked_at": datetime(2026, 11, 5, 9, 30)}
ACCOUNTS = frame(Account, {"account_id": "A1", "name": "Alpha", "closed": False}, {"account_id": "A2", "name": "Beta", "closed": False})


def test_a_purchase_adds_and_a_sale_takes_away() -> None:
    trades = frame(
        Trade,
        TRADE,
        TRADE | {"trade_id": "T2", "side": "SELL", "quantity": 30.0},
        TRADE | {"trade_id": "T3", "security_id": "S2", "side": "SELL", "quantity": 5.0},
        TRADE | {"trade_id": "T4", "account_id": "A2", "quantity": 7.0},
    )
    positions = job.transform(inputs(job.Inputs, interval=DAY, trades=trades, accounts=ACCOUNTS), job.Params()).positions
    net = {(account, security): quantity for account, security, quantity in zip(positions["account_id"], positions["security_id"], positions["net_quantity"], strict=True)}
    assert net == {("A1", "S1"): 70.0, ("A1", "S2"): -5.0, ("A2", "S1"): 7.0}
    assert positions["gross_notional"].tolist() == [13130.0, 505.0, 707.0]
