"""The positions job's check passes good data and still stops bad data: a position on or after the interval's end, a
trade in an unlisted account, and a side other than BUY or SELL."""

from datetime import date, datetime

from etl_framework.testing import frame, inputs
from jobs.positions import job
from jobs.positions.rows import Account, Trade

WEEK = "2026-11-05..2026-11-08"
TRADE = {"trade_id": "T1", "account_id": "A1", "security_id": "S1", "trade_date": date(2026, 11, 5), "side": "BUY", "quantity": 100, "price": 101, "booked_at": datetime(2026, 11, 5, 9, 30)}
ACCOUNTS = frame(Account, {"account_id": "A1", "name": "Alpha", "closed": False})


def reasons(*trades: dict[str, object], interval: str | None = WEEK) -> list[str]:
    given = inputs(job.Inputs, interval=interval, trades=frame(Trade, *trades), accounts=ACCOUNTS)
    return list(job.check(given, job.transform(given, job.Params()), job.Params()))


def test_positions_inside_a_several_day_interval_pass() -> None:
    assert reasons(TRADE, TRADE | {"trade_id": "T2", "trade_date": date(2026, 11, 7)}) == []


def test_a_position_on_the_interval_end_is_after_it() -> None:
    assert len(reasons(TRADE, TRADE | {"trade_id": "T2", "trade_date": date(2026, 11, 8)})) == 1


def test_a_position_beyond_the_interval_end_is_after_it() -> None:
    assert len(reasons(TRADE | {"trade_date": date(2026, 11, 12)})) == 1


def test_a_run_without_an_interval_checks_no_dates() -> None:
    assert reasons(TRADE | {"trade_date": date(2026, 11, 12)}, interval=None) == []


def test_a_trade_in_an_unlisted_account_still_stops_the_run() -> None:
    assert len(reasons(TRADE | {"account_id": "A9"})) == 1


def test_a_side_other_than_buy_or_sell_still_stops_the_run() -> None:
    assert len(reasons(TRADE | {"side": "BYU"})) == 1
