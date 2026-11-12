from dataclasses import dataclass
from datetime import UTC, date, datetime

import pandas as pd
import pytest

from basis.core import frames
from basis.core.destination import DestColumn, TableSpec, Unit, binding, check_table, check_values
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
        Column(name="price", kind="float", nullable=False),
        Column(name="note", kind="str", nullable=True),
        Column(name="booked_at", kind="datetime", nullable=True, zone="America/New_York"),
    )
)
PRICE: dict[str, object] = {"day": date(2026, 11, 5), "security_id": "S1", "price": 100.0, "note": None, "booked_at": None}


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
        ({"price": column("price", "NVARCHAR", length=10)}, "price: a NVARCHAR column cannot hold a row's float values exactly"),
        ({"price": column("price", "MONEY")}, "price: a MONEY column cannot hold a row's float values exactly"),
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
        ({"price": column("price", "DECIMAL", digits=(6, 4))}, {"price": 99.99995}, "values up to 100.0000, and DECIMAL(6,4) holds less than 100"),
        ({"price": column("price", "FLOAT")}, {"price": float("-inf")}, "1 infinite values, which FLOAT column price cannot store: replace them in the transform, with NaN for NULL"),
        ({"price": column("price", "INT")}, {"price": 2.5}, "1 values with a fraction, which INT column price does not keep: round them in the transform, as the data needs"),
        (
            {"price": column("price", "BIGINT")},
            {"price": 2.0**60},
            "values from 1152921504606846976 to 1152921504606846976, beyond BIGINT's -9007199254740992 to 9007199254740992 that a float holds exactly",
        ),
        ({"price": column("price", "FLOAT", nullable=False)}, {"price": float("nan")}, "1 nulls, and FLOAT column price is NOT NULL"),
        ({"security_id": column("security_id", "INT")}, {"security_id": "S1"}, "1 values that are not whole numbers, which INT column security_id cannot store, such as 'S1'"),
        ({"security_id": column("security_id", "TINYINT")}, {"security_id": "300"}, "values from 300 to 300, beyond TINYINT's 0 to 255"),
        ({"note": column("note", "NVARCHAR", nullable=False)}, {"note": None}, "1 nulls, and NVARCHAR column note is NOT NULL"),
    ],
)
def test_values_that_do_not_fit_the_table_are_refused(changed: dict[str, DestColumn], rows: dict[str, object], problem: str) -> None:
    assert check_values(TARGET, mssql(**changed), Changes(insert=prices(rows))) == [f"insert: {next(iter(changed))}: {problem}"]


def test_a_time_finer_than_the_column_keeps_is_refused() -> None:
    oracle = TableSpec("oracle", "prices", mssql().columns | {"booked_at": column("booked_at", "DATE")})
    insert = prices({"booked_at": datetime(2026, 11, 5, 14, 30, 0, 500_000, tzinfo=UTC)})
    assert check_values(TARGET, oracle, Changes(insert=insert)) == ["insert: booked_at: fractions of a second, which DATE does not keep"]


def test_a_float_goes_into_a_float_a_decimal_or_an_integer_column_and_an_identifiers_text_into_an_integer_one() -> None:
    assert check_table(TARGET, mssql(price=column("price", "DECIMAL", digits=(10, 2)), security_id=column("security_id", "BIGINT"))) == []
    oracle = TableSpec("oracle", "prices", {
        "day": column("day", "DATE", nullable=False), "security_id": column("security_id", "NUMBER", digits=(10, 0), nullable=False),
        "price": column("price", "NUMBER", digits=(10, 2), nullable=False), "note": column("note", "NVARCHAR2"), "booked_at": column("booked_at", "TIMESTAMP"),
    })  # fmt: skip
    assert check_table(TARGET, oracle) == []
    fraction = TableSpec("oracle", "prices", oracle.columns | {"security_id": column("security_id", "NUMBER", digits=(10, 2))})
    assert check_table(TARGET, fraction) == ["security_id: text goes into a NUMBER column only as an identifier's whole number, into NUMBER(p) or NUMBER(p,0)"]
    assert binding(column("price", "DECIMAL", digits=(10, 2))) == binding(column("price", "NUMBER")) == "decimal"
    assert (binding(column("price", "SMALLINT")), binding(column("price", "FLOAT")), binding(column("price", "BINARY_DOUBLE"))) == ("whole", "float", "float")


def test_an_integer_beyond_its_column_is_refused() -> None:
    target = Table((Column(name="n", kind="float", nullable=False),), table="counts", key=None)
    spec = TableSpec("mssql", "counts", {"n": column("n", "SMALLINT")})
    assert check_values(target, spec, Changes(insert=pd.DataFrame({"n": [40_000.0]}))) == ["insert: n: values from 40000 to 40000, beyond SMALLINT's -32768 to 32767 that a float holds exactly"]


def text(type_: str, length: int | None, *, unit: Unit, encoding: str | None, charset: str | None, max_bytes: int | None = None) -> DestColumn:
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


@pytest.mark.parametrize("kind", ["REAL", "BINARY_FLOAT"])
def test_a_float_a_single_precision_column_would_round_is_refused(kind: str) -> None:
    spec = TableSpec("mssql" if kind == "REAL" else "oracle", "prices", mssql().columns | {"price": column("price", kind, nullable=False)})
    assert check_values(TARGET, spec, Changes(insert=prices({"price": 0.5}))) == []
    assert check_values(TARGET, spec, Changes(insert=prices({"price": 100.1}))) == [
        f"insert: price: 1 values {kind} would round, such as 100.1, since it keeps a float to about 7 digits: store them in a FLOAT or BINARY_DOUBLE column, or round them in the transform"
    ]


@pytest.mark.parametrize(("kind", "digits"), [("DATETIME2", 3), ("TIMESTAMP", 0)])
def test_a_time_finer_than_a_columns_fractional_seconds_is_refused(kind: str, digits: int) -> None:
    spec = TableSpec("mssql" if kind == "DATETIME2" else "oracle", "prices", mssql().columns | {"booked_at": column("booked_at", kind, digits=(0, digits))})
    fine = datetime(2026, 11, 5, 14, 30, 0, 123_456, tzinfo=UTC)
    assert check_values(TARGET, spec, Changes(insert=prices({"booked_at": fine}))) == [f"insert: booked_at: times finer than {kind}({digits}) keeps, which would round them"]
    kept = datetime(2026, 11, 5, 14, 30, 0, 0, tzinfo=UTC)
    assert check_values(TARGET, spec, Changes(insert=prices({"booked_at": kept}))) == []


def test_an_identifiers_text_the_server_would_change_is_refused() -> None:
    spec = mssql(security_id=column("security_id", "BIGINT", nullable=False))
    assert check_values(TARGET, spec, Changes(insert=prices({"security_id": "00123"}, {"security_id": "+7"}, {"security_id": "42"}))) == [
        "insert: security_id: 2 values the server would store as other text, such as '00123', which BIGINT column security_id reads back as 123: store them in a text column"
    ]


def test_oracle_refuses_an_empty_string_which_it_stores_as_null() -> None:
    spec = TableSpec("oracle", "prices", mssql().columns | {"note": column("note", "VARCHAR2", length=40)})
    assert check_values(TARGET, spec, Changes(insert=prices({"note": ""}))) == [
        "insert: note: 1 empty strings, which Oracle stores as NULL, so a key or a match on them never finds the row: give the row None for them, as frame.replace({'note': {'': None}})"
    ]
    assert check_values(TARGET, mssql(), Changes(insert=prices({"note": ""}))) == []


@pytest.mark.parametrize(("dialect", "name"), [("mssql", "key"), ("mssql", "order"), ("oracle", "date"), ("oracle", "order")])
def test_a_column_named_a_word_its_database_reserves_is_refused_since_basis_quotes_no_name(dialect: str, name: str) -> None:
    target = Table((Column(name=name, kind="str", nullable=False),), key=None)
    spec = TableSpec(dialect, "prices", {name: column(name, "NVARCHAR" if dialect == "mssql" else "VARCHAR2", nullable=False)})  # ty: ignore[invalid-argument-type] -- each case's dialect
    reserved = "SQL Server" if dialect == "mssql" else "Oracle"
    assert check_table(target, spec) == [f"{name}: {reserved} reserves the word {name.upper()}, and basis never quotes a column's name: rename the row's column, and the table's"]
