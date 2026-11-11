"""The `max_price_move` check, still there: off by default, and refusing a day when a security's price moves more
than the param allows, while a smaller move passes."""

from datetime import date, datetime

from basis.job.testing import frame, inputs

from jobs.positions import job
from jobs.positions.rows import Account, Trade

TRADE = {"trade_id": "T1", "account_id": "A1", "security_id": "S1", "trade_date": date(2026, 11, 5), "side": "BUY", "quantity": 100.0, "price": 100.0, "booked_at": datetime(2026, 11, 5, 9, 30)}


def reasons(params: job.Params, *trades: dict[str, object]) -> list[str]:
    given = inputs(job.Inputs, interval="2026-11-05..2026-11-06", trades=frame(Trade, *trades), accounts=frame(Account, {"account_id": "A1", "name": "Alpha", "closed": False}))
    return job.check(given, job.transform(given, params), params)


def test_it_is_off_by_default() -> None:
    assert reasons(job.Params(), TRADE, TRADE | {"trade_id": "T2", "price": 200.0}) == []


def test_a_move_beyond_the_param_is_refused_and_one_within_it_is_not() -> None:
    params = job.Params.model_validate_json('{"max_price_move": 0.05}')
    assert len(reasons(params, TRADE, TRADE | {"trade_id": "T2", "price": 106.0})) == 1
    assert reasons(params, TRADE, TRADE | {"trade_id": "T2", "price": 104.0}) == []
