"""Parquet or CSV files, by path or URL, through `pyarrow.fs`. A dataset's query is the file's path, and `{as_of_date}` in
it is replaced by the run's date, as in `data/{as_of_date}/holdings.csv`. Swapping one dataset of a model for a file
with the same row schema covers what-ifs, portfolios that are not in the books, small tables no system holds, and test
data. A batch of portfolios reads the file's rows of those portfolios alone.

CSV values are read as text, and validation turns them into the row model's types, so `001234` stays a string where
the model says so. An empty field is null. A file is one moment, so a read-only scope reads the same file."""

import io
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass

import pandas as pd

from optim.data.frames import to_columns
from optim.sources import Columns, Param
from optim.storage import Root


@dataclass(frozen=True)
class FileSource:
    def fetch(self, query: str, params: Mapping[str, Param]) -> Columns:
        file = Root.at(query.format(**params) if "{" in query else query)
        data = io.BytesIO(file.read_bytes())
        frame = pd.read_csv(data, dtype=str, keep_default_na=False, na_values=[""]) if file.name.endswith(".csv") else pd.read_parquet(data)
        if isinstance(portfolios := params.get("portfolios"), tuple):
            frame = frame[frame["portfolio_id"].isin(portfolios)]
        return to_columns(frame)

    def read_only(self) -> nullcontext[FileSource]:
        return nullcontext(self)
