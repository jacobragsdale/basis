"""Loads run concurrently, never more at once than their source's limit, and the first failure stops the rest. A
read-only scope reads one moment of its source, on one connection its loads take turns on. Rows arrive bounded, and
booked values as floats."""

import socket
import threading
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import nullcontext
from datetime import date
from decimal import Decimal  # what a driver returns for a DECIMAL column
from pathlib import Path

import mssql_python
import oracledb
import pytest

from optim.data.dataset import Dataset
from optim.data.loader import Loader, TaskGroup, load_data
from optim.datasets import HOLDINGS, SECURITIES, SQL
from optim.schema import Row
from optim.sources import Columns, Param, TooManyRowsError, mssql, oracle
from optim.sources.file import FileSource
from optim.sources.sql import SqlSource
from optim.testing import frame


class Item(Row):
    item_id: str


class Listed(Row):
    portfolio_id: str


ITEMS = Dataset("items", Item, source="mssql", query=SQL / "holdings.sql", key=("item_id",))  # a real file: SqlSource reads it


class Database:
    """A DB-API driver over `rows`, recording each connection, and how many were open at once."""

    def __init__(self, rows: Sequence[str] = ("i1", "i2")) -> None:
        self.rows, self.connections, self.open, self.most, self.dead = rows, list[Connection](), 0, 0, False
        self.lock = threading.Lock()

    def connect(self) -> Connection:
        with self.lock:
            self.open += 1
            self.most = max(self.most, self.open)
            self.connections.append(connection := Connection(self))
        return connection


class Connection:
    def __init__(self, database: Database) -> None:
        self.database, self.statements, self.rolled_back, self.closed, self.busy = database, list[str](), False, False, False

    def cursor(self) -> Cursor:
        return Cursor(self)

    def rollback(self) -> None:
        self.rolled_back = True
        if self.database.dead:
            msg = "DPY-4011: the database or network closed the connection"
            raise ConnectionError(msg)

    def close(self) -> None:
        self.closed = True
        with self.database.lock:
            self.database.open -= 1


class Cursor:
    arraysize = 1
    description = (("ITEM_ID",),)

    def __init__(self, connection: Connection) -> None:
        self.connection, self.left = connection, [(row,) for row in connection.database.rows]

    def execute(self, statement: str, parameters: dict[str, object] | None = None, /) -> None:  # noqa: ARG002
        assert not self.connection.busy, "two threads used one connection at once"
        self.connection.busy = True
        self.connection.statements.append(statement)
        time.sleep(0.02)
        self.connection.busy = False

    def fetchmany(self, size: int = 1, /) -> list[tuple[str]]:
        batch, self.left = self.left[:size], self.left[size:]
        return batch

    def close(self) -> None:
        pass


def test_loads_in_a_read_only_scope_share_one_transaction_and_take_turns() -> None:
    database = Database()
    loader = Loader({"mssql": SqlSource(database.connect, "SET TRANSACTION READ ONLY", max_rows=100)})

    with loader.read_only("mssql") as source, TaskGroup() as group:
        loads = [group.submit(source.load, ITEMS, batch=n) for n in range(4)]

    assert all(load.result()["item_id"].tolist() == ["i1", "i2"] for load in loads)
    [connection] = database.connections
    assert connection.statements[0] == "SET TRANSACTION READ ONLY"
    assert len(connection.statements) == 5  # begun, then each load
    assert connection.rolled_back
    assert connection.closed


@pytest.mark.parametrize("datasets", [(SECURITIES,), (HOLDINGS,)], ids=["read-whole", "read-per-portfolio"])
def test_a_dataset_whose_source_is_not_configured_names_the_settings_to_set(datasets: tuple[Dataset, ...]) -> None:
    with pytest.raises(LookupError, match=r"not configured: set OPTIM_\w+__\*"):
        load_data(Loader({"file": FileSource()}), {dataset.name: dataset for dataset in datasets}, date(2026, 10, 16), ["P1"])


def test_a_transaction_that_cannot_roll_back_still_raises_the_error_that_ended_it() -> None:
    database = Database()
    database.dead = True  # as after a call timeout
    loader = Loader({"mssql": SqlSource(database.connect, "SET TRANSACTION READ ONLY", max_rows=100)})

    def time_out() -> None:
        with loader.read_only("mssql"):
            msg = "DPY-4024: call timeout of 240000 ms exceeded"
            raise TimeoutError(msg)

    with pytest.raises(TimeoutError, match="call timeout"):
        time_out()

    assert database.connections[0].closed


def test_a_read_only_scope_holds_one_of_its_sources_slots_throughout() -> None:
    database = Database()
    loader = Loader({"mssql": SqlSource(database.connect, "SET TRANSACTION READ ONLY", max_rows=100)}, limits={"mssql": threading.Semaphore(1)})

    def two_reads() -> None:
        with loader.read_only("mssql") as source:
            source.load(ITEMS)
            source.load(ITEMS)

    with TaskGroup() as group:
        for _ in range(3):
            group.submit(two_reads)

    assert len(database.connections) == 3
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


def test_a_query_that_returns_more_than_max_rows_fails() -> None:
    loader = Loader({"mssql": SqlSource(Database(rows=[f"i{n}" for n in range(11)]).connect, "", max_rows=10)})

    with pytest.raises(TooManyRowsError, match="more than 10 rows"):
        loader.load(ITEMS)


def test_a_drivers_decimal_is_a_float_and_a_value_out_of_its_fields_bounds_fails_naming_its_row() -> None:
    holdings = frame(HOLDINGS, {"quantity": Decimal("1234567.891")})  # SQL Server's driver returns DECIMAL columns so

    assert holdings["quantity"].tolist() == [1234567.891]
    assert holdings["quantity"].dtype == "float64"
    with pytest.raises(ValueError, match=r"securities.increment at security_id 'A': Input should be greater than 0"):
        frame(SECURITIES, {"increment": 0.0})


def oracle_settings(mode: str, client_lib_dir: Path) -> oracle.OracleSettings:
    return oracle.OracleSettings.model_validate({"dsn": "db:1521/X", "user": "u", "password": "p", "mode": mode, "client_lib_dir": client_lib_dir, "max_concurrent": 1, "timeout_s": 1, "max_rows": 10})


def test_oracle_in_thick_mode_loads_its_client_libraries_from_the_directory_given(tmp_path: Path) -> None:
    with pytest.raises(oracledb.DatabaseError, match=f"DPI-1047: Cannot locate .*{tmp_path}"):  # an empty directory: no client there
        oracle.connect(oracle_settings("thick", tmp_path))


def test_oracle_in_thin_mode_needs_no_client(tmp_path: Path) -> None:
    assert isinstance(oracle.connect(oracle_settings("thin", tmp_path)), SqlSource)  # connects per query, not here


@pytest.fixture
def hung_database() -> Generator[str]:
    """An address that accepts connections and never answers, as a hung database does."""
    with socket.create_server(("127.0.0.1", 0)) as server:
        yield f"127.0.0.1:{server.getsockname()[1]}"


def test_a_login_the_database_never_answers_ends_at_the_sources_timeout(hung_database: str) -> None:
    """The login waits `timeout_s`, here 1 second, rather than the 30 seconds that are the most a login ever waits.
    Oracle's login is bounded the same way, in a thread the run stops waiting for, which a test cannot tidy away."""
    host, port = hung_database.split(":")
    settings = mssql.MssqlSettings.model_validate({"connection_string": f"Server=tcp:{host},{port};Database=X;UID=u;PWD=p", "max_concurrent": 1, "timeout_s": 1, "max_rows": 10})
    start = time.monotonic()

    with pytest.raises(mssql_python.OperationalError):
        Loader({"mssql": mssql.connect(settings)}).load(HOLDINGS, as_of_date=date(2026, 10, 16), portfolios=("P1",))

    assert time.monotonic() - start < 10


def test_the_portfolios_a_model_lists_load_off_the_main_thread_where_a_stop_signal_reaches_it() -> None:
    """A load in the main thread, inside a driver, would hold off the scheduler's SIGTERM until its query returned."""
    on_main: list[bool] = []

    class Listing:
        def fetch(self, query: str, params: Mapping[str, Param]) -> Columns:  # noqa: ARG002 -- the source's signature
            on_main.append(threading.current_thread() is threading.main_thread())
            return {"portfolio_id": ["P1", "P2"]}

        def read_only(self) -> nullcontext[Listing]:
            return nullcontext(self)

    listing = Dataset("portfolios", Listed, source="mssql", query=SQL / "holdings.sql", key=("portfolio_id",))

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
