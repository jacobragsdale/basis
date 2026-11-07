"""The positions job's `exclude_securities` param: described, empty by default, read from a params file, and
leaving the trades in those securities out of every output."""

import json
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from etl_framework.testing import frame, inputs
from jobs.positions import job
from jobs.positions.rows import Account, Trade

DAY = "2026-11-05..2026-11-06"
TRADE = {"trade_id": "T1", "account_id": "A1", "security_id": "XS0001", "trade_date": date(2026, 11, 5), "side": "BUY", "quantity": 100, "price": 101, "booked_at": datetime(2026, 11, 5, 10, 0)}
TRADES = frame(
    Trade,
    TRADE,
    TRADE | {"trade_id": "T2", "security_id": "XS0002", "booked_at": datetime(2026, 11, 5, 9, 0)},
    TRADE | {"trade_id": "T3", "account_id": "A2", "booked_at": datetime(2026, 11, 5, 11, 0)},
)
ACCOUNTS = frame(Account, {"account_id": "A1", "name": "Alpha", "closed": False}, {"account_id": "A2", "name": "Beta", "closed": False})


def params(text: str) -> job.Params:
    """Params as `etl run` reads them from a file: JSON, validated strictly."""
    return job.Params.model_validate_json(text)


def test_etl_describe_shows_its_description() -> None:
    described = subprocess.run([Path(sys.executable).parent / "etl", "describe", "positions"], capture_output=True, text=True, check=True)
    field = json.loads(described.stdout)["params"]["properties"]["exclude_securities"]
    assert field.get("description", "").strip()


def test_by_default_it_leaves_nothing_out() -> None:
    assert len(params("{}").exclude_securities) == 0
    outputs = job.transform(inputs(job.Inputs, interval=DAY, trades=TRADES, accounts=ACCOUNTS), params("{}"))
    assert outputs.positions.height == 3


def test_excluded_securities_have_no_positions() -> None:
    outputs = job.transform(inputs(job.Inputs, interval=DAY, trades=TRADES, accounts=ACCOUNTS), params('{"exclude_securities": ["XS0001"]}'))
    assert outputs.positions.select("account_id", "security_id").rows() == [("A1", "XS0002")]


def test_excluded_securities_count_in_no_activity() -> None:
    outputs = job.transform(inputs(job.Inputs, interval=DAY, trades=TRADES, accounts=ACCOUNTS), params('{"exclude_securities": ["XS0001"]}'))
    assert outputs.activity.select("account_id", "last_booked_at").rows() == [("A1", datetime(2026, 11, 5, 14, 0, tzinfo=UTC))]


def test_several_securities_can_be_left_out() -> None:
    outputs = job.transform(inputs(job.Inputs, interval=DAY, trades=TRADES, accounts=ACCOUNTS), params('{"exclude_securities": ["XS0001", "XS0002"]}'))
    assert outputs.positions.height == 0
    assert outputs.activity.height == 0


def test_closed_accounts_still_follow_their_own_param() -> None:
    closed = frame(Account, {"account_id": "A1", "name": "Alpha", "closed": False}, {"account_id": "A2", "name": "Beta", "closed": True})
    given = inputs(job.Inputs, interval=DAY, trades=TRADES, accounts=closed)
    assert job.transform(given, params('{"exclude_securities": ["XS0002"]}')).positions.select("account_id", "security_id").rows() == [("A1", "XS0001")]
    assert job.transform(given, params('{"exclude_securities": ["XS0002"], "include_closed_accounts": true}')).positions.height == 2
