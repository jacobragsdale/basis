from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

import pandas as pd
import pytest

from basis.core import frames
from basis.core.destination import DestColumn, TableSpec, Unit, check_table, check_values
from basis.core.tables import Changes, Column


@dataclass(frozen=True)
class Table:
    """A target as a part declares one, with only what the checks read."""

    columns: tuple[Column, ...]
    table: str = "prices"
    database: str = "warehouse"
    key: tuple[str, ...] | None = ("day", "security_id")

    def column(self, name: str) -> Column:
        return next(column for column in self.columns if column.name == name)


TARGET = Table(
    (
        Column(name="day", kind="date", nullable=False),
        Column(name="security_id", kind="str", nullable=False, max_length=8),
        Column(name="price", kind="decimal", nullable=False, precision=18, scale=4),
        Column(name="note", kind="str", nullable=True),
        Column(name="booked_at", kind="datetime", nullable=True, zone="America/New_York"),
    )
)
PRICE: dict[str, object] = {"day": date(2026, 11, 5), "security_id": "S1", "price": Decimal(100), "note": None, "booked_at": None}


def prices(*rows: dict[str, object]) -> pd.DataFrame:
    """Rows of the target, each filled from a valid default row, so a test states only what it is about."""
    return pd.DataFrame({column.name: pd.Series([(PRICE | row)[column.name] for row in rows], dtype=frames.dtype(column)) for column in TARGET.columns})


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
    insert = prices({"booked_at": datetime(2026, 11, 5, 14, 30, 0, 500_000, tzinfo=UTC)})
    assert check_values(TARGET, oracle, Changes(insert=insert)) == ["insert: booked_at: fractions of a second, which DATE does not keep"]


def test_an_integer_beyond_its_column_is_refused() -> None:
    target = Table((Column(name="n", kind="int", nullable=False),), table="counts", key=None)
    spec = TableSpec("mssql", "counts", {"n": column("n", "SMALLINT")})
    assert check_values(target, spec, Changes(insert=pd.DataFrame({"n": pd.Series([40_000], dtype="int64[pyarrow]")}))) == ["insert: n: values from 40000 to 40000, beyond SMALLINT's -32768 to 32767"]


def text(type_: str, length: int | None, *, unit: Unit, encoding: str | None, charset: str | None, max_bytes: int | None = None) -> DestColumn:  # noqa: PLR0913 -- a column's storage, each by keyword
    """A string column `note` of `type_`, holding `length` of its `unit`, stored in `encoding`, which `charset` names."""
    return DestColumn("note", type_, length, None, None, nullable=True, generated=False, defaulted=False, unit=unit, max_bytes=max_bytes, encoding=encoding, charset=charset)


def notes(*values: str | None) -> Changes:
    return Changes(insert=prices(*(dict[str, object](note=value) for value in values)))


def cp1252(length: int | None) -> DestColumn:
    return text("VARCHAR", length, unit="bytes", encoding="cp1252", charset="code page 1252")


CANNOT_HOLD = (
    "insert: note: 4 values hold characters code page 1252 cannot store, such as '日', which the database would replace without an error:"
    " store them in a Unicode column, NVARCHAR or a VARCHAR with a UTF-8 collation"
)


@pytest.mark.parametrize(
    ("column", "values", "problems"),
    [
        (cp1252(None), ["hello world", "Zoë, café, €5", None], []),
        (cp1252(None), ["hello world", "日本", "Ω", "\U0001f600", "日本"], [CANNOT_HOLD]),
        (text("NVARCHAR", None, unit="UTF-16 units", encoding="utf-16-le", charset="UTF-16"), ["hello world", "日本", "Ω", "\U0001f600"], []),
    ],
    ids=["text-its-code-page-holds", "text-it-does-not", "a-unicode-column"],
)
def test_text_a_columns_code_page_cannot_store_is_refused(column: DestColumn, values: list[str | None], problems: list[str]) -> None:
    """SQL Server replaces a character a `varchar`'s code page cannot hold, as `日` with `?`, and raises no error. Each
    distinct value is encoded once, and the count is of every value."""
    assert check_values(TARGET, mssql(note=column), notes(*values)) == problems


@pytest.mark.parametrize(
    ("column", "fits", "too_long", "problem"),
    [
        (text("NVARCHAR", 6, unit="UTF-16 units", encoding="utf-16-le", charset="UTF-16"), "\U0001f600" * 3, "\U0001f600" * 6, "values up to 12 UTF-16 units, and the column holds 6"),
        (text("VARCHAR", 10, unit="bytes", encoding="utf-8", charset="UTF-8"), "é" * 5, "é" * 10, "values up to 20 bytes in UTF-8, and the column holds 10"),
        (text("VARCHAR", 10, unit="bytes", encoding="cp932", charset="code page 932"), "日本語abcd", "日本語" * 2, "values up to 12 bytes in code page 932, and the column holds 10"),
        (cp1252(10), "é" * 10, "é" * 11, "values up to 11 bytes in code page 1252, and the column holds 10"),
    ],
    ids=["nvarchar-in-utf16-units", "utf8-varchar-in-bytes", "cp932-varchar-in-bytes", "cp1252-varchar-in-bytes"],
)
def test_a_string_is_measured_in_its_columns_own_units(column: DestColumn, fits: str, too_long: str, problem: str) -> None:
    """SQL Server's `nvarchar(n)` holds n UTF-16 units, so 6 emoji take 12, and its `varchar(n)` n bytes of its code
    page, so `é` takes 2 in UTF-8 and 1 in code page 1252, and `日` 2 in code page 932."""
    spec = mssql(note=column)
    assert check_values(TARGET, spec, notes(fits, "x")) == []
    assert check_values(TARGET, spec, notes(fits, too_long)) == [f"insert: note: {problem}"]


def oracle(**changed: DestColumn) -> TableSpec:
    return TableSpec("oracle", "prices", mssql().columns | changed)


def test_an_oracle_varchar2_in_characters_holds_its_bytes_too() -> None:
    """`VARCHAR2(4000 CHAR)` holds 4,000 characters, and 4,000 bytes at most, which an emoji takes 4 of in AL32UTF8."""
    column = text("VARCHAR2", 4000, unit="characters", max_bytes=4000, encoding="utf-8", charset="AL32UTF8")
    assert check_values(TARGET, oracle(note=column), notes("a" * 4000)) == []
    assert check_values(TARGET, oracle(note=column), notes("a" * 3998 + "\U0001f600")) == ["insert: note: values up to 4002 bytes in AL32UTF8, and the column holds 4000"]


def test_a_character_set_basis_does_not_know_refuses_text_beyond_ascii_and_names_the_set() -> None:
    column = text("VARCHAR2", 100, unit="characters", max_bytes=100, encoding=None, charset="EE8ISO8859P2")
    assert check_values(TARGET, oracle(note=column), notes("hello", "plain")) == []
    refusal = (
        "insert: note: 1 values hold characters beyond ASCII, such as 'ë', and basis does not know which of them EE8ISO8859P2 stores, which"
        " the database would replace without an error: store them in an NVARCHAR2 or NCLOB column, whose national character set is"
        " Unicode, or add EE8ISO8859P2 to basis.core.databases.oracle.CHARSETS"
    )
    assert check_values(TARGET, oracle(note=column), notes("hello", "Zoë")) == [refusal]
