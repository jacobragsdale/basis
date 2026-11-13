"""`bd.rows(frame)`: a frame's rows as the browser takes them, AG Grid's `rowData` among them, each a dict of JSON's
values:

| A column of | Becomes |
|---|---|
| floats | numbers, NaN and ±inf `null` |
| text, booleans, whole numbers | themselves, a missing value `null` |
| dates | ISO 8601 text, `2026-11-13` |
| times | ISO 8601 text in UTC, `2026-11-13T14:05:00.123456Z` |

Built a column at a time through Arrow, which reads each column once, and the rows made by zipping them: 19 ms for
10,000 rows of 20 columns and 192 ms for 100,000, where pandas' `to_dict("records")` takes 46 and 458 and leaves NaN
and dates as JSON cannot hold them (measured in step 15.8; spike 14.1, question 11). The frame's index is left out; a
page that wants it resets it first."""

from collections.abc import Sequence
from typing import Any, cast

import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

type Rows = list[dict[str, object]]
"""Rows for the browser, as `bd.rows` makes them."""

__all__ = ["Rows", "rows"]


def rows(frame: pd.DataFrame) -> Rows:
    """`frame`'s rows, each a dict by column, of JSON's values."""
    if not isinstance(frame, pd.DataFrame):
        msg = f"bd.rows takes a pandas frame, not a {type(frame).__qualname__}: load one with bd.load"
        raise TypeError(msg)
    names = [str(name) for name in frame.columns]
    if len(set(names)) != len(names):
        msg = f"bd.rows takes a frame whose columns each have a name of their own, not {names}: rename the repeated ones"
        raise ValueError(msg)
    table = pa.Table.from_pandas(frame, preserve_index=False)
    columns: Sequence[list[object]] = [_values(column) for column in table.columns]
    return [dict(zip(names, values, strict=True)) for values in zip(*columns, strict=True)]


def _values(column: pa.ChunkedArray) -> list[object]:
    kind = column.type
    if pa.types.is_floating(kind):
        column = _call("if_else", _call("is_finite", column), column, pa.scalar(None, kind))
    elif pa.types.is_date(kind):
        column = pc.cast(column, pa.string())
    elif pa.types.is_timestamp(kind):
        utc = column if kind.tz in {"UTC", "+00:00"} else pc.cast(column, pa.timestamp(kind.unit, "UTC")) if kind.tz is not None else None
        if utc is None:
            msg = "bd.rows takes times with their zone, as a conformed frame's are: localize the column first"
            raise ValueError(msg)
        column = _call("strftime", utc, options=pc.StrftimeOptions("%Y-%m-%dT%H:%M:%SZ"))
    return cast("list[object]", column.to_pylist())  # pyarrow is untyped


def _call(function: str, *args: object, options: object = None) -> Any:  # noqa: ANN401 -- pyarrow's compute functions are untyped
    return pc.call_function(function, list(args), options)
