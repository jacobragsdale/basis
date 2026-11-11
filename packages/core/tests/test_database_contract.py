"""The contract of a database's writer and ledger, run against basis-core's fake database in every run and, in the
integration tier, against SQL Server and Oracle (packages/job/tests/integration/README.md sets them up): a run's changes
apply the same, with the same counts and the same refusals, commit only on `commit()`, and leave the same ledger, so a
test that publishes to the fake sees what production would."""

import threading
import time
import uuid
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Literal

import mssql_python
import oracledb
import pandas as pd
import pytest
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from basis.core import ddl, fakes, frames
from basis.core.databases import Counts, Database, LedgerEntry, open_database
from basis.core.errors import Conflict
from basis.core.interval import Interval
from basis.core.settings import GivenSettings
from basis.core.tables import Changes, Column, Delete, Range

type Dialect = Literal["fake", "fake-oracle", "mssql", "oracle"]
NAME = "warehouse"
DIALECTS = [
    pytest.param("fake", id="fake"),
    pytest.param("fake-oracle", id="fake-oracle"),
    pytest.param("mssql", id="mssql", marks=pytest.mark.integration),
    pytest.param("oracle", id="oracle", marks=pytest.mark.integration),
]
COLUMNS = (
    Column("security_id", "str", nullable=False, max_length=12),
    Column("price", "float", nullable=True),
    Column("note", "str", nullable=True, max_length=40),
    Column("as_of", "date", nullable=True),
    Column("priced_at", "datetime", nullable=True, zone="America/New_York"),
    Column("active", "bool", nullable=True),
)
R1, R2, R3 = (str(uuid.UUID(int=number)) for number in (1, 2, 3))
"""Run ids, as a run's are: the ledger keeps one as CHAR(36), which pads a shorter id, and Oracle then never matches it."""
DAY = Interval(datetime(2026, 11, 5, tzinfo=UTC), datetime(2026, 11, 6, tzinfo=UTC))
AT = datetime(2026, 11, 6, 12, tzinfo=UTC)


class ScratchDatabases(BaseSettings):
    """Where the integration tier may write: scratch databases only, never a shared one."""

    model_config = SettingsConfigDict(env_prefix="BASIS_TEST_", extra="ignore")

    mssql: SecretStr | None = None
    oracle_dsn: str | None = None
    oracle_user: str | None = None
    oracle_password: SecretStr | None = None


@dataclass(frozen=True)
class Table:
    table: str
    database: str = NAME
    key: tuple[str, ...] | None = ("security_id",)
    columns: tuple[Column, ...] = COLUMNS

    def column(self, name: str) -> Column:
        return next(column for column in self.columns if column.name == name)


@dataclass
class Contract:
    """One database the suite runs against, its table and its ledger, made for the test and dropped after."""

    dialect: Dialect
    suffix: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    fake: fakes.Database | None = None
    settings: dict[str, object] = field(default_factory=dict[str, object])
    execute: Callable[..., None] | None = None

    @property
    def target(self) -> Table:
        return Table(f"basis_contract_{self.suffix}")

    @property
    def keys(self) -> Table:
        """A table whose every column is its key."""
        return Table(f"basis_keys_{self.suffix}", columns=COLUMNS[:1])

    @property
    def ledger(self) -> str:
        return f"basis_ledger_{self.suffix}"

    def database(self) -> Database:
        if self.fake is not None:
            return self.fake
        return open_database(GivenSettings(databases={NAME: self.settings}), NAME)

    def rows(self) -> list[tuple[object, ...]]:
        """The table's committed rows, each value as its column's kind reads it, by key."""
        if self.fake is not None:
            found = [tuple(row[column.name] for column in COLUMNS) for row in self.fake.rows(self.target.table)]
        else:
            with self.database().reading() as reader:
                frame = reader.fetch(f"SELECT {', '.join(column.name for column in COLUMNS)} FROM {self.target.table}", None, what="the contract's read")  # noqa: S608 -- the test's own table and columns
            found = [tuple(None if pd.isna(value) else value for value in row) for row in frame.itertuples(index=False)]
        return sorted((tuple(_read(column, value) for column, value in zip(COLUMNS, row, strict=True)) for row in found), key=lambda row: str(row[0]))


def _read(column: Column, value: object) -> object:
    """A value as its column's kind holds it, whatever the driver gave: Oracle's `DATE` is a datetime, and its
    `NUMBER(1)` a number."""
    if value is None:
        return None
    if column.kind == "date" and isinstance(value, datetime):
        return value.date()
    if column.kind == "datetime" and isinstance(value, pd.Timestamp):
        return value.to_pydatetime().replace(tzinfo=None)
    if column.kind == "bool":
        return bool(value)
    if column.kind == "float":
        return float(str(value))
    return value


@pytest.fixture(params=DIALECTS)
def contract(request: pytest.FixtureRequest) -> Generator[Contract]:
    found = Contract(request.param)
    if found.dialect in {"fake", "fake-oracle"}:
        found.fake = fakes.Database(NAME, dialect="oracle" if found.dialect == "fake-oracle" else "mssql")
        found.fake.create(found.target)
        found.fake.create(found.keys)
        yield found
        return
    databases = ScratchDatabases()
    if found.dialect == "mssql":
        if databases.mssql is None:
            pytest.fail("set BASIS_TEST_MSSQL to a connection string for a scratch SQL Server database (see packages/job/tests/integration/README.md)")
        connection_string = databases.mssql.get_secret_value()
        found.settings = {
            "kind": "mssql", "connection_string": connection_string, "isolation": "snapshot", "timeout_s": 60, "max_rows": 1000,
            "write": {"connection_string": connection_string, "ledger": found.ledger},
        }  # fmt: skip
        connect: Callable[[], mssql_python.Connection | oracledb.Connection] = lambda: mssql_python.connect(connection_string, autocommit=False)  # noqa: E731 -- a connection for the DDL
    else:
        if databases.oracle_dsn is None or databases.oracle_user is None or databases.oracle_password is None:
            pytest.fail("set BASIS_TEST_ORACLE_DSN, _USER and _PASSWORD for a scratch Oracle schema (see packages/job/tests/integration/README.md)")
        dsn, user, password = databases.oracle_dsn, databases.oracle_user, databases.oracle_password.get_secret_value()
        found.settings = {
            "kind": "oracle", "dsn": dsn, "user": user, "password": password, "mode": "thin", "isolation": "read_committed", "timeout_s": 60, "max_rows": 1000,
            "write": {"user": user, "password": password, "ledger": found.ledger},
        }  # fmt: skip
        connect = lambda: oracledb.connect(user=user, password=password, dsn=dsn)  # noqa: E731 -- a connection for the DDL
    dialect = "mssql" if found.dialect == "mssql" else "oracle"

    def execute(*statements: str) -> None:
        connection = connect()
        try:
            cursor = connection.cursor()
            for text in statements:
                for part in (piece.strip() for piece in text.split(";\n")):
                    if part:
                        cursor.execute(part.removesuffix(";"))
            connection.commit()
        finally:
            connection.close()

    found.execute = execute
    execute(ddl.create_table(found.target, dialect), ddl.create_table(found.keys, dialect), ddl.ledger(found.ledger, dialect))
    yield found
    for table in (found.target.table, found.keys.table, *ddl.ledger_tables(found.ledger, dialect)):
        execute(f"DROP TABLE {table}")


def frame(*rows: Mapping[str, object]) -> pd.DataFrame:
    """A conformed frame of the table's columns, as a part gives its changes."""
    return pd.DataFrame({column.name: pd.Series([row.get(column.name) for row in rows], dtype=frames.dtype(column)) for column in COLUMNS})


def priced(security_id: str, price: float, *, at: datetime | None = None, note: str | None = None) -> dict[str, object]:
    return {"security_id": security_id, "price": price, "note": note, "as_of": date(2026, 11, 5), "priced_at": at, "active": True}


def apply(contract: Contract, changes: Changes, *, commit: bool = True) -> Counts:
    database = contract.database()
    with database.writing() as writer:
        spec = writer.describe(contract.target.table)
        assert spec is not None
        counts = writer.apply(contract.target, changes, spec)
        if commit:
            writer.commit()
    return counts


def entry(run_id: str, interval: Interval | None = DAY, output: str = "prices") -> LedgerEntry:
    return LedgerEntry(run_id, "prices", output, "basis_contract", interval, rerun_safe=False, counts=Counts(inserted=2), published_at=AT, version="0.0.0")


def test_an_insert_commits_its_rows_and_counts_them(contract: Contract) -> None:
    counts = apply(contract, Changes(insert=frame(priced("A", 1.5), priced("B", 2.25))))
    assert counts == Counts(inserted=2)
    assert [row[:2] for row in contract.rows()] == [("A", 1.5), ("B", 2.25)]


def test_a_writer_that_ends_without_committing_leaves_nothing(contract: Contract) -> None:
    apply(contract, Changes(insert=frame(priced("A", 1.5))), commit=False)
    assert contract.rows() == []


def test_a_writer_whose_block_fails_leaves_nothing(contract: Contract) -> None:
    def failing() -> None:
        with contract.database().writing() as writer:
            spec = writer.describe(contract.target.table)
            assert spec is not None
            writer.apply(contract.target, Changes(insert=frame(priced("A", 1.5))), spec)
            msg = "the run failed"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="the run failed"):
        failing()
    assert contract.rows() == []


def test_a_values_round_trip_as_each_columns_kind_holds_them(contract: Contract) -> None:
    at = datetime(2026, 11, 5, 14, 30, 15, 250_000, tzinfo=UTC)  # 09:30:15.25 in New York
    apply(contract, Changes(insert=frame(priced("A", 1.5, at=at, note="a note"), {"security_id": "B"})))
    assert contract.rows() == [("A", 1.5, "a note", date(2026, 11, 5), datetime(2026, 11, 5, 9, 30, 15, 250_000), True), ("B", None, None, None, None, None)]


def test_an_empty_string_is_kept_except_on_oracle_which_stores_it_as_null(contract: Contract) -> None:
    apply(contract, Changes(insert=frame(priced("A", 1.5, note=""))))
    [row] = contract.rows()
    assert row[2] == (None if contract.dialect in {"oracle", "fake-oracle"} else "")


def test_a_delete_within_a_range_removes_the_rows_in_it_in_its_columns_zone(contract: Contract) -> None:
    inside, before = datetime(2026, 11, 5, 15, tzinfo=UTC), datetime(2026, 11, 5, 3, tzinfo=UTC)  # 10:00 and 22:00 the day before, in New York
    apply(contract, Changes(insert=frame(priced("A", 1.0, at=inside), priced("B", 2.0, at=before), {"security_id": "C"})))
    within = Range("priced_at", datetime(2026, 11, 5, 5, tzinfo=UTC), datetime(2026, 11, 6, 5, tzinfo=UTC))  # the New York day
    counts = apply(contract, Changes(delete=Delete(within=within), insert=frame(priced("D", 4.0, at=inside))))
    assert counts == Counts(deleted=1, inserted=1)
    assert [row[0] for row in contract.rows()] == ["B", "C", "D"]


def test_a_delete_matching_removes_the_rows_whose_columns_match_and_never_a_null(contract: Contract) -> None:
    apply(contract, Changes(insert=frame(priced("A", 1.0, note="x"), priced("B", 2.0, note="y"), priced("C", 3.0))))
    counts = apply(contract, Changes(delete=Delete(matching=frame({"security_id": "A", "note": "x"}, {"security_id": "C", "note": None})[["security_id", "note"]])))
    assert counts == Counts(deleted=1)
    assert [row[0] for row in contract.rows()] == ["B", "C"]


def test_an_upsert_updates_the_keys_it_has_and_inserts_the_rest_counting_both(contract: Contract) -> None:
    apply(contract, Changes(insert=frame(priced("A", 1.0, note="kept"), priced("B", 2.0))))
    counts = apply(contract, Changes(upsert=frame(priced("A", 1.5), priced("C", 3.0))[["security_id", "price"]]))
    assert counts == Counts(upserted=2)
    assert [row[:3] for row in contract.rows()] == [("A", 1.5, "kept"), ("B", 2.0, None), ("C", 3.0, None)]


def test_an_insert_of_new_keys_leaves_the_keys_the_table_has_and_counts_the_new(contract: Contract) -> None:
    apply(contract, Changes(insert=frame(priced("A", 1.0))))
    counts = apply(contract, Changes(insert_new=frame(priced("A", 9.0), priced("B", 2.0))))
    assert counts == Counts(inserted_new=1)
    assert [row[:2] for row in contract.rows()] == [("A", 1.0), ("B", 2.0)]


def test_an_insert_of_a_key_the_table_has_is_a_conflict_and_commits_nothing(contract: Contract) -> None:
    apply(contract, Changes(insert=frame(priced("A", 1.0))))
    with pytest.raises(Conflict):
        apply(contract, Changes(insert=frame(priced("B", 2.0), priced("A", 3.0))))
    assert [row[:2] for row in contract.rows()] == [("A", 1.0)]


def test_the_ledger_says_which_runs_published_and_which_overlap_an_interval(contract: Contract) -> None:
    database = contract.database()
    with database.writing() as writer:
        assert not writer.published(R1)
        assert writer.overlapping("prices", "prices", DAY) == []
        writer.record(entry(R1))
        writer.record(entry(R2, interval=None))
        writer.commit()
    with database.writing() as writer:
        assert (writer.published(R1), writer.published(R2), writer.published(R3)) == (True, True, False)
        assert writer.overlapping("prices", "prices", Interval(datetime(2026, 11, 5, 12, tzinfo=UTC), datetime(2026, 11, 7, tzinfo=UTC))) == [R1]
        assert writer.overlapping("prices", "prices", Interval(datetime(2026, 11, 6, tzinfo=UTC), datetime(2026, 11, 7, tzinfo=UTC))) == []  # adjacent
        assert writer.overlapping("prices", "other", DAY) == []


def test_a_ledger_row_not_committed_is_not_there(contract: Contract) -> None:
    database = contract.database()
    with database.writing() as writer:
        writer.record(entry(R1))
    with database.writing() as writer:
        assert not writer.published(R1)


def test_a_runs_second_ledger_row_for_one_output_is_a_conflict(contract: Contract) -> None:
    with contract.database().writing() as writer:
        writer.record(entry(R1))
        with pytest.raises(Conflict):
            writer.record(entry(R1))


def test_a_ledger_with_no_interval_given_says_which_runs_published_the_output_at_all(contract: Contract) -> None:
    with contract.database().writing() as writer:
        writer.record(entry(R1, interval=None))
        writer.record(entry(R2))
        writer.commit()
    with contract.database().writing() as writer:
        assert writer.overlapping("prices", "prices", None) == [R1, R2]
        assert writer.overlapping("prices", "other", None) == []


def test_an_upsert_of_rows_whose_every_column_is_the_key_inserts_the_new_keys_and_counts_every_row(contract: Contract) -> None:
    keys = contract.keys
    rows = lambda *ids: pd.DataFrame({"security_id": pd.Series(ids, dtype=frames.dtype(COLUMNS[0]))})  # noqa: E731 -- the one column's frame
    with contract.database().writing() as writer:
        spec = writer.describe(keys.table)
        assert spec is not None
        assert writer.apply(keys, Changes(upsert=rows("A")), spec) == Counts(upserted=1)
        writer.commit()
    with contract.database().writing() as writer:
        spec = writer.describe(keys.table)
        assert spec is not None
        assert writer.apply(keys, Changes(upsert=rows("A", "B")), spec) == Counts(upserted=2)
        writer.commit()


def test_two_publishes_of_one_output_take_turns_so_neither_writes_beside_the_other(contract: Contract) -> None:
    """Each deletes the interval's rows and inserts its own. Without the output's lock, the second deletes before the
    first has inserted, and both sets of rows remain; with it, the second waits for the first to commit, and replaces
    its rows."""
    within = Range("priced_at", datetime(2026, 11, 5, 5, tzinfo=UTC), datetime(2026, 11, 6, 5, tzinfo=UTC))
    at = datetime(2026, 11, 5, 15, tzinfo=UTC)
    locked, failures = threading.Barrier(2, timeout=30), list[BaseException]()

    def publish(first: bool, security_id: str) -> None:  # noqa: FBT001 -- which of the two it is
        try:
            with contract.database().writing() as writer:
                if not first:
                    locked.wait()  # the first holds the lock
                writer.lock("prices", "prices")
                if first:
                    locked.wait()
                    time.sleep(0.5)  # the second asks for the lock meanwhile
                spec = writer.describe(contract.target.table)
                assert spec is not None
                writer.apply(contract.target, Changes(delete=Delete(within=within), insert=frame(priced(security_id, 1.0, at=at))), spec)
                writer.commit()
        except BaseException as error:  # noqa: BLE001 -- the thread's failure, which the test raises
            failures.append(error)

    threads = [threading.Thread(target=publish, args=(True, "A")), threading.Thread(target=publish, args=(False, "B"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert failures == []
    assert [row[0] for row in contract.rows()] == ["B"]
