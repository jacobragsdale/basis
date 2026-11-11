"""The check of large trades, with its lint fixed: it still refuses a trade over 10 million and takes one of exactly
10 million, and prints nothing."""

from datetime import date, datetime

import pytest
from basis.job.testing import frame, inputs

from jobs.positions import job
from jobs.positions.rows import Account, Trade

TRADE = {"trade_id": "T1", "account_id": "A1", "security_id": "S1", "trade_date": date(2026, 11, 5), "side": "BUY", "quantity": 100.0, "price": 101.0, "booked_at": datetime(2026, 11, 5, 9, 30)}


def reasons(quantity: float) -> list[str]:
    given = inputs(job.Inputs, interval="2026-11-05..2026-11-06", trades=frame(Trade, TRADE | {"quantity": quantity}), accounts=frame(Account, {"account_id": "A1", "name": "Alpha", "closed": False}))
    return job.check(given, job.transform(given, job.Params()), job.Params())


def test_a_trade_over_ten_million_is_refused_and_one_of_ten_million_is_not(capsys: pytest.CaptureFixture[str]) -> None:
    assert reasons(10_000_000.5) == ["1 trades are for more than 10,000,000"]
    assert reasons(10_000_000.0) == []
    assert capsys.readouterr().out == ""
