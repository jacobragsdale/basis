"""The check of trades too large to be real."""

from datetime import date, datetime

from basis.job.testing import frame, inputs

from jobs.positions import job
from jobs.positions.rows import Account, Trade

TRADE = {"trade_id": "T1", "account_id": "A1", "security_id": "S1", "trade_date": date(2026, 11, 5), "side": "BUY", "quantity": 20_000_000.0, "price": 101.0, "booked_at": datetime(2026, 11, 5, 9, 30)}


def test_a_trade_of_more_than_ten_million_is_refused() -> None:
    given = inputs(job.Inputs, interval="2026-11-05..2026-11-06", trades=frame(Trade, TRADE), accounts=frame(Account, {"account_id": "A1", "name": "Alpha", "closed": False}))
    assert job.check(given, job.transform(given, job.Params()), job.Params()) == ["1 trades are for more than 10,000,000"]
