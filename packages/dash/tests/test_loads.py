"""`await bd.load(DATASET, **params)`: off the event loop, in its database's bounded pool of threads, under its deadline;
one query for callers that ask at once; the cache's TTL, and its ceiling in megabytes; Arrow kept and a fresh frame
handed out; and what a load tells the page's status store. The database is basis-core's fake, whose answers take as
long as a test says."""

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from dash import Input, Output, callback, dcc, html

import basis.dash as bd
from basis.core import fakes
from basis.core.databases import Bind
from basis.core.errors import Timeout
from basis.core.settings import SettingsError
from basis.dash import testing
from basis.dash.ids import STATUS


class Position(bd.Row):
    account: str
    par: float
    as_of: date


SQL = (Path(__file__).parent / "sql" / "positions.sql").read_text(encoding="utf-8")
WAREHOUSE = bd.database("warehouse", dialect="mssql", deadline_s=5, max_concurrent=2)
POSITIONS = bd.query("positions", WAREHOUSE, "sql/positions.sql", Position, binds={"as_of": date}, ttl_s=60, stale_s=0)
DAY = date(2026, 11, 13)

PAGE = bd.page("/", "Positions", layout=html.Div([dcc.Input(id="loads-day"), html.Div(id="loads-rows"), html.Button(id="loads-ping"), html.Div(id="loads-pong")]))


@callback(Output("loads-rows", "children"), Input("loads-day", "value"))
async def rows(day: str) -> int:
    frame = await bd.load(POSITIONS, as_of=date.fromisoformat(day))
    return len(frame)


@callback(Output("loads-pong", "children"), Input("loads-ping", "n_clicks"))
async def pong(clicks: int | None) -> str:
    return f"pong {clicks}"


APP = bd.App(name="positions", title="Positions", settings=bd.Settings, resources=[WAREHOUSE], pages=[PAGE])


class Answers:
    """The fake warehouse's answer to the positions query: two rows for the day bound, after `wait_s`, counting each
    query and the most that ran at once."""

    def __init__(self, wait_s: float = 0.0) -> None:
        self.wait_s = wait_s
        self.queries = 0
        self.most = 0
        self._running = 0
        self._lock = threading.Lock()

    def __call__(self, binds: Mapping[str, Bind]) -> pd.DataFrame:
        with self._lock:
            self.queries += 1
            self._running += 1
            self.most = max(self.most, self._running)
        try:
            time.sleep(self.wait_s)
            return pd.DataFrame({"ACCOUNT": ["A1", "A2"], "PAR": [1_000_000, 250_000.5], "AS_OF": [binds["as_of"], binds["as_of"]]})
        finally:
            with self._lock:
                self._running -= 1


def _warehouse(answers: Callable[[Mapping[str, Bind]], pd.DataFrame]) -> fakes.Database:
    database = fakes.Database("warehouse")
    database.answers[SQL] = answers
    return database


@pytest.fixture
def answers() -> Answers:
    return Answers()


@pytest.fixture
def client(answers: Answers) -> Iterator[testing.Client]:
    with testing.client(APP, fakes={"warehouse": _warehouse(answers)}) as running:
        yield running


def _load(client: testing.Client, dataset: bd.Dataset = POSITIONS, /, **params: object) -> pd.DataFrame:
    """A load on the app's own event loop, as a callback makes it."""

    async def loading() -> pd.DataFrame:
        return await bd.load(dataset, **params)

    portal = client.http.portal
    assert portal is not None  # inside the client's block
    return portal.call(loading)


def test_a_slow_query_delays_no_other_callback_and_no_health_check(client: testing.Client, answers: Answers) -> None:
    answers.wait_s = 2.0
    with ThreadPoolExecutor(1) as pool:
        slow = pool.submit(client.fire, "loads-rows.children", {"loads-day.value": DAY.isoformat()})
        while answers.queries == 0:  # the query is running in its thread
            time.sleep(0.01)
        started = time.perf_counter()
        assert client.fire("loads-pong.children", {"loads-ping.n_clicks": 1})["loads-pong"] == {"children": "pong 1"}
        assert client.http.get("/health/live").status_code == 200
        waited = time.perf_counter() - started
        assert not slow.done()
        assert slow.result()["loads-rows"] == {"children": 2}
    assert waited < 0.5


def test_fifty_callers_at_once_make_one_query(client: testing.Client, answers: Answers) -> None:
    answers.wait_s = 0.5
    with ThreadPoolExecutor(50) as pool:
        answered = list(pool.map(lambda _: client.fire("loads-rows.children", {"loads-day.value": DAY.isoformat()}), range(50)))
    assert answers.queries == 1
    assert all(found["loads-rows"] == {"children": 2} for found in answered)


def test_a_fresh_value_answers_from_the_cache_and_each_caller_gets_its_own_frame(client: testing.Client, answers: Answers, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="basis.dash.runtime")
    first = _load(client, as_of=DAY)
    first.loc[0, "par"] = -1.0
    second = _load(client, as_of=DAY)
    assert answers.queries == 1
    assert second["par"].tolist() == [1_000_000.0, 250_000.5]  # the first caller's change is its own
    assert list(second.columns) == ["account", "par", "as_of"]  # conformed to the row: Oracle's upper case read
    assert second.dtypes["par"] == "float64"
    assert isinstance(second.dtypes["as_of"], pd.ArrowDtype)
    records = [record for record in caplog.records if record.getMessage() == "loaded positions"]
    assert [(record.__dict__["cache"], record.__dict__["rows"], record.__dict__["params"]) for record in records] == [("miss", 2, {"as_of": "2026-11-13"}), ("hit", 2, {"as_of": "2026-11-13"})]
    assert all(isinstance(record.__dict__["ms"], float) for record in records)
    _load(client, as_of=date(2026, 11, 12))
    assert answers.queries == 2  # another param is another key


def test_a_value_past_its_ttl_is_read_again(client: testing.Client, answers: Answers) -> None:
    now = [1000.0]
    client.built.runtime.cache.clock = lambda: now[0]
    _load(client, as_of=DAY)
    now[0] += 59
    _load(client, as_of=DAY)
    assert answers.queries == 1
    now[0] += 1
    _load(client, as_of=DAY)
    assert answers.queries == 2


def test_a_database_runs_at_most_its_max_concurrent_queries(client: testing.Client, answers: Answers) -> None:
    answers.wait_s = 0.2
    with ThreadPoolExecutor(6) as pool:
        list(pool.map(lambda day: _load(client, as_of=date(2026, 11, day)), range(1, 7)))
    assert (answers.queries, answers.most) == (6, 2)


def test_a_query_past_its_deadline_is_a_timeout() -> None:
    slow = bd.database("slow", dialect="mssql", deadline_s=0.2, max_concurrent=1)
    dataset = bd.query("slow_positions", slow, "sql/positions.sql", Position, binds={"as_of": date}, ttl_s=60, stale_s=0)
    app = bd.App(name="slow", title="Slow", settings=bd.Settings, resources=[slow], pages=[PAGE])
    database = fakes.Database("slow")
    database.answers[SQL] = Answers(wait_s=1.0)
    with testing.client(app, fakes={"slow": database}) as client, pytest.raises(Timeout, match=r"database slow did not answer query slow_positions within its deadline of 0\.2s"):
        _load(client, dataset, as_of=DAY)


def test_a_load_tells_the_pages_status_store_what_it_read(client: testing.Client) -> None:
    before = datetime.now(UTC)
    answered = client.fire("loads-rows.children", {"loads-day.value": DAY.isoformat()})
    patch: Any = answered[STATUS]["data"]
    (operation,) = patch["operations"]
    assert (operation["operation"], operation["location"]) == ("Assign", ["positions"])
    status = operation["params"]["value"]
    assert {key: status[key] for key in ("rows", "stale", "params")} == {"rows": 2, "stale": False, "params": {"as_of": "2026-11-13"}}
    assert datetime.fromisoformat(status["loaded_at"]) >= before.replace(microsecond=0)
    assert status["loaded_at"].endswith("Z")


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({}, r"loaded without as_of: give each of its params \(as_of: date\)"),
        ({"as_of": DAY, "book": "rates"}, "takes no param book"),
        ({"as_of": "2026-11-13"}, r"param as_of is a str, '2026-11-13': give a date, as date\.fromisoformat\(text\)"),
        ({"as_of": datetime(2026, 11, 13, tzinfo=UTC)}, "param as_of is a datetime"),
    ],
)
def test_a_load_gives_exactly_its_declared_params(client: testing.Client, params: dict[str, object], message: str) -> None:
    with pytest.raises(bd.LoadError, match=message):
        _load(client, **params)


def test_a_load_not_awaited_on_the_loop_is_refused_with_the_fix() -> None:
    def held() -> object:
        return bd.load(POSITIONS, as_of=DAY)

    async def callback_that_is_not_async() -> object:
        return held()

    with pytest.raises(bd.LoadError, match=r"loaded in held, which is not async, so it would hold the event loop and every page: make the callback async and await the load"):
        asyncio.run(callback_that_is_not_async())
    with pytest.raises(bd.LoadError, match="loaded in a thread with no event loop: make the callback async"):
        bd.load(POSITIONS, as_of=DAY)


def test_a_load_outside_a_running_app_or_of_another_apps_dataset_is_refused(client: testing.Client) -> None:
    elsewhere = bd.query("elsewhere", bd.database("other", dialect="mssql", deadline_s=1, max_concurrent=1), "sql/positions.sql", Position, ttl_s=1, stale_s=0)
    with pytest.raises(bd.LoadError, match="elsewhere is not a dataset of app positions: list every resource it reads"):
        _load(client, elsewhere)


def test_fakes_stand_in_for_every_resource_or_none(tmp_path: Path) -> None:
    desk = bd.folder("desk")
    app = bd.App(name="positions", title="Positions", settings=bd.Settings, resources=[WAREHOUSE, desk], pages=[PAGE])
    with pytest.raises(bd.DeclarationError, match="none for desk: give a fake of every resource"):
        bd.build(app, settings=bd.Settings.given(), fakes={"warehouse": fakes.Database("warehouse")})
    with pytest.raises(bd.DeclarationError, match="fakes of pricing, which it does not list"):
        bd.build(app, settings=bd.Settings.given(), fakes={"warehouse": fakes.Database("warehouse"), "desk": tmp_path, "pricing": object()})
    with pytest.raises(bd.DeclarationError, match="is oracle, and the database is declared mssql"):
        bd.build(app, settings=bd.Settings.given(), fakes={"warehouse": fakes.Database("warehouse", dialect="oracle"), "desk": tmp_path})


def test_a_databases_settings_are_of_its_declared_dialect() -> None:
    oracle = {"warehouse": {"kind": "oracle", "dsn": "db.example:1521/WH", "user": "reader", "password": "x", "mode": "thin", "isolation": "read_only", "timeout_s": 5, "max_rows": 10}}
    with pytest.raises(SettingsError, match="declared mssql, and its settings are oracle's"):
        bd.build(APP, settings=bd.Settings.given(databases=oracle))


class Mark(bd.Row):
    cusip: str
    price: float | None
    priced_on: date


def test_a_file_in_a_folder_is_read_as_text_and_typed_by_its_row(tmp_path: Path) -> None:
    (tmp_path / "marks.csv").write_text("cusip,price,priced_on\n001234,100.5,2026-11-13\n912828X1,,2026-11-12\n", encoding="utf-8")
    desk = bd.folder("desk")
    marks = bd.file("marks", desk, "marks.csv", Mark, ttl_s=60, stale_s=0)
    app = bd.App(name="marks", title="Marks", settings=bd.Settings, resources=[desk], pages=[PAGE])
    with testing.client(app, fakes={"desk": tmp_path}) as client:
        frame = _load(client, marks)
    assert frame["cusip"].tolist() == ["001234", "912828X1"]  # an id stays text
    assert frame["price"].isna().tolist() == [False, True]
    assert frame["priced_on"].tolist() == [date(2026, 11, 13), date(2026, 11, 12)]
