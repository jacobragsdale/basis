"""Incremental loads (step 12.7): a query that binds a watermark, the highest value of a column the job's last
publish read, so each run reads only what is new. The watermark is an input the manifest records, what it bound and
the highest value it read, so a replay, which reads the snapshot, reads what the run read, and the next run knows
where to start."""

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated

import pandas as pd
import pytest
from test_publishing import settings

import basis.job as etl
from basis.core import fakes
from basis.core.databases.base import Bind
from basis.core.errors import BasisError
from basis.core.interval import Interval
from basis.core.storage import Root
from basis.job import replays, runs
from basis.job.job import Job
from basis.job.outputs import Target
from basis.job.testing import frame

FOLDER = Path(__file__).parent
SQL = (FOLDER / "sql" / "watermarked.sql").read_text(encoding="utf-8")


class Trade(etl.Row):
    trade_id: Annotated[str, etl.MaxLength(12)]
    updated_at: Annotated[datetime, etl.Zone("UTC")]


def job(watermark: etl.Watermark | None = None) -> Job:
    class Inputs(etl.Inputs):
        trades = etl.Query(Trade, database="markets", sql="sql/watermarked.sql", empty="allow", watermark=watermark or etl.Watermark("updated_at", initial="2026-11-01T00:00:00Z"))

    class Outputs(etl.Outputs):
        trades = etl.Table(Trade, database="warehouse", table="dbo.trades", write=etl.Append())

    def transform(inputs: Inputs, _params: etl.Params) -> Outputs:
        return Outputs(trades=inputs.trades)

    return Job("trades", "tests.trades", etl.Params, Inputs, Outputs, transform, None, FOLDER)


class Source:
    """The markets database's trades table, which answers the query with the rows updated after its watermark."""

    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.bound: list[Bind] = []

    def __call__(self, binds: Mapping[str, Bind]) -> pd.DataFrame:
        mark = binds["watermark"]
        self.bound.append(mark)
        assert isinstance(mark, datetime)
        assert mark.tzinfo is None  # naive UTC, as the interval's binds are
        return frame(Trade, *(row for row in self.rows if isinstance(updated := row["updated_at"], datetime) and updated.replace(tzinfo=None) > mark))


def at(hour: int) -> datetime:
    return datetime(2026, 11, 5, hour, tzinfo=UTC)


@pytest.fixture
def world(tmp_path: Path) -> tuple[Source, dict[str, fakes.Database], Path]:
    source = Source()
    markets = fakes.Database("markets")
    markets.answers[SQL] = source
    warehouse = fakes.Database("warehouse")
    warehouse.create(Target(Trade, "warehouse", "dbo.trades", etl.Append(), None))
    return source, {"markets": markets, "warehouse": warehouse}, tmp_path


def run(world: tuple[Source, dict[str, fakes.Database], Path], *, publish: bool = True, day: int = 5) -> runs.Result:
    """A run for the day `day` of November, as a scheduled incremental job's runs are each for their own day."""
    _, found, root = world
    (root / "params.json").write_text("{}", encoding="utf-8")
    request = runs.Request(root / "params.json", Interval.parse(f"2026-11-{day:02}..2026-11-{day + 1:02}"), publish=publish)
    return runs.run(job(), settings("markets", "warehouse"), request, snapshot_root=Root.at(root / "snapshots"), databases=found.__getitem__)


def test_each_run_reads_what_is_new_since_the_last_publish_and_records_its_watermark(world: tuple[Source, dict[str, fakes.Database], Path]) -> None:
    source, found, _ = world
    source.rows = [{"trade_id": "T1", "updated_at": at(9)}, {"trade_id": "T2", "updated_at": at(10)}]
    first = run(world)
    assert first.record.status == "published"
    mark = first.record.inputs["trades"].watermark
    assert mark is not None
    assert (mark.column, mark.since, mark.through) == ("updated_at", "2026-11-01T00:00:00+00:00", "2026-11-05T10:00:00+00:00")
    source.rows.append({"trade_id": "T3", "updated_at": at(11)})
    second = run(world, day=6)
    assert second.record.inputs["trades"].rows == 1
    assert (second.record.inputs["trades"].watermark or mark).since == "2026-11-05T10:00:00+00:00"
    assert sorted(str(row["trade_id"]) for row in found["warehouse"].rows("dbo.trades")) == ["T1", "T2", "T3"]
    assert source.bound == [datetime(2026, 11, 1), datetime(2026, 11, 5, 10)]


def test_a_run_that_reads_nothing_new_keeps_the_watermark_where_it_was(world: tuple[Source, dict[str, fakes.Database], Path]) -> None:
    source, _, _ = world
    source.rows = [{"trade_id": "T1", "updated_at": at(9)}]
    run(world)
    quiet = run(world, day=6)
    mark = quiet.record.inputs["trades"].watermark
    assert mark is not None
    assert (quiet.record.inputs["trades"].rows, mark.since, mark.through) == (0, "2026-11-05T09:00:00+00:00", "2026-11-05T09:00:00+00:00")


def test_a_run_that_does_not_publish_moves_no_watermark(world: tuple[Source, dict[str, fakes.Database], Path]) -> None:
    source, _, _ = world
    source.rows = [{"trade_id": "T1", "updated_at": at(9)}]
    run(world, publish=False)
    assert run(world).record.inputs["trades"].rows == 1
    assert source.bound == [datetime(2026, 11, 1), datetime(2026, 11, 1)]


def test_a_replay_reads_what_the_run_read_from_its_snapshot_whatever_the_source_holds_now(world: tuple[Source, dict[str, fakes.Database], Path]) -> None:
    source, _, root = world
    source.rows = [{"trade_id": "T1", "updated_at": at(9)}]
    first = run(world)
    source.rows.append({"trade_id": "T2", "updated_at": at(10)})
    assert first.snapshot is not None
    replayed, differences = replays.replay(job(), first.snapshot, snapshot_root=Root.at(root / "replays"))
    assert (replayed.record.inputs["trades"].rows, differences) == (1, [])
    assert replayed.record.inputs["trades"].watermark == first.record.inputs["trades"].watermark


def test_a_date_watermark_binds_a_date() -> None:
    class Dated(etl.Row):
        trade_id: str
        day: date

    query = etl.Query(Dated, database="markets", sql="sql/watermarked.sql", empty="allow", watermark=etl.Watermark("day", initial="2026-11-01"))
    assert query.watermark is not None
    assert query.watermark.bind("2026-11-01", query.row) == date(2026, 11, 1)


@pytest.mark.parametrize(
    ("make", "says"),
    [
        (lambda: etl.Query(Trade, database="markets", sql="q.sql", empty="allow", watermark=etl.Watermark("venue", initial="1")), "its row has no column venue"),
        (lambda: etl.Query(Trade, database="markets", sql="q.sql", empty="allow", watermark=etl.Watermark("updated_at", initial="yesterday")), "initial 'yesterday' is no datetime"),
        (lambda: etl.Query(Trade, database="markets", sql="q.sql", empty="allow", watermark=etl.Watermark("trade_id", initial="")), "a watermark's initial value is given"),
    ],
)
def test_a_watermark_declared_wrong_is_refused_where_it_is_made(make: Callable[[], object], says: str) -> None:
    with pytest.raises(ValueError, match=says):
        make()


def test_a_query_that_binds_a_watermark_it_does_not_declare_is_refused(tmp_path: Path) -> None:
    (tmp_path / "q.sql").write_text(SQL, encoding="utf-8")

    class Inputs(etl.Inputs):
        trades = etl.Query(Trade, database="markets", sql=str(tmp_path / "q.sql"), empty="allow")

    with pytest.raises(BasisError, match="trades binds watermark: a query binds only interval_start and interval_end, and watermark where it declares one"):
        runs.open_sql(Job("trades", "tests.trades", etl.Params, Inputs, etl.Outputs, lambda *_: etl.Outputs(), None, tmp_path))
