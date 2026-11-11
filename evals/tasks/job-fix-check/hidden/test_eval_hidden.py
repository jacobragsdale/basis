"""The positions job's check, fixed: good data over a several-day interval gives no reason, and it still stops
positions after the interval, trades in accounts it does not know, and sides that are neither BUY nor SELL."""

from datetime import date, datetime

from basis.job.testing import frame, inputs

from jobs.positions import job
from jobs.positions.rows import Account, Trade

DAYS = "2026-11-02..2026-11-06"
TRADE = {"trade_id": "T1", "account_id": "A1", "security_id": "S1", "trade_date": date(2026, 11, 2), "side": "BUY", "quantity": 100.0, "price": 101.0, "booked_at": datetime(2026, 11, 2, 9, 30)}
ACCOUNTS = frame(Account, {"account_id": "A1", "name": "Alpha", "closed": False})


def reasons(*trades: dict[str, object]) -> list[str]:
    given = inputs(job.Inputs, interval=DAYS, trades=frame(Trade, *trades), accounts=ACCOUNTS)
    return job.check(given, job.transform(given, job.Params()), job.Params())


def test_good_data_over_several_days_gives_no_reason() -> None:
    assert reasons(TRADE, TRADE | {"trade_id": "T2", "trade_date": date(2026, 11, 3)}, TRADE | {"trade_id": "T3", "trade_date": date(2026, 11, 5), "side": "SELL"}) == []


def test_a_position_on_the_intervals_end_is_late() -> None:
    assert len(reasons(TRADE, TRADE | {"trade_id": "T2", "trade_date": date(2026, 11, 6)})) == 1


def test_a_position_after_it_is_late() -> None:
    assert len(reasons(TRADE | {"trade_date": date(2026, 11, 9)})) == 1


def test_an_unknown_account_and_a_bad_side_are_still_stopped() -> None:
    assert len(reasons(TRADE | {"account_id": "A9"})) == 1
    assert len(reasons(TRADE | {"side": "HOLD"})) == 1
