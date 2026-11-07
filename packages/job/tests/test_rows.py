from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated

import polars as pl
import pytest
from builders import Price

import basis.job as etl
from basis.job.rows import RowError, SchemaError, conform


def problems(frame: pl.DataFrame, *, naive: str = "in_zone") -> list[str]:
    with pytest.raises(SchemaError) as raised:
        conform(frame, Price, what="prices", naive="reject" if naive == "reject" else "in_zone")
    return raised.value.problems


def test_a_csvs_text_converts_to_every_column_type() -> None:
    text = pl.DataFrame({"DAY": ["2026-11-05"], "security_id": ["S1"], "price": ["100.25"], "note": [None], "booked_at": ["2026-11-05T09:30:00"]}, schema_overrides={"note": pl.String})
    row = conform(text, Price, what="prices", naive="in_zone").row(0, named=True)
    assert row == {"day": date(2026, 11, 5), "security_id": "S1", "price": Decimal("100.2500"), "note": None, "booked_at": datetime(2026, 11, 5, 14, 30, tzinfo=UTC)}


def test_an_oracle_date_and_number_convert_when_nothing_is_lost() -> None:
    oracle = pl.DataFrame(
        {"day": [datetime(2026, 11, 5)], "security_id": ["S1"], "price": [Decimal(100)], "note": [None], "booked_at": [None]},
        schema_overrides={"price": pl.Decimal(38, 0), "note": pl.String, "booked_at": pl.Datetime("ms")},
    )
    assert conform(oracle, Price, what="prices", naive="in_zone").schema == Price.schema()


@pytest.mark.parametrize(
    ("column", "value", "dtype", "problem"),
    [
        ("day", datetime(2026, 11, 5, 9), pl.Datetime("us"), "datetimes with a time of day are not dates"),
        ("price", Decimal("1.23456"), pl.Decimal(38, 5), "1 of 1 values change when stored as Decimal(precision=18, scale=4)"),
        ("price", 1.5, pl.Float64, "Float64 is inexact"),
        ("price", "1.23456", pl.String, "at most 4 decimal places"),
        ("security_id", "S123456789", pl.String, "longer than its MaxLength(8)"),
        ("security_id", None, pl.String, "1 nulls in a column that may not be null"),
        ("security_id", 12, pl.Int64, "Int64 cannot become String"),
    ],
)
def test_a_value_that_would_change_or_does_not_fit_is_refused(column: str, value: object, dtype: pl.DataType, problem: str) -> None:
    frame = pl.DataFrame({"day": [date(2026, 11, 5)], "security_id": ["S1"], "price": [Decimal(1)], "note": [None], "booked_at": [None]}, schema_overrides={"note": pl.String})
    frame = frame.with_columns(pl.Series(column, [value], dtype=dtype))
    assert any(problem in found for found in problems(frame))


def test_a_naive_datetime_in_an_output_is_refused() -> None:
    frame = pl.DataFrame({"day": [date(2026, 11, 5)], "security_id": ["S1"], "price": [Decimal(1)], "note": ["x"], "booked_at": [datetime(2026, 11, 5, 9)]})
    assert problems(frame, naive="reject") == ["booked_at: naive datetimes: give them a zone, as .dt.replace_time_zone('America/New_York'), or convert them to UTC"]


def test_missing_and_extra_columns_are_named() -> None:
    frame = pl.DataFrame({"day": [date(2026, 11, 5)], "security_id": ["S1"], "extra": [1]})
    assert problems(frame) == ["columns the row does not declare: extra", "missing columns: price, note, booked_at"]


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
