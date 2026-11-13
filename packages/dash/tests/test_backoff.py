"""A read that fails is not made again until a back-off passes: 5 s, doubling to 60 s at most and never past the
dataset's `ttl_s`, cleared by a read that succeeds. Meanwhile a load answers the stale value where there is one, and the
same failure at once where there is none, without touching the database; `bd.reload` does not cut it short."""

import sys
from collections.abc import Awaitable, Callable, Iterator
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

import basis.dash as bd
from basis.core.errors import Unavailable
from basis.dash import fixtures, testing
from basis.dash.cache import Cache

TESTS = Path(__file__).parent
DAY = date(2026, 11, 13)


class Running:
    """An app running on a fake database, with a clock the test moves."""

    def __init__(self, client: testing.Client, dataset: bd.Query) -> None:
        self.client, self.dataset = client, dataset
        self.now = 1000.0
        client.built.runtime.cache.clock = lambda: self.now

    def load(self) -> pd.DataFrame:
        async def loading() -> pd.DataFrame:
            return await bd.load(self.dataset, **self.params)

        return self._call(loading)

    def stale(self) -> bool:
        """A load, and whether it answered a stale value."""

        async def fetching() -> bool:
            return (await self.client.built.runtime.fetch(self.dataset, self.params)).stale

        return self._call(fetching)

    @property
    def params(self) -> dict[str, object]:
        return {"as_of": DAY} if self.dataset.params else {}

    def _call[T](self, function: Callable[[], Awaitable[T]]) -> T:
        portal = self.client.http.portal
        assert portal is not None  # inside the client's block
        return portal.call(function)


@pytest.fixture
def declared(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[bd.App, bd.Query, fixtures.FixtureDatabase]]:
    monkeypatch.syspath_prepend(str(TESTS))
    from fixture_app.app import app  # noqa: PLC0415 -- the tests' package, on the path once the fixture puts it there
    from fixture_app.data import POSITIONS, WAREHOUSE  # noqa: PLC0415

    yield app, POSITIONS, fixtures.database(WAREHOUSE, TESTS / "fixtures")
    for name in [name for name in sys.modules if name == "fixture_app" or name.startswith("fixture_app.")]:
        monkeypatch.delitem(sys.modules, name)


def test_fifty_loads_during_the_back_off_make_no_query_and_the_first_after_it_makes_one(declared: tuple[bd.App, bd.Query, fixtures.FixtureDatabase]) -> None:
    app, positions, warehouse = declared
    with testing.client(app, fakes={"warehouse": warehouse}) as client:
        running = Running(client, positions)
        assert running.stale() is False
        running.now += 120  # past the TTL of 60 s, inside the stale window of 3,600
        warehouse.fails(Unavailable, query="positions", times=3)
        assert running.stale() is True  # the read failed: the last good value answers, and 5 s of back-off start
        assert len(warehouse.queries) == 2
        assert all(running.stale() for _ in range(50))
        running.now += 4.9
        assert running.stale() is True
        assert len(warehouse.queries) == 2
        running.now += 0.1
        assert running.stale() is True
        assert len(warehouse.queries) == 3
        running.now += 9.9
        assert running.stale() is True
        assert len(warehouse.queries) == 3
        assert bd.reload(positions, as_of=DAY) is False  # a reload does not cut the back-off short
        running.now += 0.1
        assert running.stale() is True  # the third failure: 20 s now
        running.now += 20
        assert running.stale() is False  # the database answers again, and the back-off is cleared
        assert len(warehouse.queries) == 5


def test_with_no_value_kept_a_load_during_the_back_off_fails_at_once_without_a_query(declared: tuple[bd.App, bd.Query, fixtures.FixtureDatabase]) -> None:
    app, positions, warehouse = declared
    warehouse.fails(Unavailable, query="positions", times=1)
    with testing.client(app, fakes={"warehouse": warehouse}) as client:
        running = Running(client, positions)
        with pytest.raises(Unavailable, match="database warehouse could not be reached"):
            running.load()
        for _ in range(50):
            with pytest.raises(Unavailable, match="database warehouse could not be reached"):
                running.load()
        assert len(warehouse.queries) == 1
        running.now += 5
        assert len(running.load()) == 2
        assert len(warehouse.queries) == 2


def test_the_back_off_doubles_to_60_s_never_past_the_datasets_ttl_and_a_success_clears_it() -> None:
    found = ("prices", ())
    failure = Unavailable("database prices could not be reached, or lost the connection", resource="prices")
    for ttl_s, delays in ((300.0, [5.0, 10.0, 20.0, 40.0, 60.0, 60.0]), (8.0, [5.0, 8.0, 8.0])):
        cache = Cache(1, clock=lambda: 0.0)
        assert [cache.failed(found, failure, ttl_s=ttl_s) for _ in delays] == delays
        assert cache.failing(found) is failure
        cache.cleared(found)
        assert cache.failing(found) is None
        assert cache.failed(found, failure, ttl_s=ttl_s) == 5.0
