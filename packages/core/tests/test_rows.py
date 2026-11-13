"""A row's columns as basis-core declares them, for every part: what basis-job's own tests of `etl.Row` and `conform`
do not already hold, which is a column's display format."""

from datetime import date, datetime
from typing import Annotated

import pandas as pd
import pytest

from basis.core.display import Display, text
from basis.core.rows import Json, Row, RowError, SchemaError, Zone, conform

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


class Spelled(Row, spelling="xy"):
    """A part's base row, as a part's code imports the part as `xy`."""


def test_a_refusal_names_the_fix_as_the_part_that_declared_the_row_spells_it() -> None:
    with pytest.raises(RowError, match=r"as Annotated\[datetime, xy\.Zone\('UTC'\)\]"):
        type("Unzoned", (Spelled,), {"__annotations__": {"at": datetime}})

    class Zoned(Spelled):
        at: Annotated[datetime, Zone("America/New_York")]
        terms: Annotated[str, Json()]

    fall_back = pd.DataFrame({"at": [datetime(2026, 11, 1, 1, 30)], "terms": ["{}"]})  # a time New York shows twice
    with pytest.raises(SchemaError, match=r"as xy\.Zone\('America/New_York', ambiguous='earliest'\)"):
        conform(fall_back, Zoned, what="positions", naive="in_zone")
    not_json = pd.DataFrame({"at": [datetime(2026, 11, 2, 12)], "terms": ["7"]})
    with pytest.raises(SchemaError, match=r"write it with xy\.json_text"):
        conform(not_json, Zoned, what="positions", naive="in_zone")


def test_a_row_of_cores_own_spells_cores_names_and_a_base_row_declares_no_columns() -> None:
    with pytest.raises(RowError, match=r"basis\.core\.rows\.Zone\('UTC'\)"):
        type("Unzoned", (Row,), {"__annotations__": {"at": datetime}})
    with pytest.raises(RowError, match="gives its spelling alone"):
        type("Both", (Row,), {"__annotations__": {"at": float}}, spelling="xy")
