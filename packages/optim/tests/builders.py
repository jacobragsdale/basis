"""Helpers for the core's tests: a source serving rows from memory, which records what it was asked, and a clock that
moves a second at each reading, so a test can run a model end to end, snapshot included, with no database."""

from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from optim.data.dataset import Dataset
from optim.data.frames import to_columns
from optim.data.loader import Loader
from optim.sources import Columns, Param
from optim.sources.file import FileSource

EXAMPLE = Path(__file__).parent.parent / "src" / "optim" / "example"


class Rows:
    """A source that answers each dataset's query with its frame, and a batch of portfolios with their rows alone. It
    records each fetch's params, and how many read-only transactions were opened."""

    def __init__(self, frames: Mapping[Dataset, pd.DataFrame]) -> None:
        self.frames = {str(dataset.query): frame for dataset, frame in frames.items()}
        self.fetches: list[tuple[str, Mapping[str, Param]]] = []
        self.transactions = 0

    def fetch(self, query: str, params: Mapping[str, Param]) -> Columns:
        self.fetches.append((query, params))
        frame = self.frames[query]
        if isinstance(ids := params.get("portfolios"), tuple):
            frame = frame[frame["portfolio_id"].isin(ids)]
        return to_columns(frame)

    @contextmanager
    def read_only(self) -> Generator[Rows]:
        self.transactions += 1
        yield self


def loader(source: Rows, *, clock: Clock | None = None) -> Loader:
    """A loader whose databases and API are `source`, and which reads files as a run does."""
    return Loader({"oracle": source, "mssql": source, "rest": source, "file": FileSource()}, clock=clock or Clock())


class Clock:
    """A clock a second later at each reading, from the business date's 9 am in New York."""

    def __init__(self) -> None:
        self._ticks: Iterator[int] = iter(range(10**6))

    def __call__(self) -> datetime:
        return datetime(2026, 10, 16, 13, tzinfo=UTC) + timedelta(seconds=next(self._ticks))
