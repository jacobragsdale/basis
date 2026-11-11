"""basis-core's publishing, on basis-core's fake databases: what any part that publishes gets, whatever it writes.
The integration tier publishes to real SQL Server and Oracle; these hold the rules without them."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import pandas as pd
import pytest

from basis.core import fakes, frames
from basis.core import publish as publishing
from basis.core.databases import Counts
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


def databases(*names: str, missing: str | None = None) -> dict[str, fakes.Database]:
    found = {name: fakes.Database(name) for name in names}
    for name, database in found.items():
        if f"{name}.prices" != missing:
            database.create(Table(f"{name}.prices", name), spec=TableSpec("mssql", f"{name}.prices", SPEC))
    return found


def prices(security_ids: list[str], values: list[float]) -> pd.DataFrame:
    return pd.DataFrame({column.name: pd.Series(given, dtype=frames.dtype(column)) for column, given in zip(COLUMNS, (security_ids, values), strict=True)})


def run(
    found: Mapping[str, fakes.Database], *, run_id: str = "R1", again: bool = False, insert: pd.DataFrame | None = None, checked: Mapping[str, TableSpec] | None = None
) -> dict[str, PublishRecord]:
    targets = {f"{name}_prices": Table(f"{name}.prices", name) for name in found}
    frame = insert if insert is not None else prices(["A", "B"], [1.5, 2.0])
    changes = {output: Changes(insert=frame) for output in targets}
    return publish(Publication(run_id, "prices", DAY, "0.0.0", again, lambda: AT), targets, changes, found.__getitem__, checked)


def test_each_database_commits_its_tables_with_a_ledger_row_for_each_in_name_order() -> None:
    found = databases("warehouse", "archive")
    records = run(found)
    assert list(records) == ["archive", "warehouse"]
    assert all(record == PublishRecord(status="committed", at=AT, counts={f"{name}_prices": vars(Counts(inserted=2))}) for name, record in records.items())
    assert [(entry.run_id, entry.job, entry.output, entry.interval) for entry in found["warehouse"].entries] == [("R1", "prices", "warehouse_prices", DAY)]


def test_a_run_the_ledger_holds_is_not_written_again() -> None:
    found = databases("warehouse")
    run(found)
    assert run(found)["warehouse"] == PublishRecord(status="committed", at=AT, counts={"warehouse_prices": vars(Counts(inserted=2))}, earlier=True)  # what the commit counted
    assert len(found["warehouse"].rows("warehouse.prices")) == 2


def test_a_database_that_refuses_commits_nothing_and_the_databases_after_it_are_not_written() -> None:
    found = databases("archive", "warehouse", missing="archive.prices")
    records = run(found)
    assert list(records) == ["archive"]
    assert records["archive"].status == "failed"
    assert records["archive"].error == "PublishRefusedError: archive.prices does not exist in archive: `basis ddl prices --dialect DIALECT` prints a CREATE TABLE for it"
    assert (found["archive"].entries, found["warehouse"].entries, found["warehouse"].rows("warehouse.prices")) == ([], [], [])


def test_a_value_the_table_cannot_hold_refuses_the_publish_and_writes_nothing() -> None:
    found = databases("warehouse")
    records = run(found, insert=prices(["A" * 13], [1.5]))
    assert records["warehouse"].status == "failed"
    assert "security_id: values up to 13 characters, and the column holds 12" in (records["warehouse"].error or "")
    assert found["warehouse"].rows("warehouse.prices") == []


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
    same = found["warehouse"].tables["warehouse.prices"].spec
    assert run(found, checked={"warehouse_prices": same})["warehouse"].status == "committed"
    assert checked == []
    changed = TableSpec("mssql", "warehouse.prices", SPEC | {"price": DestColumn("price", "REAL", None, None, None, nullable=False, generated=False, defaulted=False)})
    assert run(databases("warehouse"), checked={"warehouse_prices": changed})["warehouse"].status == "committed"
    assert checked == ["warehouse.prices"]
