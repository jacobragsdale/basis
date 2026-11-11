from datetime import date, datetime

from basis.job.testing import frame, inputs

from jobs.positions import job
from jobs.positions.rows import Account, Trade

TRADE: dict[str, object] = {
    "trade_id": "T1",
    "account_id": "A1",
    "security_id": "S1",
    "trade_date": date(2026, 11, 5),
    "side": "BUY",
    "quantity": 100.0,
    "price": 100.0,
    "booked_at": datetime(2026, 11, 5, 9, 30),
}


def reasons(params: job.Params, *trades: dict[str, object]) -> list[str]:
    given = inputs(job.Inputs, interval="2026-11-05..2026-11-06", trades=frame(Trade, *trades), accounts=frame(Account, {"account_id": "A1", "name": "Alpha", "closed": False}))
    return job.check(given, job.transform(given, params), params)


def test_a_price_that_moves_too_far_in_a_day_is_refused() -> None:
    assert reasons(job.Params(max_price_move=0.05), TRADE, TRADE | {"trade_id": "T2", "price": 110.0}) == ["S1's price moved more than 5% on 2026-11-05"]


def test_a_smaller_move_is_taken() -> None:
    assert reasons(job.Params(max_price_move=0.05), TRADE, TRADE | {"trade_id": "T2", "price": 101.0}) == []
