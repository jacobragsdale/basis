import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from basis.core import frames
from basis.core.storage import digest
from basis.core.tables import Column

COLUMNS = (
    Column("trade_date", "date", nullable=False),
    Column("account_id", "str", nullable=False),
    Column("quantity", "decimal", nullable=True, precision=18, scale=4),
    Column("lots", "int", nullable=True),
    Column("price", "float", nullable=True),
    Column("closed", "bool", nullable=True),
    Column("booked_at", "datetime", nullable=True, zone="America/New_York"),
)
ROWS = [
    (date(2026, 11, 5), "A1", Decimal("100.2500"), 3, 1.5, False, datetime(2026, 11, 5, 14, 31, tzinfo=UTC)),
    (date(2026, 11, 5), "A2", None, None, None, None, None),
    (date(2026, 11, 6), "A3", Decimal("-0.0001"), 2**62, 2.25, True, datetime(2026, 11, 6, 9, 0, 0, 1, tzinfo=UTC)),
]


def made() -> pd.DataFrame:
    """The rows as a frame of the columns, built from their Python values."""
    return pd.DataFrame({column.name: pd.Series([row[index] for row in ROWS], dtype=frames.dtype(column)) for index, column in enumerate(COLUMNS)})


def test_every_kind_of_column_is_arrow_backed_so_nothing_is_object_and_nulls_keep_their_type() -> None:
    frame = made()
    assert [str(found) for found in frame.dtypes] == [
        "date32[day][pyarrow]", "string[pyarrow]", "decimal128(18, 4)[pyarrow]", "int64[pyarrow]", "double[pyarrow]", "bool[pyarrow]", "timestamp[us, tz=UTC][pyarrow]",
    ]  # fmt: skip
    assert all(isinstance(found, pd.ArrowDtype) for found in frame.dtypes)
    assert frame["lots"].tolist()[:2] == [3, pd.NA]
    assert frame["quantity"].sum() == Decimal("100.2499")


def test_a_frame_is_arrow_with_its_columns_types_and_no_index_or_pandas_metadata() -> None:
    table = frames.to_arrow(made().iloc[[2, 0]])
    assert table.schema == pa.schema([(column.name, frames.arrow_type(column)) for column in COLUMNS])
    assert table.schema.metadata is None
    assert table.column("account_id").to_pylist() == ["A3", "A1"]
    assert frames.from_arrow(table).index.equals(pd.RangeIndex(2))


def test_a_frame_that_is_not_arrow_backed_is_refused_rather_than_typed_by_a_guess() -> None:
    with pytest.raises(TypeError, match=r"not Arrow-backed, so have no declared type: price \(float64\): conform the frame"):
        frames.to_arrow(pd.DataFrame({"price": [1.5]}))


@pytest.mark.parametrize(
    "build",
    [
        made,
        lambda: frames.from_arrow(frames.to_arrow(made())),
        lambda: frames.from_arrow(pa.Table.from_batches(frames.to_arrow(made()).to_batches(max_chunksize=1))),
        lambda: pd.concat([made().iloc[[0]], made().iloc[1:]]),
        lambda: made().sample(frac=1, random_state=1).sort_values("account_id"),
    ],
    ids=["from-python", "read-back", "one-chunk-per-row", "concatenated", "shuffled-and-sorted"],
)
def test_the_same_rows_are_the_same_object_however_the_frame_was_made(build: Callable[[], pd.DataFrame]) -> None:
    assert digest(frames.to_arrow(build())) == digest(frames.to_arrow(made()))


def test_the_same_rows_are_the_same_object_in_another_process() -> None:
    script = "from test_frames import made; from basis.core import frames; from basis.core.storage import digest; print(digest(frames.to_arrow(made())))"
    environment = os.environ | {"PYTHONPATH": os.pathsep.join([str(Path(__file__).parent), *filter(None, [os.environ.get("PYTHONPATH")])]), "PYTHONHASHSEED": "1"}
    ran = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=environment, check=True)
    assert ran.stdout.strip() == digest(frames.to_arrow(made()))


@pytest.mark.parametrize(
    ("column", "given", "expected"),
    [
        (Column("n", "int", nullable=True), pd.Series([1.0, np.nan]).astype("Int64"), [1, None]),
        (Column("n", "int", nullable=True), pd.Series([1, 2], dtype="int32"), [1, 2]),
        (Column("q", "decimal", nullable=True, precision=18, scale=4), pd.Series([12, None], dtype="Int64"), [Decimal("12.0000"), None]),
        (Column("q", "decimal", nullable=True, precision=18, scale=4), pd.Series([Decimal("1.5"), 2], dtype=object), [Decimal("1.5000"), Decimal("2.0000")]),
        (Column("x", "float", nullable=True), pd.Series([1.5, np.nan]), [1.5, None]),
        (Column("s", "str", nullable=True), pd.Series(["a", None], dtype="category"), ["a", None]),
        (Column("d", "date", nullable=True), pd.Series([datetime(2026, 11, 5), None], dtype="datetime64[ns]"), [date(2026, 11, 5), None]),
        (Column("t", "datetime", nullable=True, zone="UTC"), pd.Series(pd.to_datetime(["2026-11-05 09:30"]).tz_localize("America/New_York")), [datetime(2026, 11, 5, 14, 30, tzinfo=UTC)]),
        (Column("b", "bool", nullable=True), pd.Series([0, 1, None], dtype="Int8"), [False, True, None]),
        (Column("b", "bool", nullable=True), pd.Series(["TRUE", "0", None]), [True, False, None]),
    ],
    ids=[
        "a-masked-int",
        "a-narrower-int",
        "an-int-as-a-decimal",
        "python-decimals-and-ints",
        "numpys-nan-as-null",
        "a-category",
        "a-datetime-at-midnight",
        "an-aware-datetime",
        "0-and-1",
        "boolean-text",
    ],
)
def test_a_value_converts_to_its_column_where_nothing_is_lost(column: Column, given: pd.Series, expected: list[object]) -> None:
    converted, problem = frames.convert(given, column, "reject")
    assert (problem, converted.dtype, converted.tolist()) == (None, frames.dtype(column), [pd.NA if value is None else value for value in expected])


@pytest.mark.parametrize(
    ("column", "given", "problem"),
    [
        (Column("n", "int", nullable=True), pd.Series([Decimal("1.5")], dtype=object), "1 of 1 values change when stored as int64"),
        (Column("n", "int", nullable=True), pd.Series([2**63], dtype="uint64"), "1 of 1 values change when stored as int64"),
        (
            Column("q", "decimal", nullable=True, precision=5, scale=2),
            pd.Series([Decimal("1.234"), Decimal("1234.5"), Decimal("1.5")], dtype=object),
            "2 of 3 values change when stored as decimal128(5, 2)",
        ),
        (Column("q", "decimal", nullable=True, precision=5, scale=2), pd.Series([1.5]), "double is inexact"),
        (Column("x", "float", nullable=True), pd.Series([2**53 + 1]), "1 of 1 values change when stored as double"),
        (Column("x", "float", nullable=True), pd.Series([float("inf")]), "NaN or infinite values"),
        (Column("b", "bool", nullable=True), pd.Series([2]), "int64 values other than 0 and 1 are not booleans"),
        (Column("d", "date", nullable=True), pd.Series([datetime(2026, 11, 5, 9)]), "datetimes with a time of day are not dates"),
        (Column("t", "datetime", nullable=True, zone="UTC"), pd.Series(pd.to_datetime([1001], unit="ns", utc=True)), "times finer than a microsecond"),
        (Column("t", "datetime", nullable=True, zone="UTC"), pd.Series([datetime(2026, 11, 5, 9)]), "naive datetimes: give them a zone, as .dt.tz_localize('UTC')"),
        (Column("s", "str", nullable=True), pd.Series(["a", 1], dtype=object), "values Arrow cannot type as one column"),
        (Column("s", "str", nullable=True), pd.Series([1]), "int64 cannot become string"),
    ],
    ids=[
        "a-fraction-to-an-int",
        "beyond-int64",
        "rounding-or-overflowing-a-decimal",
        "a-float-to-a-decimal",
        "an-int-a-float-would-round",
        "infinity",
        "two-as-a-boolean",
        "a-time-of-day-as-a-date",
        "nanoseconds",
        "a-naive-time-in-an-output",
        "mixed-objects",
        "a-number-as-text",
    ],
)
def test_a_value_that_would_change_is_refused_with_what_would_change(column: Column, given: pd.Series, problem: str) -> None:
    _, found = frames.convert(given, column, "reject")
    assert found is not None
    assert problem in found
