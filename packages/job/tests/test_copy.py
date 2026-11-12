"""A copy of a query's rows to a lake (step 12.6): a table too large for a run's memory, streamed in batches from the
driver to Parquet with no frame and no transform, each batch conformed to the query's row, written as the run's
interval's partition as an archive is, recorded in the ledger, and renamed into place. Only `--publish` copies; a
resume streams it again; a replay copies nothing."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pandas as pd
import pyarrow.fs as pafs
import pyarrow.parquet as pq
import pytest
from builders import DAY, Price, params_file, prices

import basis.job as etl
from basis.core import fakes, frames, lake
from basis.core.databases import Database
from basis.core.errors import BasisError, Unavailable
from basis.core.settings import GivenSettings
from basis.core.storage import Root
from basis.job import runs
from basis.job.job import Job

DATABASE = {"kind": "mssql", "connection_string": "Server=sql01;Database=Trading", "isolation": "snapshot", "timeout_s": 5, "max_rows": 5}
WAREHOUSE = DATABASE | {"write": {"connection_string": "Server=sql01;UID=etl", "ledger": "etl.publishes"}}
SETTINGS = GivenSettings.model_validate({"databases": {"warehouse": WAREHOUSE, "markets": DATABASE}})
SQL = (Path(__file__).parent / "sql" / "copied_prices.sql").read_text(encoding="utf-8")
PARTITION = "archive/prices/date=2026-11-05"


def clock() -> datetime:
    return datetime(2026, 11, 6, 6, 0, tzinfo=UTC)


class Inputs(etl.Inputs):
    pass


class Outputs(etl.Outputs):
    copied = etl.LakeCopy(etl.Query(Price, database="markets", sql="sql/copied_prices.sql", empty="fail"), lake="history", table="prices", partition="date", ledger="warehouse")


def transform(inputs: Inputs, params: etl.Params) -> Outputs:
    del inputs, params  # a copy has no transform
    return Outputs()


JOB = Job("copy", "tests.copy", etl.Params, Inputs, Outputs, transform, None, Path(__file__).parent)


@dataclass
class World:
    root: Path
    ledger: fakes.Database = field(default_factory=lambda: fakes.Database("warehouse"))
    markets: fakes.Database = field(default_factory=lambda: fakes.Database("markets"))

    def __post_init__(self) -> None:
        (self.root / "lake" / "archive").mkdir(parents=True)
        given: list[dict[str, object]] = [{"security_id": f"S{n}", "price": float(n)} for n in range(7)]
        self.markets.answers[SQL] = prices(*given)  # beyond max_rows=5, which a copy is not held to
        self.markets.batch_rows = 3

    def open_lake(self, name: str) -> lake.Lake:
        assert name == "history"
        return lake.Lake("history", "archive", pafs.SubTreeFileSystem(str(self.root / "lake"), pafs.LocalFileSystem()))

    def database(self, name: str) -> Database:
        return {"warehouse": self.ledger, "markets": self.markets}[name]

    def run(self, *, publish: bool = True) -> runs.Result:
        request = runs.Request(params_file(self.root), DAY, None, publish=publish)
        return runs.run(JOB, SETTINGS, request, snapshot_root=Root.at(self.root / "snapshots"), clock=clock, databases=self.database, open_lake=self.open_lake)

    def partition(self) -> pd.DataFrame:
        return frames.from_arrow(pq.read_table(self.root / "lake" / PARTITION))


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_a_published_run_streams_its_querys_rows_to_the_partition_in_files_of_at_most_file_rows(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lake, "FILE_ROWS", 4)  # 7 rows in batches of 3, two files: 4 and 3
    result = world.run()
    copied = result.record.copied["copied"]
    assert (result.record.status, copied.status, copied.rows, copied.path) == ("published", "archived", 7, PARTITION)
    assert [(file.name, file.rows) for file in copied.files] == [("part-0.parquet", 4), ("part-1.parquet", 3)]
    assert sorted(world.partition()["security_id"]) == [f"S{n}" for n in range(7)]
    assert str(pq.read_schema(world.root / "lake" / PARTITION / "part-0.parquet").field("booked_at").type) == "timestamp[us, tz=UTC]"  # conformed to the row
    [entry] = world.ledger.entries
    assert (entry.output, entry.table, entry.counts.inserted, json.loads(entry.detail or "{}")["rows"]) == ("copied", "lake:history:prices", 7, 7)
    assert result.record.copies["copied"].sql == "sql/copied_prices.sql"


def test_a_run_without_publish_copies_nothing_and_saves_no_rows(world: World) -> None:
    result = world.run(publish=False)
    assert (result.record.status, result.record.copied, world.ledger.entries) == ("checked", {}, [])
    assert result.record.copies["copied"].partition == "date=2026-11-05"
    assert not (world.root / "lake" / PARTITION).exists()


def test_a_copy_whose_query_fails_leaves_the_run_partial_and_its_resume_streams_it_again(world: World) -> None:
    world.markets.fails(Unavailable, on="fetch", times=1)
    result = world.run()
    assert (result.record.status, result.record.copied["copied"].status) == ("partial", "failed")
    assert result.snapshot is not None
    assert runs.resume(JOB, result.snapshot, SETTINGS, databases=world.database, open_lake=world.open_lake).status == "published"
    assert len(world.partition()) == 7


def test_a_copy_of_no_rows_fails_where_its_query_does_not_allow_it(world: World) -> None:
    world.markets.answers[SQL] = prices().iloc[:0]
    result = world.run()
    assert result.record.copied["copied"].status == "failed"
    assert "returned no rows, which its dataset does not allow" in (result.record.copied["copied"].error or "")


def test_the_transform_gives_a_copy_nothing() -> None:
    with pytest.raises(TypeError, match="declares no outputs named \\['copied'\\]"):
        Outputs(copied=prices())


def test_a_copy_names_a_query_and_its_query_is_checked_before_the_run_reads_settings(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match=r"a copy is of an etl\.Query"):
        etl.LakeCopy(cast("etl.Query", etl.File(Price, path="p.csv", empty="fail")), lake="history", table="prices", partition="date", ledger="warehouse")

    class Missing(etl.Outputs):
        copied = etl.LakeCopy(etl.Query(Price, database="markets", sql="sql/nowhere.sql", empty="fail"), lake="history", table="prices", partition="date", ledger="warehouse")

    with pytest.raises(BasisError, match=r"copied reads .*nowhere\.sql, which does not exist"):
        runs.open_sql(Job("copy", "tests.copy", etl.Params, Inputs, Missing, transform, None, tmp_path))
