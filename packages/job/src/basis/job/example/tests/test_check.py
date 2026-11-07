"""The job's own check, beyond the framework's: each reason it gives not to write."""

from datetime import date, datetime

from basis.job.testing import frame, inputs

from basis.job.example import job
from basis.job.example.rows import Account, Trade

DAY = "2026-11-05..2026-11-06"
TRADE: dict[str, object] = {
    "trade_id": "T1",
    "account_id": "A1",
    "security_id": "S1",
    "trade_date": date(2026, 11, 5),
    "side": "BUY",
    "quantity": 100,
    "price": 101,
    "booked_at": datetime(2026, 11, 5, 9, 30),
}
ACCOUNTS = frame(Account, {"account_id": "A1", "name": "Alpha", "closed": False})


def reasons(*trades: dict[str, object]) -> list[str]:
    given = inputs(job.Inputs, interval=DAY, trades=frame(Trade, *trades), accounts=ACCOUNTS)
    return job.check(given, job.transform(given, job.Params()), job.Params())


def test_a_days_trades_give_no_reason() -> None:
    assert reasons(TRADE, TRADE | {"trade_id": "T2", "side": "SELL", "quantity": 40}) == []


def test_the_check_names_a_side_that_is_neither_a_purchase_nor_a_sale() -> None:
    assert reasons(TRADE | {"side": "HOLD"}) == ["trades have sides other than BUY and SELL: ['HOLD']"]


def test_the_check_names_positions_after_the_interval() -> None:
    assert reasons(TRADE | {"trade_date": date(2026, 11, 6)}) == ["1 positions are for trade dates after 2026-11-06"]
