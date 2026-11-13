"""A callback's failure: one sentence a person can act on, in the page's notifications with the request's id, its
outputs keeping what they showed, and the failure logged once; and a dataset inside its `stale_s` answering its last
good value, marked stale, when reading it again fails. The fixture database fails on demand, so no mock is needed."""

import logging
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import basis.dash as bd
from basis.core.errors import Conflict, Denied, Failed, ServiceError, Timeout, Unavailable
from basis.dash import fixtures, testing
from basis.dash.ids import NOTIFICATIONS, STATUS

TESTS = Path(__file__).parent
DAY = {"fixture-day.value": "2026-11-13"}


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[bd.App]:
    monkeypatch.syspath_prepend(str(TESTS))
    from fixture_app.app import app  # noqa: PLC0415 -- the tests' package, on the path once the fixture puts it there

    yield app
    for name in [name for name in sys.modules if name == "fixture_app" or name.startswith("fixture_app.")]:
        monkeypatch.delitem(sys.modules, name)


def _warehouse(app: bd.App) -> fixtures.FixtureDatabase:
    (resource,) = app.resources
    assert isinstance(resource, bd.Database)
    return fixtures.database(resource, TESTS / "fixtures")


@pytest.mark.parametrize(
    ("kind", "title", "then"),
    [
        (Timeout, "warehouse did not answer within 5 s.", "This shows what it showed before: try again in a moment."),
        (Unavailable, "warehouse could not be reached.", "This shows what it showed before: try again in a moment. If it lasts, tell the app's support team."),
        (Denied, "warehouse refused this app.", "Tell the app's support team: its login or a permission needs fixing."),
        (Conflict, "warehouse failed to answer.", "Tell the app's support team."),
        (Failed, "warehouse failed to answer.", "Tell the app's support team."),
    ],
)
def test_a_failure_is_a_sentence_in_the_notifications_and_logged_once(app: bd.App, caplog: pytest.LogCaptureFixture, kind: type[ServiceError], title: str, then: str) -> None:
    caplog.set_level(logging.INFO)
    warehouse = _warehouse(app)
    warehouse.fails(kind, query="positions")
    with testing.client(app, fakes={"warehouse": warehouse}) as client:
        answered = client.fire("fixture-par.children", DAY)
    assert "fixture-par" not in answered  # the output keeps what it showed
    (shown,) = answered[NOTIFICATIONS]["sendNotifications"]  # ty: ignore[not-iterable] -- a list, as on_error sends it
    assert shown["title"] == title
    request = re.fullmatch(rf"{re.escape(then)} Request ([0-9a-f-]{{36}})\.", shown["message"])
    assert request is not None, shown["message"]
    assert shown["id"] == f"basis-error-{request.group(1)}"
    assert (shown["color"], shown["autoClose"], shown["withCloseButton"]) == ("red", False, True)
    failed = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(failed) == 1
    assert (failed[0].__dict__["kind"], failed[0].__dict__["resource"], failed[0].exc_info) == (kind.__name__, "warehouse", None)  # no defect, so no traceback


def test_a_dataset_inside_its_stale_window_answers_its_last_good_value_marked_stale(app: bd.App, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    warehouse = _warehouse(app)
    with testing.client(app, fakes={"warehouse": warehouse}) as client:
        now = [1000.0]
        client.built.runtime.cache.clock = lambda: now[0]
        first = client.fire("fixture-par.children", DAY)
        assert _status(first)["stale"] is False
        now[0] += 120  # past its TTL of 60 s, inside its stale_s of 3,600
        warehouse.fails(Unavailable, query="positions", times=1)
        stale = client.fire("fixture-par.children", DAY)
        assert stale["fixture-par"] == first["fixture-par"]
        assert NOTIFICATIONS not in stale
        assert _status(stale)["stale"] is True
        assert _status(stale)["loaded_at"] == _status(first)["loaded_at"]  # when it was read
        warnings = [record for record in caplog.records if record.levelno == logging.WARNING and record.name == "basis.dash.runtime"]
        assert [record.__dict__["kind"] for record in warnings] == ["Unavailable"]
        assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
        fresh = client.fire("fixture-par.children", DAY)  # the database answers again, and the value is read
        assert _status(fresh)["stale"] is False
        assert _status(fresh)["loaded_at"] > _status(first)["loaded_at"]
        now[0] += 3_661  # past the stale window too: a failure is a failure
        warehouse.fails(Unavailable, query="positions", times=1)
        assert NOTIFICATIONS in client.fire("fixture-par.children", DAY)


def _status(answered: dict[str, dict[str, object]]) -> dict[str, Any]:
    patch: Any = answered[STATUS]["data"]
    (operation,) = patch["operations"]
    return operation["params"]["value"]
