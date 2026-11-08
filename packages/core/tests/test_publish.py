"""basis-core's publishing, on databases of the test's own: what any part that publishes gets, whatever it writes. The
integration tier publishes to real SQL Server and Oracle; these hold the rules without them."""

from collections.abc import Generator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pandas as pd
import pytest

from basis.core import frames
from basis.core import publish as publishing
from basis.core.databases import Counts, LedgerEntry, Reader, Statements, Writer
from basis.core.destination import DestColumn, TableSpec
from basis.core.interval import Interval
from basis.core.publish import Publication, publish, refused_overlap
from basis.core.snapshots import PublishRecord
from basis.core.tables import Changes, Column, Target

AT = datetime(2026, 11, 6, 6, tzinfo=UTC)
DAY = Interval(datetime(2026, 11, 5, tzinfo=UTC), datetime(2026, 11, 6, tzinfo=UTC))
COLUMNS = (Column("security_id", "str", nullable=False, max_length=12), Column("price", "float", nullable=False))
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
    rows: dict[str, list[pd.DataFrame]] = field(default_factory=dict[str, list[pd.DataFrame]])
    entries: list[LedgerEntry] = field(default_factory=list[LedgerEntry])


@dataclass
class FakeWriter:
    ledger: Ledger
    rows: dict[str, list[pd.DataFrame]] = field(default_factory=dict[str, list[pd.DataFrame]])
    entries: list[LedgerEntry] = field(default_factory=list[LedgerEntry])

    def fetch(self, sql: str, interval: Interval | None, *, what: str) -> pd.DataFrame:
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
        return Counts(inserted=len(changes.insert))

    def record(self, entry: LedgerEntry) -> None:
        self.entries.append(entry)

    def commit(self) -> None:
        for table, written in self.rows.items():
            self.ledger.rows.setdefault(table, []).extend(written)
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


def prices(security_ids: list[str], values: list[float]) -> pd.DataFrame:
    return pd.DataFrame({column.name: pd.Series(given, dtype=frames.dtype(column)) for column, given in zip(COLUMNS, (security_ids, values), strict=True)})


def run(found: Mapping[str, Ledger], *, run_id: str = "R1", again: bool = False, insert: pd.DataFrame | None = None, checked: Mapping[str, TableSpec] | None = None) -> dict[str, PublishRecord]:
    targets = {f"{name}_prices": Table(f"{name}.prices", name) for name in found}
    frame = insert if insert is not None else prices(["A", "B"], [1.5, 2.0])
    changes = {output: Changes(insert=frame) for output in targets}
    return publish(Publication(run_id, "prices", DAY, "0.0.0", again, lambda: AT), targets, changes, lambda name: FakeDatabase(found[name]), checked)


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
    records = run(found, insert=prices(["A" * 13], [1.5]))
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


def test_values_the_run_checked_against_the_same_table_are_not_checked_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run checks its values against each table as its read login describes it, and the publish describes the table
    again in its transaction: where the two are the same, the values are not checked a second time."""
    checked: list[str] = []
    real = publishing.check_values

    def counting(target: Target, spec: TableSpec, changes: Changes) -> list[str]:
        checked.append(spec.table)
        return real(target, spec, changes)

    monkeypatch.setattr(publishing, "check_values", counting)
    found = databases("warehouse")
    same = found["warehouse"].specs["warehouse.prices"]
    assert run(found, checked={"warehouse_prices": same})["warehouse"].status == "committed"
    assert checked == []
    changed = TableSpec("mssql", "warehouse.prices", SPEC | {"price": DestColumn("price", "REAL", None, None, None, nullable=False, generated=False, defaulted=False)})
    assert run(databases("warehouse"), checked={"warehouse_prices": changed})["warehouse"].status == "committed"
    assert checked == ["warehouse.prices"]
