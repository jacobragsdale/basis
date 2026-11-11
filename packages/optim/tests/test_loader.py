"""Loads run concurrently, never more at once than their source's limit, and the first failure stops the rest. A
read-only scope reads one moment of its source, in one transaction its loads take turns on, a batch's ids bound as one
JSON array. Booked values arrive as floats. How basis-core's dialects bound each read's time and rows is its own tests'."""

import threading
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import date
from decimal import Decimal  # what a driver returns for a DECIMAL column
from pathlib import Path

import pyarrow as pa
import pytest

from basis.core.databases import Bind
from optim.data.dataset import Dataset
from optim.data.loader import EmptyDatasetError, Loader, TaskGroup, load_data
from optim.datasets import HOLDINGS, SECURITIES, SQL
from optim.schema import Row
from optim.sources import Columns, Param
from optim.sources.database import DatabaseSource, read_sql
from optim.sources.file import FileSource
from optim.testing import frame


class Item(Row):
    item_id: str


class Listed(Row):
    portfolio_id: str


ITEMS = Dataset("items", Item, source="mssql", query=SQL / "holdings.sql", key=("item_id",), empty="allow")  # a real file: a database source reads it


class Database:
    """A database whose reads return `rows`, recording each transaction it opens, and how many were open at once."""

    name = "mssql"

    def __init__(self, rows: Sequence[str] = ("i1", "i2")) -> None:
        self.rows, self.transactions, self.open, self.most = rows, list[Transaction](), 0, 0
        self.lock = threading.Lock()

    @contextmanager
    def statements(self, *, writes: bool) -> Generator[Transaction]:
        assert not writes, "a source reads through the read login alone"
        with self.lock:
            self.open += 1
            self.most = max(self.most, self.open)
            self.transactions.append(transaction := Transaction(self))
        try:
            yield transaction
        finally:
            with self.lock:
                self.open -= 1


class Transaction:
    def __init__(self, database: Database) -> None:
        self.database, self.queries, self.busy = database, list[tuple[str, Mapping[str, Bind]]](), False

    def query(self, sql: str, binds: Mapping[str, Bind], *, timeout_s: float) -> pa.Table:
        assert not self.busy, "two threads used one connection at once"
        assert timeout_s == 60
        self.busy = True
        self.queries.append((sql, binds))
        time.sleep(0.02)
        self.busy = False
        return pa.table({"item_id": list(self.database.rows)})

    def execute(self, sql: str, binds: Mapping[str, Bind], *, timeout_s: float) -> int:
        raise NotImplementedError

    def commit(self) -> None:
        raise NotImplementedError


def test_loads_in_a_read_only_scope_share_one_transaction_and_take_turns() -> None:
    database = Database()
    loader = Loader({"mssql": DatabaseSource(database, timeout_s=60)})

    with loader.read_only("mssql") as source, TaskGroup() as group:
        loads = [group.submit(source.load, ITEMS, as_of_date=date(2026, 10, 16), portfolios=(f"P{n}",)) for n in range(4)]

    assert all(load.result()["item_id"].tolist() == ["i1", "i2"] for load in loads)
    [transaction] = database.transactions
    assert len(transaction.queries) == 4
    assert {binds["portfolios"] for _, binds in transaction.queries} == {f'["P{n}"]' for n in range(4)}  # a batch's ids bind as one JSON array
    assert all(sql == read_sql(str(ITEMS.query)) for sql, _ in transaction.queries)


@pytest.mark.parametrize("datasets", [(SECURITIES,), (HOLDINGS,)], ids=["read-whole", "read-per-portfolio"])
def test_a_dataset_whose_source_is_not_configured_names_the_settings_to_set(datasets: tuple[Dataset, ...]) -> None:
    with pytest.raises(LookupError, match=r"not configured: set BASIS_DATABASES__\w+__\* and BASIS_OPTIM__DATABASES__\w+__MAX_CONCURRENT"):
        load_data(Loader({"file": FileSource()}), {dataset.name: dataset for dataset in datasets}, date(2026, 10, 16), ["P1"])


def test_a_read_only_scope_holds_one_of_its_sources_slots_throughout() -> None:
    database = Database()
    loader = Loader({"mssql": DatabaseSource(database, timeout_s=60)}, limits={"mssql": threading.Semaphore(1)})

    def two_reads() -> None:
        with loader.read_only("mssql") as source:
            source.load(ITEMS)
            source.load(ITEMS)

    with TaskGroup() as group:
        for _ in range(3):
            group.submit(two_reads)

    assert len(database.transactions) == 3
    assert database.most == 1


class Failing:
    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, query: str, params: Mapping[str, Param]) -> Columns:  # noqa: ARG002
        self.calls += 1
        time.sleep(0.05)
        msg = "bad batch"
        raise RuntimeError(msg)

    def read_only(self) -> nullcontext[Failing]:
        return nullcontext(self)


def test_a_failed_load_skips_the_loads_still_waiting() -> None:
    source = Failing()
    loader = Loader({"mssql": source}, limits={"mssql": threading.Semaphore(1)})

    def ten_loads() -> None:
        with TaskGroup() as group:
            for n in range(10):
                group.submit(loader.load, ITEMS, batch=n)

    with pytest.raises(RuntimeError, match="bad batch"):
        ten_loads()

    assert source.calls == 1


def test_a_drivers_decimal_is_a_float_and_a_value_out_of_its_fields_bounds_fails_naming_its_row() -> None:
    holdings = frame(HOLDINGS, {"quantity": Decimal("1234567.891")})  # SQL Server's driver returns DECIMAL columns so

    assert holdings["quantity"].tolist() == [1234567.891]
    assert holdings["quantity"].dtype == "float64"
    with pytest.raises(ValueError, match=r"securities.increment at security_id 'A': Input should be greater than 0"):
        frame(SECURITIES, {"increment": 0.0})


def test_the_portfolios_a_model_lists_load_off_the_main_thread_where_a_stop_signal_reaches_it() -> None:
    """A load in the main thread, inside a driver, would hold off the scheduler's SIGTERM until its query returned."""
    on_main: list[bool] = []

    class Listing:
        def fetch(self, query: str, params: Mapping[str, Param]) -> Columns:  # noqa: ARG002 -- the source's signature
            on_main.append(threading.current_thread() is threading.main_thread())
            return {"portfolio_id": ["P1", "P2"]}

        def read_only(self) -> nullcontext[Listing]:
            return nullcontext(self)

    listing = Dataset("portfolios", Listed, source="mssql", query=SQL / "holdings.sql", key=("portfolio_id",), empty="fail")

    _, _, portfolio_ids = load_data(Loader({"mssql": Listing()}), {"portfolios": listing}, date(2026, 10, 16), None)

    assert portfolio_ids == ["P1", "P2"]
    assert on_main == [False]


def test_a_file_dataset_reads_parquet_or_csv_for_the_run_date_and_a_batch_reads_its_portfolios_rows(tmp_path: Path) -> None:
    """A CSV's values are text until validation types them, so an id of digits stays a string."""
    day = tmp_path / "2026-10-16"
    day.mkdir()
    (day / "holdings.csv").write_text("portfolio_id,security_id,quantity\n001,USD,1000000\n002,USD,5.5\n")
    frame(HOLDINGS, {"portfolio_id": "001"}, {"portfolio_id": "002"}).to_parquet(day / "holdings.parquet", index=False)
    loader = Loader({"file": FileSource()})

    for suffix in ("csv", "parquet"):
        dataset = HOLDINGS.file(f"{tmp_path}/{{as_of_date}}/holdings.{suffix}")
        holdings = loader.load(dataset, as_of_date=date(2026, 10, 16), portfolios=("002",))
        assert holdings["portfolio_id"].tolist() == ["002"]
        assert holdings["quantity"].dtype == "float64"


def test_a_dataset_that_returns_no_rows_fails_the_load_unless_it_allows_it(tmp_path: Path) -> None:
    """An empty feed that has not landed and a day with nothing in it look the same, so each dataset says which it may
    be: a model that decided on no signals would trade on none."""
    (tmp_path / "items.csv").write_text("item_id\n")
    frame(HOLDINGS, {"portfolio_id": "P1"}).to_csv(tmp_path / "holdings.csv", index=False)
    loader = Loader({"file": FileSource()})
    items = Dataset("items", Item, source="file", query=tmp_path / "items.csv", key=("item_id",), empty="fail")
    datasets = {"holdings": HOLDINGS.file(tmp_path / "holdings.csv"), "items": items}

    with pytest.raises(EmptyDatasetError, match=r"items returned no rows for 2026-10-16, which its dataset does not allow \(empty='fail'\): has the data landed\?"):
        load_data(loader, datasets, date(2026, 10, 16), ["P1"])
    frames, _, _ = load_data(loader, datasets | {"items": replace(items, empty="allow")}, date(2026, 10, 16), ["P1"])

    assert frames["items"].empty
