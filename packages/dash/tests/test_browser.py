"""`bd.rows`: a frame's rows as the browser takes them, every value one JSON holds, as Dash's own encoder writes it."""

from datetime import date, datetime
from typing import Annotated

import numpy as np
import orjson
import pandas as pd
import pytest

import basis.dash as bd
from basis.core.rows import conform


class Holding(bd.Row):
    cusip: str | None
    par: float | None
    held: bool
    as_of: date
    priced_at: Annotated[datetime, bd.Zone("UTC")]


def _holdings() -> pd.DataFrame:
    given = pd.DataFrame(
        {
            "cusip": ["001234", None],
            "par": [1_000_000.5, np.nan],
            "held": [True, False],
            "as_of": [date(2026, 11, 13), date(2026, 11, 12)],
            "priced_at": pd.to_datetime(["2026-11-13T14:05:00.123456Z", "2026-11-13T15:00:00.000000Z"]),
        }
    )
    return conform(given, Holding, what="holdings", naive="reject")


def test_rows_are_json_values_with_nan_as_null_and_dates_as_iso_text() -> None:
    shown = bd.rows(_holdings())
    assert shown == [
        {"cusip": "001234", "par": 1_000_000.5, "held": True, "as_of": "2026-11-13", "priced_at": "2026-11-13T14:05:00.123456Z"},
        {"cusip": None, "par": None, "held": False, "as_of": "2026-11-12", "priced_at": "2026-11-13T15:00:00.000000Z"},
    ]
    assert orjson.loads(orjson.dumps(shown)) == shown


def test_a_frame_a_page_made_itself_becomes_rows_too() -> None:
    frame = pd.DataFrame({"sector": ["rates", "credit"], "count": [3, 4], "weight": [np.inf, -np.inf]}, index=[7, 9])
    assert bd.rows(frame) == [{"sector": "rates", "count": 3, "weight": None}, {"sector": "credit", "count": 4, "weight": None}]
    assert bd.rows(frame.iloc[0:0]) == []


def test_what_bd_rows_cannot_make_rows_of_is_refused() -> None:
    with pytest.raises(TypeError, match="takes a pandas frame, not a list"):
        bd.rows([{"a": 1}])  # ty: ignore[invalid-argument-type] -- not a frame, on purpose
    with pytest.raises(ValueError, match="columns each have a name of their own"):
        bd.rows(pd.DataFrame([[1, 2]], columns=["a", "a"]))
    with pytest.raises(ValueError, match="takes times with their zone"):
        bd.rows(pd.DataFrame({"at": [datetime(2026, 11, 13, 14, 5)]}))
