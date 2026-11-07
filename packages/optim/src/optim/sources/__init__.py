"""Where rows come from: databases, through basis-core's dialects, REST APIs and files. Every source answers
`fetch(query, params)` with raw columns, and `optim.data` validates them, so all sources are held to the same schemas.
`read_only()` scopes several fetches to one transaction that only reads, so reads that depend on each other see one
moment of the source. `optim.context` opens them.

A source is named by a dataset's `source`: a database's name, as `BASIS_DATABASES__NAME__*` configures it, or one of
optim's own, `rest` and `file`, which no database may take."""

from collections.abc import Mapping
from contextlib import AbstractContextManager
from datetime import date
from typing import Literal, Protocol

type SourceName = str
"""A database's name, lower-case letters, digits and underscores, or one of `OWN`."""
type Param = str | int | float | date | tuple[str, ...]
type Columns = dict[str, list[object]]

OWN: tuple[Literal["rest", "file"], ...] = ("rest", "file")
"""The sources that are optim's own, rather than a database's: the REST API, and files."""


class Source(Protocol):
    def fetch(self, query: str, params: Mapping[str, Param]) -> Columns: ...

    def read_only(self) -> AbstractContextManager[Source]:
        """A source whose fetches share one transaction that only reads, until the block ends. Its fetches may come
        from several threads: it takes them one at a time."""
        ...


def is_database(source: SourceName) -> bool:
    """Whether a dataset's source is a database, whose query is a SQL file, rather than one of optim's own."""
    return source not in OWN


class TooManyRowsError(ValueError):
    """A REST call that returned more than its source's `max_rows`; a database's reads raise basis-core's."""

    def __init__(self, query: str, max_rows: int) -> None:
        super().__init__(f"{query} returned more than {max_rows} rows, the source's max_rows: is a filter missing, or does the limit need raising?")
