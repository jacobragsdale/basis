"""Each callback's answer is one log record with its output, its bytes before compression and its milliseconds, and a
warning past the budget; other requests are left alone."""

import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import basis.dash as bd
from basis.dash import sizes, testing

TESTS = Path(__file__).parent


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[bd.App]:
    monkeypatch.syspath_prepend(str(TESTS))
    from fixture_app.app import app  # noqa: PLC0415 -- the tests' package, on the path once the fixture puts it there

    yield app
    for name in [name for name in sys.modules if name == "fixture_app" or name.startswith("fixture_app.")]:
        monkeypatch.delitem(sys.modules, name)


def test_each_callbacks_answer_is_measured_and_one_past_the_budget_is_a_warning(app: bd.App, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    caplog.set_level(logging.INFO, logger="basis.dash.sizes")
    with testing.client(app) as client:
        client.index()
        client.fire("fixture-par.children", {"fixture-day.value": "2026-11-13"})
        monkeypatch.setattr(sizes, "BUDGET_BYTES", 10)
        client.fire("fixture-par.children", {"fixture-day.value": "2026-11-12"})
    records = [record for record in caplog.records if record.name == "basis.dash.sizes"]
    assert [(record.levelno, record.__dict__["output"], record.__dict__["status"]) for record in records] == [
        (logging.INFO, "fixture-par.children", 200),
        (logging.WARNING, "fixture-par.children", 200),
    ]
    assert all(record.__dict__["bytes"] > 50 for record in records)
    assert "more than the 0 MB budget" in records[1].getMessage()
