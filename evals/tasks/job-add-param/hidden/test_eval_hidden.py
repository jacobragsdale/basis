"""The `exclude_securities` param: described, read from JSON, empty by default, and leaving its securities out of both
outputs, while every other security's rows stay as they were."""

from datetime import date, datetime

from basis.job.testing import frame, inputs

from jobs.positions import job
from jobs.positions.rows import Account, Trade

DAY = "2026-11-05..2026-11-06"
TRADE = {"trade_id": "T1", "account_id": "A1", "security_id": "S1", "trade_date": date(2026, 11, 5), "side": "BUY", "quantity": 100.0, "price": 101.0, "booked_at": datetime(2026, 11, 5, 9, 30)}
TRADES = (TRADE, TRADE | {"trade_id": "T2", "security_id": "S2", "quantity": 10.0}, TRADE | {"trade_id": "T3", "account_id": "A2", "security_id": "S2", "booked_at": datetime(2026, 11, 5, 16, 0)})
ACCOUNTS = frame(Account, {"account_id": "A1", "name": "Alpha", "closed": False}, {"account_id": "A2", "name": "Beta", "closed": False})


def outputs(params: job.Params) -> job.Outputs:
    return job.transform(inputs(job.Inputs, interval=DAY, trades=frame(Trade, *TRADES), accounts=ACCOUNTS), params)


def test_it_is_described() -> None:
    described = job.Params.model_json_schema()["properties"]["exclude_securities"]
    assert len(str(described.get("description", "")).strip()) > 10


def test_by_default_it_leaves_nothing_out() -> None:
    assert list(job.Params().exclude_securities) == []
    assert sorted(outputs(job.Params()).positions["security_id"]) == ["S1", "S2", "S2"]


def test_it_is_read_from_json() -> None:
    assert list(job.Params.model_validate_json('{"exclude_securities": ["S2"]}').exclude_securities) == ["S2"]


def test_it_leaves_its_securities_out_of_every_output() -> None:
    found = outputs(job.Params.model_validate_json('{"exclude_securities": ["S2"]}'))
    assert found.positions["security_id"].tolist() == ["S1"]
    assert found.positions["net_quantity"].tolist() == [100.0]
    assert found.activity["account_id"].tolist() == ["A1"]


def test_other_securities_are_as_they_were() -> None:
    every = outputs(job.Params()).positions
    kept = outputs(job.Params.model_validate_json('{"exclude_securities": ["S9"]}')).positions
    assert kept.equals(every)
