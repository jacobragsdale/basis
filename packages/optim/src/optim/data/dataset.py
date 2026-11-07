"""`Dataset`: one table's row schema, source, query, key and how long it may be cached, declared once beside its row
model. A declaration only: importing one opens nothing, so model code can import the row models beside it. The
`Loader` fetches them. On a model's `Data` class, a dataset is a descriptor: the class attribute is the `Dataset`, and
the instance attribute its frame.

Every query takes only the as-of date, as `as_of_date`, and a dataset read per portfolio also takes the batch's
portfolio ids, as `portfolios`. Params never change what is loaded, so a sweep can run every variant from one
snapshot."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Protocol, Self, overload

import pandas as pd

from optim.data.frames import validate_columns
from optim.schema import Row
from optim.sources import Columns, SourceName


class _Frames(Protocol):
    @property
    def frames(self) -> Mapping[str, pd.DataFrame]: ...


@dataclass(frozen=True)
class Dataset:
    """One table: its row schema, where it comes from, its key, and how it is cached."""

    name: str
    """Names its frame and its snapshot file: unique among a model's datasets."""
    row: type[Row]
    source: SourceName
    query: str | Path
    """A SQL file for a database, a path for a REST API, or a file's path or URL for `file`."""
    key: tuple[str, ...]
    """Columns whose values no two rows share."""
    cache: timedelta | None = None
    """How long a cache entry stays fresh; None reads it live on every load. `optim warm` fills every cached dataset."""
    check: Callable[[pd.DataFrame], None] | None = None
    """Rules across rows, which raise `ValueError` on rows that disagree."""

    @overload
    def __get__(self, instance: None, owner: type) -> Self: ...
    @overload
    def __get__(self, instance: _Frames, owner: type) -> pd.DataFrame: ...
    def __get__(self, instance: _Frames | None, owner: type) -> Self | pd.DataFrame:
        return self if instance is None else instance.frames[self.name]

    def file(self, path: str | Path) -> Dataset:
        """The same dataset read from a Parquet or CSV file, for a what-if or a test: `holdings = HOLDINGS.file(...)`."""
        return replace(self, source="file", query=path, cache=None)

    @property
    def per_portfolio(self) -> bool:
        """Read a batch of portfolios at a time, and sliced to each portfolio: its rows have a `portfolio_id`."""
        return "portfolio_id" in self.row.model_fields

    def to_frame(self, columns: Columns) -> pd.DataFrame:
        """Raw columns to this dataset's validated, typed frame, with its key and cross-row rules checked."""
        frame = self.unique(validate_columns(self.row, columns, name=self.name, key=self.key))
        if self.check is not None:
            self.check(frame)
        return frame

    def unique(self, frame: pd.DataFrame, *, why: str = "") -> pd.DataFrame:
        """`frame`, once no two rows share a key."""
        if (duplicated := frame.duplicated(list(self.key))).any():
            examples = frame.loc[duplicated, list(self.key)].head(3).to_dict("records")
            msg = f"{self.name}: duplicate {self.key}{why}, e.g. {examples}"
            raise ValueError(msg)
        return frame
