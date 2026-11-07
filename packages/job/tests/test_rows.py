from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated

import pandas as pd
import pyarrow as pa
import pytest
from builders import Price

import basis.job as etl
from basis.core import frames
from basis.job.rows import RowError, SchemaError, conform


def problems(frame: pd.DataFrame, *, naive: str = "in_zone") -> list[str]:
    with pytest.raises(SchemaError) as raised:
        conform(frame, Price, what="prices", naive="reject" if naive == "reject" else "in_zone")
    return raised.value.problems


def valid(**changed: object) -> pd.DataFrame:
    """One valid row of Python values, as a transform might return it, with the columns `changed` replaces."""
    return pd.DataFrame({"day": [date(2026, 11, 5)], "security_id": ["S1"], "price": [Decimal(1)], "note": [None], "booked_at": [None]} | {name: [value] for name, value in changed.items()})


def test_a_csvs_text_converts_to_every_column_type() -> None:
    text = pd.DataFrame({"DAY": ["2026-11-05"], "security_id": ["S1"], "price": ["100.25"], "note": [None], "booked_at": ["2026-11-05T09:30:00"]}, dtype="string[pyarrow]")
    row = conform(text, Price, what="prices", naive="in_zone").iloc[0].to_dict()
    assert row == {"day": date(2026, 11, 5), "security_id": "S1", "price": Decimal("100.2500"), "note": None, "booked_at": datetime(2026, 11, 5, 14, 30, tzinfo=UTC)}


def test_an_oracle_date_and_number_convert_when_nothing_is_lost() -> None:
    oracle = pa.table({
        "DAY": pa.array([datetime(2026, 11, 5)], pa.timestamp("us")), "SECURITY_ID": pa.array(["S1"], pa.large_string()), "PRICE": pa.array([Decimal(100)], pa.decimal128(38, 0)),
        "NOTE": pa.array([None], pa.string()), "BOOKED_AT": pa.array([None], pa.timestamp("ms")),
    })  # fmt: skip
    assert conform(frames.from_arrow(oracle), Price, what="prices", naive="in_zone").dtypes.to_dict() == Price.dtypes()


def test_every_column_is_arrow_backed_so_an_integer_with_nulls_stays_an_integer() -> None:
    class Counted(etl.Row):
        count: int | None
        at: Annotated[datetime, etl.Zone("Europe/London")] | None

    conformed = conform(pd.DataFrame({"count": pd.array([1, None], dtype="Int64"), "at": [datetime(2026, 7, 1, 12), None]}), Counted, what="counts", naive="in_zone")
    assert [str(found) for found in conformed.dtypes] == ["int64[pyarrow]", "timestamp[us, tz=UTC][pyarrow]"]
    assert conformed["count"].tolist() == [1, pd.NA]
    assert conformed["at"].tolist() == [datetime(2026, 7, 1, 11, tzinfo=UTC), pd.NA]
    with pytest.raises(SchemaError, match="double is inexact"):  # numpy's integers have no null, so pandas makes [1, None] floats
        conform(pd.DataFrame({"count": [1, None], "at": [None, None]}), Counted, what="counts", naive="in_zone")


@pytest.mark.parametrize(
    ("column", "value", "problem"),
    [
        ("day", pd.Series([datetime(2026, 11, 5, 9)]), "datetimes with a time of day are not dates"),
        ("price", pd.Series([Decimal("1.23456")], dtype=object), "1 of 1 values change when stored as decimal128(18, 4)"),
        ("price", pd.Series([1.5]), "double is inexact"),
        ("price", pd.Series(["1.23456"], dtype="string[pyarrow]"), "at most 4 decimal places"),
        ("security_id", pd.Series(["S123456789"]), "longer than its MaxLength(8)"),
        ("security_id", pd.Series([None], dtype="string[pyarrow]"), "1 nulls in a column that may not be null"),
        ("security_id", pd.Series([12]), "int64 cannot become string"),
    ],
)
def test_a_value_that_would_change_or_does_not_fit_is_refused(column: str, value: pd.Series, problem: str) -> None:
    assert any(problem in found for found in problems(valid().assign(**{column: value})))


def test_a_naive_datetime_in_an_output_is_refused() -> None:
    frame = valid(note="x", booked_at=datetime(2026, 11, 5, 9))
    assert problems(frame, naive="reject") == ["booked_at: naive datetimes: give them a zone, as .dt.tz_localize('America/New_York'), or convert them to UTC"]


def test_missing_and_extra_columns_are_named() -> None:
    frame = pd.DataFrame({"day": [date(2026, 11, 5)], "security_id": ["S1"], "extra": [1]})
    assert problems(frame) == ["columns the row does not declare: extra", "missing columns: price, note, booked_at"]


def test_a_column_left_in_the_index_is_named_as_such() -> None:
    grouped = valid().groupby(["day", "security_id"]).agg(price=("price", "sum"), note=("note", "first"), booked_at=("booked_at", "first"))
    assert problems(grouped) == ["missing columns: day, security_id (day, security_id in the frame's index: reset_index() it, or group with as_index=False)"]


@pytest.mark.parametrize(
    ("annotation", "message"),
    [
        (Decimal, "names its precision and scale"),
        (datetime, "names the zone its naive values are in"),
        (Annotated[datetime, etl.Zone("Mars/Olympus")], "not an IANA time zone"),
        (list[int], "is not a column type"),
        (int | str, "one type, or one type `| None`"),
        (Annotated[int, etl.MaxLength(3)], "does not apply to a int column"),
    ],
)
def test_a_row_declares_its_columns_fully(annotation: object, message: str) -> None:
    with pytest.raises(RowError, match=message):
        type("Bad", (etl.Row,), {"__annotations__": {"value": annotation}})
