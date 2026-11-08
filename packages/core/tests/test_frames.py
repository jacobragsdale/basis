import os
import random
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
from basis.core.frames import NumberError
from basis.core.storage import digest
from basis.core.tables import Column

COLUMNS = (
    Column("trade_date", "date", nullable=False),
    Column("account_id", "str", nullable=False),
    Column("quantity", "float", nullable=True),
    Column("price", "float", nullable=True),
    Column("closed", "bool", nullable=True),
    Column("booked_at", "datetime", nullable=True, zone="America/New_York"),
)
ROWS = [
    (date(2026, 11, 5), "A1", 100.25, 1.5, False, datetime(2026, 11, 5, 14, 31, tzinfo=UTC)),
    (date(2026, 11, 5), "A2", None, None, None, None),
    (date(2026, 11, 6), "A3", -0.0001, 2.25, True, datetime(2026, 11, 6, 9, 0, 0, 1, tzinfo=UTC)),
]


def made() -> pd.DataFrame:
    """The rows as a frame of the columns, built from their Python values."""
    return pd.DataFrame({column.name: pd.Series([row[index] for row in ROWS], dtype=frames.dtype(column)) for index, column in enumerate(COLUMNS)})


def test_a_number_is_numpys_float_with_nan_for_null_and_the_rest_are_arrow_backed_so_nothing_is_object() -> None:
    frame = made()
    assert [str(found) for found in frame.dtypes] == ["date32[day][pyarrow]", "string[pyarrow]", "float64", "float64", "bool[pyarrow]", "timestamp[us, tz=UTC][pyarrow]"]
    assert np.isnan(frame["quantity"].iloc[1])
    assert frame["quantity"].sum() == 100.2499


def test_a_frame_is_arrow_with_its_columns_types_nan_as_null_and_no_index_or_pandas_metadata() -> None:
    table = frames.to_arrow(made().iloc[[2, 1, 0]])
    assert table.schema == pa.schema([(column.name, frames.arrow_type(column)) for column in COLUMNS])
    assert table.schema.metadata is None
    assert table.column("account_id").to_pylist() == ["A3", "A2", "A1"]
    assert table.column("quantity").to_pylist() == [-0.0001, None, 100.25]
    back = frames.from_arrow(table)
    assert back.index.equals(pd.RangeIndex(3))
    assert back["quantity"].dtype == frames.FLOAT


def test_a_frame_that_is_neither_float_nor_arrow_backed_is_refused_rather_than_typed_by_a_guess() -> None:
    with pytest.raises(TypeError, match=r"neither floats nor Arrow-backed, so have no declared type: lots \(int64\): conform the frame"):
        frames.to_arrow(pd.DataFrame({"lots": [1]}))


@pytest.mark.parametrize(
    "build",
    [
        made,
        lambda: frames.from_arrow(frames.to_arrow(made())),
        lambda: frames.from_arrow(pa.Table.from_batches(frames.to_arrow(made()).to_batches(max_chunksize=1))),
        lambda: pd.concat([made().iloc[[0]], made().iloc[1:]]),
        lambda: made().sample(frac=1, random_state=1).sort_values("account_id"),
        lambda: made().assign(quantity=lambda frame: frame["quantity"].where(frame["quantity"].notna(), float("nan"))),
    ],
    ids=["from-python", "read-back", "one-chunk-per-row", "concatenated", "shuffled-and-sorted", "nan-made-again"],
)
def test_the_same_rows_are_the_same_object_however_the_frame_was_made(build: Callable[[], pd.DataFrame]) -> None:
    assert digest(frames.to_arrow(build())) == digest(frames.to_arrow(made()))


def test_the_same_rows_are_the_same_object_in_another_process() -> None:
    script = "from test_frames import made; from basis.core import frames; from basis.core.storage import digest; print(digest(frames.to_arrow(made())))"
    environment = os.environ | {"PYTHONPATH": os.pathsep.join([str(Path(__file__).parent), *filter(None, [os.environ.get("PYTHONPATH")])]), "PYTHONHASHSEED": "1"}
    ran = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=environment, check=True)
    assert ran.stdout.strip() == digest(frames.to_arrow(made()))


def test_a_decimal_read_is_the_nearest_double_which_arrows_own_cast_misses() -> None:
    """2.675 is the shortest text of a double just below it, and Arrow's cast of a decimal to a float gives the double
    after it, 2.6750000000000003; so a filter on `== 2.675` would miss it. Through text, every value is the nearest."""
    exact = pa.array([Decimal("2.675")], pa.decimal128(38, 10))
    assert exact.cast(pa.float64()).to_pylist() == [2.6750000000000003]  # pyarrow 26's own cast, which basis does not take
    assert frames.floats(pa.table({"amount": exact}), what="a test").column("amount").to_pylist() == [2.675]
    sample = random.Random(7)  # noqa: S311 -- reproducible test data, not secrets
    values = [Decimal(sample.randrange(-(10**18), 10**18)).scaleb(-2) for _ in range(100_000)]
    for decimal in (pa.decimal128(18, 2), pa.decimal128(38, 2), pa.decimal128(19, 4), pa.decimal128(10, 4)):
        kept = [value for value in values if abs(value) < Decimal(10) ** (decimal.precision - decimal.scale)]
        read = frames.floats(pa.table({"amount": pa.array(kept, decimal)}), what="a test").column("amount").to_pylist()
        assert read == [float(str(value)) for value in kept], decimal


def test_a_read_gives_every_number_as_a_float_and_refuses_a_whole_number_a_float_holds_only_roughly() -> None:
    table = pa.table({
        "small": pa.array([1, None], pa.int16()), "big": pa.array([2**53, -(2**53)], pa.int64()), "unsigned": pa.array([255, 0], pa.uint8()),
        "real": pa.array([0.1, 3.3], pa.float32()), "number": pa.array([Decimal(12), None], pa.decimal128(10, 0)), "name": pa.array(["a", "b"]),
    })  # fmt: skip
    read = frames.floats(table, what="dataset prices")
    assert [str(field.type) for field in read.schema] == ["double", "double", "double", "double", "double", "string"]
    assert read.to_pydict() == {"small": [1.0, None], "big": [2.0**53, -(2.0**53)], "unsigned": [255.0, 0.0], "real": [0.1, 3.3], "number": [12.0, None], "name": ["a", "b"]}
    for beyond in (pa.array([2**53 + 1], pa.int64()), pa.array([Decimal(-(2**53) - 1)], pa.decimal128(19, 0))):
        with pytest.raises(NumberError, match=r"^dataset prices: column id holds whole numbers beyond 2\^53, which a float holds only roughly: select it as text if it names something"):
            frames.floats(pa.table({"id": beyond}), what="dataset prices")


@pytest.mark.parametrize(
    ("column", "given", "expected"),
    [
        (Column("n", "float", nullable=True), pd.Series([1.0, np.nan]).astype("Int64"), [1.0, np.nan]),
        (Column("n", "float", nullable=True), pd.Series([1, 2], dtype="int32"), [1.0, 2.0]),
        (Column("n", "float", nullable=True), pd.Series([Decimal("2.675"), 2], dtype=object), [2.675, 2.0]),
        (Column("n", "float", nullable=True), pd.Series(["2.675", "-1e-7", None], dtype="string[pyarrow]"), [2.675, -1e-7, np.nan]),
        (Column("x", "float", nullable=True), pd.Series([1.5, np.nan]), [1.5, np.nan]),
        (Column("x", "float", nullable=True), pd.Series([0.1], dtype="float32"), [0.1]),
        (Column("s", "str", nullable=True), pd.Series(["a", None], dtype="category"), ["a", None]),
        (Column("d", "date", nullable=True), pd.Series([datetime(2026, 11, 5), None], dtype="datetime64[ns]"), [date(2026, 11, 5), None]),
        (Column("t", "datetime", nullable=True, zone="UTC"), pd.Series(pd.to_datetime(["2026-11-05 09:30"]).tz_localize("America/New_York")), [datetime(2026, 11, 5, 14, 30, tzinfo=UTC)]),
        (Column("b", "bool", nullable=True), pd.Series([0, 1, None], dtype="Int8"), [False, True, None]),
        (Column("b", "bool", nullable=True), pd.Series(["TRUE", "0", None]), [True, False, None]),
        (Column("d", "date", nullable=True), pd.Series(pd.to_datetime(["2024-01-01 00:00-05:00"])), [date(2024, 1, 1)]),
    ],
    ids=[
        "a-masked-int",
        "a-narrower-int",
        "python-decimals-and-ints",
        "number-text",
        "numpys-nan",
        "a-float32-by-its-shortest-text",
        "a-category",
        "a-datetime-at-midnight",
        "an-aware-datetime",
        "0-and-1",
        "boolean-text",
        "midnight-in-its-own-zone",
    ],
)
def test_a_value_converts_to_its_column_where_nothing_is_lost(column: Column, given: pd.Series, expected: list[object]) -> None:
    converted, problem = frames.convert(given, column, "reject")
    assert problem is None
    assert converted.dtype == frames.dtype(column)
    if column.kind == "float":
        assert np.array_equal(converted.to_numpy(), np.array(expected, dtype=float), equal_nan=True)
    else:
        assert converted.tolist() == [pd.NA if value is None else value for value in expected]


@pytest.mark.parametrize(
    ("column", "given", "problem"),
    [
        (Column("n", "float", nullable=True), pd.Series([2**53 + 1]), "whole numbers beyond 2^53, which a float holds only roughly: give it as text if it names something"),
        (Column("n", "float", nullable=True), pd.Series([2**64 - 1], dtype="uint64"), "whole numbers beyond 2^53"),
        (Column("x", "float", nullable=True), pd.Series([float("inf")]), "infinite values"),
        (Column("x", "float", nullable=True), pd.Series([True]), "bool cannot become double"),
        (Column("b", "bool", nullable=True), pd.Series([2]), "int64 values other than 0 and 1 are not booleans"),
        (Column("d", "date", nullable=True), pd.Series([datetime(2026, 11, 5, 9)]), "datetimes with a time of day are not dates"),
        (Column("d", "date", nullable=True), pd.Series(pd.to_datetime(["2024-01-01 00:00Z"]).tz_convert("America/New_York")), "datetimes with a time of day are not dates"),
        (Column("t", "datetime", nullable=True, zone="UTC"), pd.Series(pd.to_datetime([1001], unit="ns", utc=True)), "times finer than a microsecond"),
        (Column("t", "datetime", nullable=True, zone="UTC"), pd.Series([datetime(2026, 11, 5, 9)]), "naive datetimes: give them a zone, as .dt.tz_localize('UTC')"),
        (Column("s", "str", nullable=True), pd.Series(["a", 1], dtype=object), "values Arrow cannot type as one column"),
        (Column("s", "str", nullable=True), pd.Series([1]), "int64 cannot become string"),
    ],
    ids=[
        "beyond-2-53",
        "beyond-2-53-unsigned",
        "infinity",
        "a-boolean-as-a-number",
        "two-as-a-boolean",
        "a-time-of-day-as-a-date",
        "utcs-midnight-is-evening-in-new-york",
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
