"""Where rows come from: Oracle, SQL Server, REST APIs and files. Every source answers `fetch(query, params)` with raw
columns, and `optim.data` validates them,
so all sources are held to the same schemas. `read_only()` scopes several fetches to one read-only transaction, so reads
that depend on each other see one moment of the source. `optim.context` opens the connections."""

from collections.abc import Mapping
from contextlib import AbstractContextManager
from datetime import date
from typing import Annotated, Literal, Protocol

from pydantic import ConfigDict, Field

from optim.schema import Model

type SourceName = Literal["oracle", "mssql", "rest", "file"]
type Param = str | int | float | date | tuple[str, ...]
type Columns = dict[str, list[object]]


class SourceSettings(Model):
    """What every source's settings carry: how hard a run may press it, how long it may take to answer, and how much it
    may return."""

    model_config = ConfigDict(hide_input_in_errors=True)  # an error would otherwise print the password beside the field it is missing

    max_concurrent: Annotated[int, Field(ge=1)]  # calls this process has open to the source at once; the loader holds the rest
    timeout_s: Annotated[int, Field(ge=1)]  # deadline on every call that crosses the network
    max_rows: Annotated[int, Field(ge=1)]  # a call that returns more fails, rather than filling the Job's memory


class Source(Protocol):
    def fetch(self, query: str, params: Mapping[str, Param]) -> Columns: ...

    def read_only(self) -> AbstractContextManager[Source]:
        """A source whose fetches share one read-only transaction until the block ends. Its fetches may come from several
        threads: it takes them one at a time."""
        ...


class TooManyRowsError(ValueError):
    def __init__(self, query: str, max_rows: int) -> None:
        super().__init__(f"{query} returned more than {max_rows} rows, the source's max_rows: is a filter missing, or does the limit need raising?")
