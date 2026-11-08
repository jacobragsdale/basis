"""A dataset route that answers a table, `response=api.Table[Row]`: its handler returns a pandas frame or an Arrow table,
which basis checks once against `Row`'s columns, their names, types, nulls and time zones, never row by row, and
answers as Arrow, or as JSON written from the Arrow (design.md, "APIs"). Since no row is checked by itself, `Row`
declares no value constraints, which `dataset.rows` refuses. This module only declares: `arrow.py` checks and writes
the table, which a service's declarations never load, so `Table`'s own types are read only by a type checker."""

from typing import TYPE_CHECKING, get_args, get_origin

if TYPE_CHECKING:
    import pandas as pd
    import pyarrow as pa

type Table[R] = pa.Table | pd.DataFrame
"""What a handler of a route that answers `api.Table[Row]` returns: a pandas frame or an Arrow table of `Row`'s
columns."""


def row_of(response: object) -> object | None:
    """The row model of `api.Table[Row]`, or None for any other response."""
    if get_origin(response) is Table:
        return get_args(response)[0]
    return None
