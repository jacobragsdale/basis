"""A dataset's rows and a grid's totals, written once per value and kept with it in the cache (`bd.load_rows`,
`GRID.load_totals`): the same JSON `bd.rows` and `GRID.totals` give, made in a worker thread for the first caller,
and for every other caller of that value nothing at all, until the value is read again."""

import json
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

import orjson
import pytest

import basis.dash as bd
from basis.dash import browser, internals
from basis.dash.example.app import app
from basis.dash.example.data import HOLDINGS, POSITIONS, Position

AS_OF = date(2026, 11, 12)
GRID = bd.grid("written-grid", Position, key="cusip", totals={"par": "sum", "ytw": bd.weighted("market_value")})


def _in(client: bd.testing.Client, made: Callable[[], Awaitable[Any]]) -> Any:  # noqa: ANN401 -- whatever the coroutine gives
    portal = client.http.portal
    assert portal is not None
    return portal.call(made)


def test_a_datasets_rows_are_written_once_and_are_the_rows_bd_rows_gives(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[int] = []
    rows = browser.rows
    monkeypatch.setattr(browser, "rows", lambda frame: built.append(len(frame)) or rows(frame))

    async def both() -> tuple[Any, Any, Any]:
        return await bd.load_rows(POSITIONS, as_of=AS_OF), await bd.load_rows(POSITIONS, as_of=AS_OF), await bd.load(POSITIONS, as_of=AS_OF)

    with bd.testing.client(app) as client:
        first, second, frame = _in(client, both)
        held = client.built.runtime.cache.bytes
    assert isinstance(first, orjson.Fragment)
    assert second is first  # kept with the value: the second caller pays nothing
    assert built == [320]  # built once, in its worker thread
    assert json.loads(internals.answer_json({"rowData": first}))["rowData"] == rows(frame)
    assert held > len(orjson.dumps(rows(frame)))  # the cache counts them beside the Arrow tables


def test_rows_are_written_again_once_the_value_is_read_again() -> None:
    async def twice() -> tuple[Any, Any]:
        before = await bd.load_rows(HOLDINGS, as_of=AS_OF)
        assert bd.reload(HOLDINGS, as_of=AS_OF) is False  # read under its reload_s ago: kept
        return before, await bd.load_rows(HOLDINGS, as_of=AS_OF)

    with bd.testing.client(app) as client:
        before, after = _in(client, twice)
        client.built.runtime.cache.forget(("holdings", (("as_of", AS_OF),)))

        async def again() -> Any:  # noqa: ANN401 -- the fragment
            return await bd.load_rows(HOLDINGS, as_of=AS_OF)

        read_again = _in(client, again)
    assert after is before
    assert read_again is not before
    assert orjson.loads(orjson.dumps({"rows": read_again})) == orjson.loads(orjson.dumps({"rows": before}))


def test_a_grids_totals_are_written_once_and_are_the_totals_it_gives() -> None:
    async def both() -> tuple[Any, Any, Any]:
        return await GRID.load_totals(POSITIONS, as_of=AS_OF), await GRID.load_totals(POSITIONS, as_of=AS_OF), await bd.load(POSITIONS, as_of=AS_OF)

    with bd.testing.client(app) as client:
        first, second, frame = _in(client, both)
    assert json.loads(internals.answer_json(first)) == json.loads(internals.answer_json(GRID.totals(frame)))
    assert json.loads(internals.answer_json(second)) == json.loads(internals.answer_json(first))


def test_a_load_of_rows_outside_a_started_app_is_refused() -> None:
    async def outside() -> object:
        return await bd.load_rows(POSITIONS, as_of=AS_OF)

    import asyncio  # noqa: PLC0415 -- a loop of the test's own, with no app started

    with pytest.raises(bd.LoadError, match="is loaded while no app is running"):
        asyncio.run(outside())
