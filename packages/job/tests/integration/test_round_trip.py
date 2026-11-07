"""Values through a real database and the run store and back: an integer with nulls stays an integer, a decimal keeps
every digit, and a datetime keeps its instant, each as the frame that left: published by one run, read by another's
query, and each run's snapshot read back."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import pandas as pd
import pytest
from integration.scratch import Scratch, run

import basis.job as etl
from basis.core import frames
from basis.core.interval import Interval
from basis.core.settings import GivenSettings
from basis.core.storage import Root
from basis.job import ddl, runs
from basis.job.job import Job
from basis.job.outputs import Target
from basis.job.testing import frame

pytestmark = pytest.mark.integration


class Kept(etl.Row):
    id: Annotated[str, etl.MaxLength(8)]
    lots: int | None
    amount: Annotated[Decimal, etl.Precision(28, 10)]
    booked_at: Annotated[datetime, etl.Zone("America/New_York")] | None


KEPT = frame(
    Kept,
    {"id": "K1", "lots": None, "amount": Decimal("-123456789012345678.0123456789"), "booked_at": datetime(2026, 7, 1, 12, 0, 0, 123456)},
    {"id": "K2", "lots": 2**62, "amount": Decimal("0.0000000001"), "booked_at": None},
    {"id": "K3", "lots": -1, "amount": Decimal(0), "booked_at": datetime(2026, 11, 1, 3, 0)},
)


def publishing(table: str) -> Job:
    class Inputs(etl.Inputs):
        prices = etl.File(Kept, path="prices.parquet", empty="fail")

    class Outputs(etl.Outputs):
        kept = etl.Table(Kept, database="warehouse", table=table, write=etl.Upsert(), key=("id",))

    def transform(inputs: Inputs, _params: etl.Params) -> Outputs:
        return Outputs(kept=inputs.prices)

    return Job("kept_it", "tests.kept_it", etl.Params, Inputs, Outputs, transform, None, Path(__file__).parent)


def reading(query: Path) -> Job:
    class Inputs(etl.Inputs):
        kept = etl.Query(Kept, database="source", sql=str(query), empty="fail")

    class Outputs(etl.Outputs):
        kept = etl.Table(Kept, database="unused", table="kept", write=etl.Append())

    def transform(inputs: Inputs, _params: etl.Params) -> Outputs:
        return Outputs(kept=inputs.kept)

    return Job("kept_reads", "tests.kept_reads", etl.Params, Inputs, Outputs, transform, None, Path(__file__).parent)


def test_an_integer_with_nulls_a_decimal_and_a_zone_aware_time_come_back_as_they_left(scratch: Scratch, tmp_path: Path) -> None:
    table = f"kept_{scratch.suffix}"
    scratch.execute(ddl.create_table(Target(Kept, "warehouse", table, etl.Upsert(), ("id",)), scratch.dialect))
    try:
        published = run(publishing(table), {"warehouse": scratch}, KEPT, tmp_path)
        assert published.record.status == "published", published.record
        assert scratch.rows(f"SELECT booked_at FROM {table} WHERE id = 'K1'") == [(datetime(2026, 7, 1, 12, 0, 0, 123456),)]  # naive, in its column's zone

        query = tmp_path / "kept.sql"
        query.write_text(f"SELECT id, lots, amount, booked_at FROM {table}\n", encoding="utf-8")
        params = tmp_path / "params.json"
        params.write_text("{}", encoding="utf-8")
        isolation = {"isolation": "read_committed"} if scratch.dialect == "oracle" else {}  # the table is moments old: see test_read.load
        settings = GivenSettings(databases={"source": scratch.settings | isolation})
        read = runs.run(reading(query), settings, runs.Request(params, Interval.parse("2026-11-05..2026-11-06")), snapshot_root=Root.at(tmp_path / "read"))
        assert read.record.status == "checked", read.record
        assert published.snapshot is not None
        assert read.snapshot is not None
    finally:
        scratch.drop(table)

    saved = published.snapshot.changes()["kept"].upsert
    assert saved is not None
    for found in (read.inputs["kept"], frames.from_arrow(read.snapshot.inputs()["kept"]), saved):
        assert found.sort_values("id", ignore_index=True).equals(KEPT)
    assert [str(dtype) for dtype in KEPT.dtypes] == ["string[pyarrow]", "int64[pyarrow]", "decimal128(28, 10)[pyarrow]", "timestamp[us, tz=UTC][pyarrow]"]
    assert KEPT["lots"].tolist() == [pd.NA, 2**62, -1]
    assert KEPT["booked_at"].tolist()[0] == datetime(2026, 7, 1, 16, 0, 0, 123456, tzinfo=UTC)
