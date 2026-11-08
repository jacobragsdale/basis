"""A dataset route that answers a table, `response=api.Table[Row]`: its handler returns a pandas frame or an Arrow table,
which basis checks once against `Row`'s columns, their names, types, nulls and time zones, never row by row, and
answers as Arrow, or as JSON written from the Arrow (design.md, "APIs"). Since no row is checked by itself, `Row`
declares no value constraints, which `dataset.rows` refuses. This module only declares: `arrow.py` checks and writes
the table, which a service's declarations never load, so `Table`'s own types are read only by a type checker."""

from types import UnionType
from typing import TYPE_CHECKING, Union, get_args, get_origin

if TYPE_CHECKING:
    import pandas as pd
    import pyarrow as pa

type Table[R] = pa.Table | pd.DataFrame
"""What a handler of a route that answers `api.Table[Row]` returns: a pandas frame or an Arrow table of `Row`'s
columns."""


_FRAMES = {("pandas", "DataFrame"), ("pyarrow", "Table")}
"""A pandas frame and an Arrow table, by their packages and names, so a declaration need import neither."""


def is_frame_or_table(annotation: object) -> bool:
    """Whether a handler's return annotation is `pd.DataFrame`, `pa.Table`, or a union of them, which a route that
    answers `api.Table[Row]` takes as it takes `api.Table[Row]` itself."""
    if get_origin(annotation) in {Union, UnionType}:
        return all(is_frame_or_table(argument) for argument in get_args(annotation))
    module, name = getattr(annotation, "__module__", ""), getattr(annotation, "__name__", "")
    return isinstance(module, str) and (module.partition(".")[0], name) in _FRAMES


def row_of(response: object) -> object | None:
    """The row model of `api.Table[Row]`, or None for any other response."""
    if get_origin(response) is Table:
        return get_args(response)[0]
    return None
