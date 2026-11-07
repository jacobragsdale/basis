from datetime import date
from decimal import Decimal

from etl_framework.testing import frame, inputs

from jobs.daily_cash import job
from jobs.daily_cash.rows import Movement

DAY = "2026-11-05..2026-11-06"
MOVEMENT = {"movement_id": "M1", "account_id": "A1", "currency": "USD", "amount": Decimal(100), "value_date": date(2026, 11, 5), "status": "SETTLED"}


def test_movements_net_per_account_and_currency() -> None:
    movements = frame(Movement, MOVEMENT, MOVEMENT | {"movement_id": "M2", "amount": Decimal("-30.5"), "status": "PENDING"})
    outputs = job.transform(inputs(job.Inputs, interval=DAY, movements=movements), job.Params())
    assert outputs.daily_cash.rows() == [(date(2026, 11, 5), "A1", "USD", Decimal("69.5000"), 2)]


def test_cancelled_movements_do_not_count() -> None:
    movements = frame(Movement, MOVEMENT | {"status": "CANCELLED", "currency": "usd"})
    given = inputs(job.Inputs, interval=DAY, movements=movements)
    outputs = job.transform(given, job.Params())
    assert outputs.daily_cash.height == 0
    assert job.check(given, outputs, job.Params()) == []


def test_a_counted_movement_with_a_bad_currency_stops_the_run() -> None:
    given = inputs(job.Inputs, interval=DAY, movements=frame(Movement, MOVEMENT | {"currency": "usd"}))
    assert job.check(given, job.transform(given, job.Params()), job.Params()) == ["movements ['M1'] have a bad currency"]
