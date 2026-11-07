"""basis-core's publishing, on databases of the test's own: what any part that publishes gets, whatever it writes. The
integration tier publishes to real SQL Server and Oracle; these hold the rules without them."""

from collections.abc import Generator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

import polars as pl
import pytest

from basis.core.databases import Counts, LedgerEntry, Reader, Statements, Writer
from basis.core.destination import DestColumn, TableSpec
from basis.core.interval import Interval
from basis.core.publish import Publication, publish, refused_overlap
from basis.core.snapshots import PublishRecord
from basis.core.tables import Changes, Column, Target

AT = datetime(2026, 11, 6, 6, tzinfo=UTC)
DAY = Interval(datetime(2026, 11, 5, tzinfo=UTC), datetime(2026, 11, 6, tzinfo=UTC))
COLUMNS = (Column("security_id", "str", nullable=False, dtype=pl.String(), max_length=12), Column("price", "float", nullable=False, dtype=pl.Float64()))
SPEC = {name: DestColumn(name, type_, length, None, None, nullable=False, generated=False, defaulted=False) for name, type_, length in [("security_id", "NVARCHAR", 12), ("price", "FLOAT", None)]}


@dataclass(frozen=True)
class Table:
    table: str
    database: str
    key: tuple[str, ...] | None = None
    columns: tuple[Column, ...] = COLUMNS

    def column(self, name: str) -> Column:
        return next(column for column in self.columns if column.name == name)


@dataclass
class Ledger:
    """One database: its tables, which exist when they have a spec, and its ledger, as committed."""

    name: str
    specs: dict[str, TableSpec] = field(default_factory=dict[str, TableSpec])
    rows: dict[str, list[pl.DataFrame]] = field(default_factory=dict[str, list[pl.DataFrame]])
    entries: list[LedgerEntry] = field(default_factory=list[LedgerEntry])


@dataclass
class FakeWriter:
    ledger: Ledger
    rows: dict[str, list[pl.DataFrame]] = field(default_factory=dict[str, list[pl.DataFrame]])
    entries: list[LedgerEntry] = field(default_factory=list[LedgerEntry])

    def fetch(self, sql: str, interval: Interval | None, *, what: str) -> pl.DataFrame:
        raise NotImplementedError

    def describe(self, table: str) -> TableSpec | None:
        return self.ledger.specs.get(table)

    def published(self, run_id: str) -> bool:
        return any(entry.run_id == run_id for entry in self.ledger.entries)

    def overlapping(self, job: str, output: str, interval: Interval) -> list[str]:
        return [e.run_id for e in self.ledger.entries if (e.job, e.output) == (job, output) and e.interval is not None and e.interval.start < interval.end and interval.start < e.interval.end]

    def apply(self, target: Target, changes: Changes, spec: TableSpec) -> Counts:  # noqa: ARG002 -- the Writer protocol's signature
        assert changes.insert is not None
        self.rows.setdefault(target.table, []).append(changes.insert)
        return Counts(inserted=changes.insert.height)

    def record(self, entry: LedgerEntry) -> None:
        self.entries.append(entry)

    def commit(self) -> None:
        for table, frames in self.rows.items():
            self.ledger.rows.setdefault(table, []).extend(frames)
        self.ledger.entries.extend(self.entries)


@dataclass(frozen=True)
class FakeDatabase:
    ledger: Ledger

    @property
    def name(self) -> str:
        return self.ledger.name

    @property
    def writable(self) -> bool:
        return True

    def reading(self) -> AbstractContextManager[Reader]:
        raise NotImplementedError

    @contextmanager
    def writing(self) -> Generator[Writer]:
        yield FakeWriter(self.ledger)  # what it holds commits only on commit(), as a transaction's does

    def statements(self, *, writes: bool) -> AbstractContextManager[Statements]:
        raise NotImplementedError


def databases(*names: str, missing: str | None = None) -> dict[str, Ledger]:
    return {name: Ledger(name, {} if f"{name}.prices" == missing else {f"{name}.prices": TableSpec("mssql", f"{name}.prices", SPEC)}) for name in names}


def run(found: Mapping[str, Ledger], *, run_id: str = "R1", again: bool = False, insert: pl.DataFrame | None = None) -> dict[str, PublishRecord]:
    targets = {f"{name}_prices": Table(f"{name}.prices", name) for name in found}
    frame = insert if insert is not None else pl.DataFrame({"security_id": ["A", "B"], "price": [1.5, 2.0]})
    changes = {output: Changes(insert=frame) for output in targets}
    return publish(Publication(run_id, "prices", DAY, "0.0.0", again, lambda: AT), targets, changes, lambda name: FakeDatabase(found[name]))


def test_each_database_commits_its_tables_with_a_ledger_row_for_each_in_name_order() -> None:
    found = databases("warehouse", "archive")
    records = run(found)
    assert list(records) == ["archive", "warehouse"]
    assert all(record == PublishRecord(status="committed", at=AT, counts={f"{name}_prices": vars(Counts(inserted=2))}) for name, record in records.items())
    assert [(entry.run_id, entry.job, entry.output, entry.interval) for entry in found["warehouse"].entries] == [("R1", "prices", "warehouse_prices", DAY)]


def test_a_run_the_ledger_holds_is_not_written_again() -> None:
    found = databases("warehouse")
    run(found)
    assert run(found)["warehouse"] == PublishRecord(status="committed", at=AT, counts={})
    assert len(found["warehouse"].rows["warehouse.prices"]) == 1


def test_a_database_that_refuses_commits_nothing_and_the_databases_after_it_are_not_written() -> None:
    found = databases("archive", "warehouse", missing="archive.prices")
    records = run(found)
    assert list(records) == ["archive"]
    assert records["archive"].status == "failed"
    assert records["archive"].error == "PublishRefusedError: archive.prices does not exist in archive: `basis ddl prices --dialect DIALECT` prints a CREATE TABLE for it"
    assert (found["archive"].entries, found["warehouse"].entries, found["warehouse"].rows) == ([], [], {})


def test_a_value_the_table_cannot_hold_refuses_the_publish_and_writes_nothing() -> None:
    found = databases("warehouse")
    records = run(found, insert=pl.DataFrame({"security_id": ["A" * 13], "price": [1.5]}))
    assert records["warehouse"].status == "failed"
    assert "security_id: values up to 13 characters, and the column holds 12" in (records["warehouse"].error or "")
    assert found["warehouse"].rows == {}


@pytest.mark.parametrize(("again", "status"), [(False, "failed"), (True, "committed")], ids=["refused", "told-again"])
def test_a_second_plain_insert_of_an_interval_is_refused_unless_the_run_is_told_again(again: bool, status: str) -> None:  # noqa: FBT001 -- a parameter of the table
    found = databases("warehouse")
    run(found, run_id="R1")
    records = run(found, run_id="R2", again=again)
    assert records["warehouse"].status == status
    assert refused_overlap(records) is not again
