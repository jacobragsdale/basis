"""A row's columns as basis-core declares them, for every part: what basis-job's own tests of `etl.Row` and `conform`
do not already hold, which is a column's display format."""

from datetime import date
from typing import Annotated

import pandas as pd
import pytest

from basis.core.display import Display, text
from basis.core.rows import Row, RowError, conform

MONEY = Display(kind="money", currency="USD", decimals=0)


class Holding(Row):
    as_of: date
    account: str
    par: Annotated[float, MONEY]
    weight: Annotated[float, Display(kind="pct", decimals=1)] | None
    price: float


def test_a_float_column_carries_the_display_format_its_row_declares() -> None:
    assert [column.display for column in Holding.columns] == [None, None, MONEY, Display(kind="pct", decimals=1), None]
    assert text(1234567.4, Holding.column("par").display) == "$1,234,567"


def test_a_display_format_changes_nothing_a_frame_holds() -> None:
    frame = pd.DataFrame({"as_of": [date(2026, 11, 13)], "account": ["A1"], "par": [1e6], "weight": [None], "price": [99.5]})
    conformed = conform(frame, Holding, what="holdings", naive="in_zone")
    assert conformed.dtypes.to_dict() == Holding.dtypes()
    assert conformed["par"].tolist() == [1e6]


@pytest.mark.parametrize(
    ("annotation", "message"),
    [
        (Annotated[str, MONEY], r"does not apply to a str column"),
        (Annotated[date, MONEY], r"does not apply to a date column"),
        (Annotated[float, MONEY, Display(kind="number")], r"a column has one display format, not .* and .*: keep one"),
    ],
)
def test_a_display_format_is_one_and_only_on_a_float(annotation: object, message: str) -> None:
    with pytest.raises(RowError, match=message):
        type("Bad", (Row,), {"__annotations__": {"value": annotation}})
