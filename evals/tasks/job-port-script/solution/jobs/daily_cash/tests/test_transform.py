from datetime import date

from basis.job.testing import frame, inputs

from jobs.daily_cash import job
from jobs.daily_cash.rows import Movement

DAY = "2026-11-05..2026-11-06"
MOVEMENT: dict[str, object] = {"movement_id": "M1", "account_id": "A1", "currency": "USD", "amount": 1000.0, "value_date": date(2026, 11, 5), "status": "SETTLED"}


def run(*rows: dict[str, object]) -> tuple[job.Outputs, list[str]]:
    given = inputs(job.Inputs, interval=DAY, movements=frame(Movement, *rows))
    outputs = job.transform(given, job.Params())
    return outputs, job.check(given, outputs, job.Params())


def test_a_cancelled_movement_does_not_count() -> None:
    outputs, reasons = run(MOVEMENT, MOVEMENT | {"movement_id": "M2", "amount": -250.5, "status": "PENDING"}, MOVEMENT | {"movement_id": "M3", "status": "CANCELLED"})
    assert outputs.daily_cash["net_amount"].tolist() == [749.5]
    assert outputs.daily_cash["movements"].tolist() == [2.0]
    assert reasons == []


def test_a_counted_movement_with_a_bad_currency_is_refused() -> None:
    _, reasons = run(MOVEMENT | {"currency": "usd"})
    assert reasons == ["movement M1 has a bad currency 'usd': nothing written"]
