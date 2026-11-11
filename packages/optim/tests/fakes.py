"""Fakes for optim's tests: a source serving rows from memory, which records what it was asked, and a clock that moves
a second at each reading, so a test can run a model end to end, snapshot included, with no database; `Trading`, a
database holding the orders table and its ledger in memory, which a run publishes to; `saved`, a run by its id in a
store; and `Basis`, the type of the `basis` fixture, which runs the command in a model repo. Named apart from
basis-job's `builders`, since the root's pytest run puts both packages' tests folders on the path."""

from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.fs as pafs

from basis.core import fakes
from basis.core.databases import Bind
from basis.core.destination import DestColumn, TableSpec
from basis.core.errors import Rejected
from basis.core.storage import Root
from optim import snapshots
from optim.data.dataset import Dataset
from optim.data.frames import to_columns
from optim.data.loader import Loader
from optim.orders import COLUMNS, Orders, Table
from optim.snapshots import Saved
from optim.sources import Columns, Param
from optim.sources.file import FileSource

EXAMPLE = Path(__file__).parent.parent / "src" / "optim" / "example"

type Basis = Callable[..., int]
"""`basis ARGV...` in a model repo, which returns its exit code: the `basis` fixture's type."""


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


def saved(root: Root, run_id: str) -> Saved:
    """The run `run_id` in the store at `root`."""
    return snapshots.find([root], run_id)


ORDERS_TABLE = "trading.orders"
LEDGER = "trading.publishes"
ORDERS_SPEC = TableSpec(
    "mssql",
    ORDERS_TABLE,
    {column.name: DestColumn(column.name, "FLOAT" if column.kind == "float" else "NVARCHAR", column.max_length, None, None, nullable=False, generated=False, defaulted=False) for column in COLUMNS},
)
"""The orders table as SQL Server describes the one `basis ddl` creates."""


LEDGER_QUERY = f"SELECT deleted, upserted, inserted_new, inserted, published_at FROM {LEDGER} WHERE run_id = %(run_id)s AND output = %(output)s"  # noqa: S608 -- the ledger's name, a constant
"""The one statement optim runs itself: its ledger row, as the database stored it, with times naive UTC."""


def trading(*, spec: TableSpec | None = ORDERS_SPEC, fails: bool = False) -> fakes.Database:
    """The database `trading`, basis-core's fake, with the orders table as `spec` describes it, None where it does not
    exist, and the answer to optim's ledger query. With `fails`, its first insert fails, as a constraint on the table
    would refuse it, so the write commits nothing."""
    found = fakes.Database("trading")
    if spec is not None:
        found.create(Table("trading", ORDERS_TABLE), spec=spec)

    def ledgered(binds: Mapping[str, Bind]) -> pd.DataFrame:
        rows = [vars(entry.counts) | {"published_at": entry.published_at.replace(tzinfo=None)} for entry in found.entries if (entry.run_id, entry.output) == (binds["run_id"], binds["output"])]
        return pd.DataFrame(rows, columns=["deleted", "upserted", "inserted_new", "inserted", "published_at"])

    found.answers[LEDGER_QUERY] = ledgered
    if fails:
        found.fails(Rejected, on="apply", times=1)
    return found


def publishing_to(database: fakes.Database) -> Orders:
    """Orders published to `database`'s orders table."""

    def databases(name: str) -> fakes.Database:
        assert name == database.name
        return database

    return Orders(Table(database.name, ORDERS_TABLE), databases, "mssql", LEDGER, timeout_s=30)


class ReadsRecorded(pafs.FileSystemHandler):
    """The local filesystem, recording each file opened to read: a run store whose reads a test can see, as
    `Root(pafs.PyFileSystem(handler), PATH, PATH)`."""

    def __init__(self) -> None:
        self.local, self.read = pafs.LocalFileSystem(), list[str]()

    def get_type_name(self) -> str:
        return "reads-recorded"

    def normalize_path(self, path: str) -> str:
        return path

    def get_file_info(self, paths: list[str]) -> list[pafs.FileInfo]:
        return self.local.get_file_info(paths)

    def get_file_info_selector(self, selector: pafs.FileSelector) -> list[pafs.FileInfo]:
        return self.local.get_file_info(selector)

    def create_dir(self, path: str, recursive: bool) -> None:  # noqa: FBT001 -- pyarrow's signature
        self.local.create_dir(path, recursive=recursive)

    def delete_dir(self, path: str) -> None:
        self.local.delete_dir(path)

    def delete_dir_contents(self, path: str, missing_dir_ok: bool = False) -> None:  # noqa: FBT001, FBT002 -- pyarrow's signature
        self.local.delete_dir_contents(path, missing_dir_ok=missing_dir_ok)

    def delete_root_dir_contents(self) -> None:
        raise NotImplementedError

    def delete_file(self, path: str) -> None:
        self.local.delete_file(path)

    def move(self, src: str, dest: str) -> None:
        self.local.move(src, dest)

    def copy_file(self, src: str, dest: str) -> None:
        self.local.copy_file(src, dest)

    def open_input_stream(self, path: str) -> pa.NativeFile:
        self.read.append(path)
        return self.local.open_input_stream(path)

    def open_input_file(self, path: str) -> pa.NativeFile:
        self.read.append(path)
        return self.local.open_input_file(path)

    def open_output_stream(self, path: str, metadata: dict[str, str]) -> pa.NativeFile:
        return self.local.open_output_stream(path, metadata=metadata)

    def open_append_stream(self, path: str, metadata: dict[str, str]) -> pa.NativeFile:
        return self.local.open_append_stream(path, metadata=metadata)
