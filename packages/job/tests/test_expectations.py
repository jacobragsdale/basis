"""Expectations of an output (step 12.5): declared on a table, evaluated before any write, each that fails a reason
as a check's is. A row count is held to a band around the job's recent published runs', a column's share of nulls
to a bound, and a total to an input's."""

from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from builders import Price, prices
from test_publishing import database, run

import basis.job as etl
from basis.job.job import Job

TABLE = "dbo.prices"


class Trade(etl.Row):
    security_id: str
    price: float


def job(*expect: etl.Expectation) -> Job:
    """A job that copies its `prices` input to `dbo.prices`, which its expectations are of."""

    class Inputs(etl.Inputs):
        prices = etl.File(Price, path="prices.parquet", empty="fail")

    class Outputs(etl.Outputs):
        prices = etl.Table(Price, database="warehouse", table=TABLE, write=etl.Append(), expect=expect)

    def transform(inputs: Inputs, _params: etl.Params) -> Outputs:
        return Outputs(prices=inputs.prices)

    return Job("prices", "tests.prices", etl.Params, Inputs, Outputs, transform, None, Path(__file__).parent)


def rows(count: int, *, notes: int = 0) -> pd.DataFrame:
    given: list[dict[str, object]] = [{"security_id": f"S{n}", "note": "x" if n < notes else None} for n in range(count)]
    return prices(*given)


def test_a_row_count_far_from_the_recent_published_runs_is_a_reason_not_to_write(tmp_path: Path) -> None:
    warehouse = database("warehouse", (TABLE, None))
    expecting = job(etl.RowCount(within=0.5, last=3))
    for count, day in ((10, "05"), (12, "06"), (11, "07")):
        assert run(expecting, {"warehouse": warehouse}, rows(count), tmp_path, interval=f"2026-11-{day}..2026-11-{int(day) + 1:02}").record.status == "published"
    refused = run(expecting, {"warehouse": warehouse}, rows(4), tmp_path, interval="2026-11-08..2026-11-09")
    assert refused.record.status == "failed"
    assert refused.record.reasons == ["prices: 4 rows, and the last 3 published runs' median is 11: expected 5.5 to 16.5, within 50% of it"]
    assert run(expecting, {"warehouse": warehouse}, rows(9), tmp_path, interval="2026-11-08..2026-11-09").record.status == "published"


def test_a_row_count_with_no_published_run_before_it_has_nothing_to_compare_and_passes(tmp_path: Path) -> None:
    assert run(job(etl.RowCount(within=0.1)), {"warehouse": database("warehouse", (TABLE, None))}, rows(1), tmp_path).record.status == "published"


def test_a_columns_share_of_nulls_over_its_bound_is_a_reason(tmp_path: Path) -> None:
    refused = run(job(etl.NullRate("note", at_most=0.25)), {"warehouse": database("warehouse", (TABLE, None))}, rows(4, notes=2), tmp_path)
    assert refused.record.reasons == ["prices: note is null in 50% of its 4 rows, and at most 25% may be"]
    assert run(job(etl.NullRate("note", at_most=0.5)), {"warehouse": database("warehouse", (TABLE, None))}, rows(4, notes=2), tmp_path).record.status == "published"


def test_a_total_that_does_not_reconcile_with_an_inputs_is_a_reason(tmp_path: Path) -> None:
    class Inputs(etl.Inputs):
        prices = etl.File(Price, path="prices.parquet", empty="fail")

    class Outputs(etl.Outputs):
        prices = etl.Table(Price, database="warehouse", table=TABLE, write=etl.Append(), expect=(etl.Total("price", matches="prices.price", within=0.01),))

    def halved(inputs: Inputs, _params: etl.Params) -> Outputs:
        return Outputs(prices=inputs.prices.assign(price=inputs.prices["price"] / 2))

    job_ = Job("prices", "tests.prices", etl.Params, Inputs, Outputs, halved, None, Path(__file__).parent)
    refused = run(job_, {"warehouse": database("warehouse", (TABLE, None))}, rows(2), tmp_path)
    assert refused.record.reasons == ["prices: price totals 100, and prices.price totals 200: they differ by 100, and may by 0.01 at most"]


def test_a_total_of_an_input_the_job_does_not_read_is_a_reason_naming_it(tmp_path: Path) -> None:
    refused = run(job(etl.Total("price", matches="trades.price")), {"warehouse": database("warehouse", (TABLE, None))}, rows(1), tmp_path)
    assert refused.record.reasons == ["prices: expects a total to match trades.price, and the job reads no dataset trades with a column price"]


@pytest.mark.parametrize(
    ("expect", "says"),
    [
        (lambda: etl.RowCount(within=0), "a row count's band is a share of the median above 0"),
        (lambda: etl.RowCount(within=0.5, last=0), "last is the number of published runs, 1 or more"),
        (lambda: etl.NullRate("note", at_most=1.5), "a share of nulls is from 0 to 1"),
        (lambda: etl.Total("price", matches="price"), "matches names DATASET.COLUMN"),
        (lambda: etl.Total("price", matches="trades.price", within=-1), "within is 0 or more"),
    ],
)
def test_an_expectation_declared_wrong_is_refused_where_it_is_made(expect: Callable[[], object], says: str) -> None:
    with pytest.raises(ValueError, match=says):
        expect()


@pytest.mark.parametrize(
    ("expect", "says"),
    [(etl.NullRate("venue", at_most=0.1), "expects venue's share of nulls, and its row has no column venue"), (etl.Total("note", matches="prices.note"), "expects note's total, and note is no float")],
)
def test_an_expectation_of_a_column_the_row_lacks_is_refused_where_the_table_is_declared(expect: etl.Expectation, says: str) -> None:
    with pytest.raises(ValueError, match=says):
        etl.Table(Price, database="warehouse", table=TABLE, write=etl.Append(), expect=(expect,))
