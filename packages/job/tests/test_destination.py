from datetime import datetime
from decimal import Decimal

import polars as pl
import pytest
from builders import Price, prices

import etl_framework as etl
from etl_framework.destination import DestColumn, TableSpec, check_table, check_values
from etl_framework.outputs import Changes, Target

TARGET = Target(Price, "warehouse", "prices", etl.Append(), ("day", "security_id"))


def column(name: str, kind: str, *, length: int | None = None, digits: tuple[int, int] | None = None, nullable: bool = True) -> DestColumn:
    precision, scale = digits if digits is not None else (None, None)
    return DestColumn(name, kind, length, precision, scale, nullable=nullable, generated=False, defaulted=False)


def mssql(**changed: DestColumn) -> TableSpec:
    columns = {
        "day": column("day", "DATE", nullable=False),
        "security_id": column("security_id", "NVARCHAR", length=8, nullable=False),
        "price": column("price", "DECIMAL", digits=(18, 4), nullable=False),
        "note": column("note", "NVARCHAR", length=None),
        "booked_at": column("booked_at", "DATETIME2"),
    }
    return TableSpec("mssql", "prices", columns | changed)


def test_a_table_that_matches_its_row_has_no_problems() -> None:
    assert check_table(TARGET, mssql()) == []


@pytest.mark.parametrize(
    ("changed", "problem"),
    [
        ({"price": column("price", "FLOAT")}, "price: a FLOAT column cannot hold a row's decimal values exactly"),
        ({"price": column("price", "DECIMAL", digits=(18, 2))}, "price: DECIMAL(18,2) keeps fewer decimal places than the row's 4"),
        ({"day": column("day", "NVARCHAR", length=10)}, "day: a NVARCHAR column cannot hold a row's date values exactly"),
        ({"extra": column("extra", "INT", nullable=False)}, "extra: prices requires it, with no default, and the row leaves it out"),
    ],
)
def test_a_table_that_has_drifted_from_its_row_is_refused(changed: dict[str, DestColumn], problem: str) -> None:
    assert check_table(TARGET, mssql(**changed)) == [problem]


def test_a_column_the_table_does_not_have_or_fills_itself_is_refused() -> None:
    spec = mssql()
    generated = DestColumn("booked_at", "DATETIME2", None, None, None, nullable=True, generated=True, defaulted=False)
    without = TableSpec("mssql", "prices", {name: found for name, found in spec.columns.items() if name != "note"} | {"booked_at": generated})
    assert check_table(TARGET, without) == ["note: prices has no such column", "booked_at: prices fills it itself (identity, computed or virtual): leave it out of the row"]


@pytest.mark.parametrize(
    ("changed", "rows", "problem"),
    [
        ({"security_id": column("security_id", "NVARCHAR", length=2)}, {"security_id": "S123"}, "values up to 4 characters, and the column holds 2"),
        ({"price": column("price", "DECIMAL", digits=(6, 4))}, {"price": Decimal(100)}, "values up to 100.0000, and DECIMAL(6,4) holds less than 100"),
        ({"note": column("note", "NVARCHAR", nullable=False)}, {"note": None}, "1 nulls, and NVARCHAR column note is NOT NULL"),
    ],
)
def test_values_that_do_not_fit_the_table_are_refused(changed: dict[str, DestColumn], rows: dict[str, object], problem: str) -> None:
    assert check_values(TARGET, mssql(**changed), Changes(insert=prices(rows))) == [f"insert: {next(iter(changed))}: {problem}"]


def test_a_time_finer_than_the_column_keeps_is_refused() -> None:
    oracle = TableSpec("oracle", "prices", mssql().columns | {"booked_at": column("booked_at", "DATE")})
    insert = prices({"booked_at": datetime(2026, 11, 5, 9, 30, 0, 500_000)})
    assert check_values(TARGET, oracle, Changes(insert=insert)) == ["insert: booked_at: fractions of a second, which DATE does not keep"]


def test_an_integer_beyond_its_column_is_refused() -> None:
    class Count(etl.Row):
        n: int

    target = Target(Count, "warehouse", "counts", etl.Append())
    spec = TableSpec("mssql", "counts", {"n": column("n", "SMALLINT")})
    assert check_values(target, spec, Changes(insert=pl.DataFrame({"n": [40_000]}))) == ["insert: n: values from 40000 to 40000, beyond SMALLINT's -32768 to 32767"]
