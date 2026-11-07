"""Text as basis publishes it, through every write mode, read back exactly. Under mssql-python 1.17.0's own typing,
which types each column of an `executemany` by its longest value:

- a batch whose longest value was ASCII bound the column as `varchar`, and the server converted every value through
  the database's code page, so `日本` arrived as `??`, `Ω` as `?` and `😀` as `:-D`, with no error, into an `nvarchar`
  and a UTF-8 `varchar` alike;
- a value with more UTF-16 units than that longest one, as six emoji beside `hello world`, raised;
- a value past 4,000 UTF-16 units made the driver write each row by itself and count only the last, so the count
  check refused the publish;
- a decimal of 38 digits, whose text is longer than 38 characters, raised.

test_text's batches passed only because their longest values were not ASCII. Every row here also carries a decimal, a
bigint, a bit, a float and a datetime, which basis binds by their kinds, in a row past 4,000 units too, and every
publish keeps the frame's order, which a plain insert's identity column shows.

A column measures a value in its own units, `nvarchar(n)` in UTF-16 units and `varchar(n)` in its code page's bytes,
and refuses a character its code page cannot hold, before anything is written, since the server would replace it
without an error: so does a column of an alias type, by the type it is an alias of. A table whose unique index ignores
duplicate keys drops a row, which the server counts: the count check refuses that publish, whether the row it drops
went in a batch or by itself, through the insert and through the upsert's stage. Oracle's AL32UTF8 database, CI's,
takes the same text, and refuses a value past a `VARCHAR2`'s 4,000 bytes; its non-Unicode character sets have unit
tests alone (docs/spikes/sqlserver-json.md)."""

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import pandas as pd
import pytest
from integration.scratch import Scratch, configured, mssql, oracle

import basis.job as etl
from basis.core.interval import Interval
from basis.core.settings import GivenSettings
from basis.core.storage import Root
from basis.job import runs
from basis.job.job import Job
from basis.job.outputs import FrameWrite
from basis.job.testing import frame

pytestmark = pytest.mark.integration

COLUMNS = (
    "seq int IDENTITY(1,1) NOT NULL, id int NOT NULL PRIMARY KEY, day date NOT NULL, code int NOT NULL, amount decimal(38,10) NULL,"
    " flag bit NULL, ratio float NULL, at datetime2(6) NULL, unicode nvarchar(max) NULL,"
    " utf8 varchar(max) COLLATE Latin1_General_100_CI_AS_SC_UTF8 NULL, latin varchar(max) COLLATE SQL_Latin1_General_CP1_CI_AS NULL"
)
EXACT = "SELECT id, day, code, amount, flag, ratio, at, unicode, CAST(utf8 AS nvarchar(max)), CAST(latin AS nvarchar(max)) FROM {table} ORDER BY id"
"""What a table holds, its text read as nvarchar, which the driver reads exactly."""
DAY = date(2026, 11, 5)
AMOUNT = Decimal("123456789012345678901234567.0123456789")
"""37 digits, beyond the range of `money`, within which the driver binds a decimal as text itself."""
WIDEST = Decimal("-1234567890123456789012345678.0123456789")
"""38 digits, and 40 characters of text, which the driver's own typing refused, as `Invalid precision value`."""
AT = datetime(2026, 11, 5, 9, 30, 1, 123456, tzinfo=UTC)
EMOJI = "\U0001f600"
UNITS_4000 = "a" * 3998 + EMOJI
UNITS_4001 = "a" * 3999 + EMOJI
"""4,000 characters, and 4,001 UTF-16 units, which only `nvarchar(max)` binds."""
POLYGON = "POLYGON ((" + ", ".join(f"{index} {index}" for index in range(1200)) + "))"
"""Text the driver's own typing of an `executemany` took for geometry, sizing it by its characters."""
UNICODE: dict[str, list[str | None]] = {
    "ascii-longest": ["hello world", "日本", "Ω", EMOJI, None],
    "more-units-than-the-longest": ["hello world", EMOJI * 6],
    "either-side-of-4000-units": ["日" * 3999, UNITS_4000, UNITS_4001, "x"],
    "100000-characters": [("日本" + EMOJI) * 33_333 + "a", "hello world", "日本"],
    "geometry-like": [POLYGON, "POINTS: " + EMOJI * 2500, "POINTS: " + EMOJI * 10, "POINT (1 2)"],
}
"""Batches of text only Unicode columns hold: the code page 1252 column is null."""
LATIN: dict[str, list[str | None]] = {
    "ascii-longest": ["hello world", "Zoë", "€", "café", None],
    "either-side-of-4000-units": ["é" * 3999, "é" * 4000, "é" * 4001, "x"],
    "100000-characters": [("Zoë, café, €5; " * 6_667)[:100_000], "hello world"],
}
"""Batches of text code page 1252 holds, in all three columns."""


class Doc(etl.Row):
    id: int
    day: date
    code: int
    amount: Annotated[Decimal, etl.Precision(38, 10)] | None
    flag: bool | None
    ratio: float | None
    at: Annotated[datetime, etl.Zone("UTC")] | None
    unicode: str | None
    utf8: str | None
    latin: str | None


def ids(texts: list[str | None]) -> list[int]:
    """Each row's id, falling, so the frame's order is not the ids'."""
    return list(range(len(texts), 0, -1))


def values(index: int, text: str | None, *, latin: bool, amount: Decimal) -> dict[str, object]:
    """A row of every kind: each but the decimal is null in every third row."""
    nulls = index % 3 == 0
    return {
        "id": index, "day": DAY, "code": index, "amount": amount, "flag": None if nulls else index % 2 == 0, "ratio": None if nulls else index / 10,
        "at": None if nulls else AT, "unicode": text, "utf8": text, "latin": text if latin else None,
    }  # fmt: skip


def docs(texts: list[str | None], *, latin: bool, amount: Decimal = AMOUNT) -> pd.DataFrame:
    """A row for each text, in the Unicode columns and, where `latin`, the code page 1252 one, with every other kind."""
    return frame(Doc, *(values(index, text, latin=latin, amount=amount) for index, text in zip(ids(texts), texts, strict=True)))


def expected(texts: list[str | None], *, latin: bool, amount: Decimal = AMOUNT) -> list[tuple[object, ...]]:
    """The rows `docs` makes, as `EXACT` reads them back, by id."""

    def read(index: int, text: str | None) -> tuple[object, ...]:
        nulls = index % 3 == 0
        at = None if nulls else AT.replace(tzinfo=None)
        return (index, DAY, index, amount, None if nulls else index % 2 == 0, None if nulls else index / 10, at, text, text, text if latin else None)

    return [read(index, text) for index, text in sorted(zip(ids(texts), texts, strict=True), key=lambda pair: pair[0])]


@pytest.fixture
def scratch() -> Iterator[Scratch]:
    """A SQL Server scratch database with a ledger and a table of `Doc`s of this test's own, dropped after."""
    found = mssql(configured(), uuid.uuid4().hex[:8]).with_ledger()
    found.execute(f"CREATE TABLE {table_of(found)} ({COLUMNS})")
    yield found
    found.drop(table_of(found), found.ledger())


def table_of(scratch: Scratch) -> str:
    return f"etl_it_docs_{scratch.suffix}"


def publish(scratch: Scratch, write: FrameWrite, rows: pd.DataFrame, root: Path, *, row: type[etl.Row] = Doc, table: str | None = None) -> runs.Result:  # noqa: PLR0913 -- a test's knobs, each by keyword
    data = root / "data"
    data.mkdir(parents=True)
    rows.to_parquet(data / "docs.parquet", index=False)

    class Inputs(etl.Inputs):
        docs = etl.File(row, path="docs.parquet", empty="fail")

    class Outputs(etl.Outputs):
        docs = etl.Table(row, database="warehouse", table=table or table_of(scratch), write=write, key=("id",))

    def transform(inputs: Inputs, _params: etl.Params) -> Outputs:
        return Outputs(docs=inputs.docs)

    job = Job("docs_it", "tests.docs_it", etl.Params, Inputs, Outputs, transform, None, Path(__file__).parent)
    params = root / "params.json"
    params.write_text("{}", encoding="utf-8")
    request = runs.Request(params, Interval.parse("2026-11-05..2026-11-06"), data, publish=True)
    return runs.run(job, GivenSettings(databases={"warehouse": scratch.settings}), request, snapshot_root=Root.at(root / "snapshots"))


WRITES = [etl.Append(), etl.ReplaceWhere(interval="day"), etl.Upsert()]
"""The plain insert, a delete then the plain insert, and the stage."""


def counted(write: FrameWrite, rows: int) -> dict[str, dict[str, int]]:
    upserted, inserted = (rows, 0) if isinstance(write, etl.Upsert) else (0, rows)
    return {"docs": {"deleted": 0, "upserted": upserted, "inserted_new": 0, "inserted": inserted}}


BATCHES = [pytest.param(texts, False, id=f"unicode-{name}") for name, texts in UNICODE.items()] + [pytest.param(texts, True, id=f"latin-{name}") for name, texts in LATIN.items()]


@pytest.mark.parametrize("write", WRITES, ids=lambda write: type(write).__name__)
@pytest.mark.parametrize(("texts", "latin"), BATCHES)
def test_text_publishes_exactly_in_the_frames_order_and_is_counted_by_the_server(scratch: Scratch, tmp_path: Path, write: FrameWrite, texts: list[str | None], *, latin: bool) -> None:
    """A plain insert keeps the frame's order, a long row in its place among the others, as the identity column shows;
    the upsert's insert from its stage has no order to keep."""
    result = publish(scratch, write, docs(texts, latin=latin), tmp_path)
    assert result.record.status == "published", result.record
    assert result.record.publishes["warehouse"].counts == counted(write, len(texts))
    assert scratch.rows(EXACT.format(table=table_of(scratch))) == expected(texts, latin=latin)
    if not isinstance(write, etl.Upsert):
        assert [row[0] for row in scratch.rows(f"SELECT id FROM {table_of(scratch)} ORDER BY seq")] == ids(texts)


@pytest.mark.parametrize("write", WRITES, ids=lambda write: type(write).__name__)
def test_a_decimal_of_38_digits_publishes_exactly_in_a_batch_and_in_a_row_by_itself(scratch: Scratch, tmp_path: Path, write: FrameWrite) -> None:
    texts: list[str | None] = ["hello world", UNITS_4001, "a" * 100_000, None]
    result = publish(scratch, write, docs(texts, latin=False, amount=WIDEST), tmp_path)
    assert result.record.status == "published", result.record
    assert scratch.rows(EXACT.format(table=table_of(scratch))) == expected(texts, latin=False, amount=WIDEST)


@pytest.mark.parametrize("write", WRITES, ids=lambda write: type(write).__name__)
def test_a_varchar_in_code_page_1252_refuses_text_it_cannot_hold_before_anything_is_written(scratch: Scratch, tmp_path: Path, write: FrameWrite) -> None:
    operation = "upsert" if isinstance(write, etl.Upsert) else "insert"
    result = publish(scratch, write, docs(UNICODE["ascii-longest"], latin=True), tmp_path)
    assert result.record.status == "failed"
    refusal = (
        f"docs: {operation}: latin: 3 values hold characters code page 1252 cannot store, such as '日', which the database would replace without"
        " an error: store them in a Unicode column, NVARCHAR or a VARCHAR with a UTF-8 collation"
    )
    assert result.record.reasons == [refusal]
    assert scratch.rows(f"SELECT COUNT(*) FROM {table_of(scratch)}") == [(0,)]


@pytest.mark.parametrize("write", WRITES, ids=lambda write: type(write).__name__)
@pytest.mark.parametrize("dropped", [2, 3], ids=["in-a-batch", "by-itself"])
def test_a_row_the_server_drops_and_counts_fails_the_count_check(scratch: Scratch, tmp_path: Path, write: FrameWrite, dropped: int) -> None:
    """Row 3 holds 4,001 UTF-16 units, so it goes by itself, and the others in batches. The unique index on `code`
    ignores a duplicate key, with a warning and no error, and the table already holds `dropped`'s code, so the server
    writes, and counts, one row fewer than the run computed."""
    table = table_of(scratch)
    scratch.execute(
        f"CREATE UNIQUE INDEX etl_it_docs_code_{scratch.suffix} ON {table} (code) WITH (IGNORE_DUP_KEY = ON)",
        f"INSERT INTO {table} (id, day, code) VALUES (99, '2026-11-01', {dropped})",  # outside the interval ReplaceWhere replaces
    )
    texts: list[str | None] = ["a" * 100_000, "x", UNITS_4001, "日本", "hello world"]
    result = publish(scratch, write, docs(texts, latin=False), tmp_path)
    what = "upsert" if isinstance(write, etl.Upsert) else "insert"
    assert result.record.status == "failed"
    error = result.record.publishes["warehouse"].error or ""
    assert f"RuntimeError: the {what} into {table} wrote 4 rows, and the run computed 5: nothing is committed" == error
    assert scratch.rows(f"SELECT id, code FROM {table}") == [(99, dropped)]


class Sized(etl.Row):
    id: int
    day: date
    units: str | None
    utf8: str | None
    japanese: str | None


def test_a_value_is_measured_in_its_columns_own_units_before_anything_is_written(scratch: Scratch, tmp_path: Path) -> None:
    """`nvarchar(6)` holds 6 UTF-16 units, which 3 emoji fill; a UTF-8 `varchar(10)` 10 bytes, which 5 `é` fill; and a
    code page 932 `varchar(10)` 10 bytes, which `日本語abcd` fills. Past them the server would raise, once the rows
    before were written."""
    table = f"etl_it_sized_{scratch.suffix}"
    scratch.execute(
        f"CREATE TABLE {table} (id int NOT NULL PRIMARY KEY, day date NOT NULL, units nvarchar(6) NULL,"
        " utf8 varchar(10) COLLATE Latin1_General_100_CI_AS_SC_UTF8 NULL, japanese varchar(10) COLLATE Japanese_CI_AS NULL)"
    )
    try:
        fits = frame(Sized, {"id": 1, "day": DAY, "units": EMOJI * 3, "utf8": "é" * 5, "japanese": "日本語abcd"})
        published = publish(scratch, etl.Append(), fits, tmp_path / "fits", row=Sized, table=table)
        assert published.record.status == "published", published.record
        read = scratch.rows(f"SELECT id, units, CAST(utf8 AS nvarchar(10)), CAST(japanese AS nvarchar(10)) FROM {table}")
        assert read == [(1, EMOJI * 3, "é" * 5, "日本語abcd")]
        too_long = frame(Sized, {"id": 2, "day": DAY, "units": EMOJI * 6, "utf8": "é" * 10, "japanese": "日本語" * 2})
        refused = publish(scratch, etl.Append(), too_long, tmp_path / "too-long", row=Sized, table=table)
        assert refused.record.status == "failed"
        assert refused.record.reasons == [
            "docs: insert: units: values up to 12 UTF-16 units, and the column holds 6",
            "docs: insert: utf8: values up to 20 bytes in UTF-8, and the column holds 10",
            "docs: insert: japanese: values up to 12 bytes in code page 932, and the column holds 10",
        ]
        assert scratch.rows(f"SELECT COUNT(*) FROM {table}") == [(1,)]
    finally:
        scratch.drop(table)


class Aliased(etl.Row):
    id: int
    day: date
    code: str | None
    name: str | None


def test_a_column_of_an_alias_type_is_checked_as_the_type_it_is(scratch: Scratch, tmp_path: Path) -> None:
    """A column of `CREATE TYPE ... FROM varchar(50)` stores text in its collation's code page, as a `varchar` does, and
    one of an alias of `nvarchar(50)` holds any text."""
    table, code, name = (f"etl_it_{what}_{scratch.suffix}" for what in ("aliased", "code", "name"))
    scratch.execute(f"CREATE TYPE {code} FROM varchar(50)", f"CREATE TYPE {name} FROM nvarchar(50)")
    try:
        scratch.execute(f"CREATE TABLE {table} (id int NOT NULL PRIMARY KEY, day date NOT NULL, code {code} NULL, name {name} NULL)")
        rows = frame(Aliased, {"id": 1, "day": DAY, "code": "Zoë, €5", "name": "日本 " + EMOJI})
        published = publish(scratch, etl.Append(), rows, tmp_path / "held", row=Aliased, table=table)
        assert published.record.status == "published", published.record
        assert scratch.rows(f"SELECT id, CAST(code AS nvarchar(50)), name FROM {table}") == [(1, "Zoë, €5", "日本 " + EMOJI)]
        refused = publish(scratch, etl.Append(), frame(Aliased, {"id": 2, "day": DAY, "code": "日本"}), tmp_path / "lost", row=Aliased, table=table)
        assert refused.record.status == "failed"
        refusal = (
            "docs: insert: code: 1 values hold characters code page 1252 cannot store, such as '日', which the database would replace without an"
            " error: store them in a Unicode column, NVARCHAR or a VARCHAR with a UTF-8 collation"
        )
        assert refused.record.reasons == [refusal]
    finally:
        scratch.drop(table)
        scratch.execute(f"DROP TYPE IF EXISTS {code}", f"DROP TYPE IF EXISTS {name}")


class OracleDoc(etl.Row):
    id: int
    day: date
    note: str | None
    body: str | None


@pytest.fixture
def oracle_scratch() -> Iterator[Scratch]:
    found = oracle(configured(), uuid.uuid4().hex[:8]).with_ledger()
    found.execute(f"CREATE TABLE {table_of(found)} (id NUMBER(10) NOT NULL PRIMARY KEY, day DATE NOT NULL, note VARCHAR2(4000 CHAR), body CLOB)")
    yield found
    found.drop(table_of(found), found.ledger())


@pytest.mark.parametrize("write", WRITES, ids=lambda write: type(write).__name__)
@pytest.mark.parametrize("texts", [UNICODE["ascii-longest"], UNICODE["more-units-than-the-longest"], ["a" * 4000, "é" * 2000, "x"]], ids=["ascii-longest", "more-units-than-the-longest", "4000-bytes"])
def test_oracle_takes_the_same_text_exactly(oracle_scratch: Scratch, tmp_path: Path, write: FrameWrite, texts: list[str | None]) -> None:
    rows = frame(OracleDoc, *({"id": index, "day": DAY, "note": text, "body": text} for index, text in enumerate(texts, 1)))
    result = publish(oracle_scratch, write, rows, tmp_path, row=OracleDoc)
    assert result.record.status == "published", result.record
    assert oracle_scratch.rows(f"SELECT id, note, body FROM {table_of(oracle_scratch)} ORDER BY id") == [(index, text, text) for index, text in enumerate(texts, 1)]


def test_an_oracle_varchar2_refuses_a_value_past_its_bytes_before_anything_is_written(oracle_scratch: Scratch, tmp_path: Path) -> None:
    """`VARCHAR2(4000 CHAR)` holds 4,000 bytes at most, which 3,998 `a` and an emoji pass by 2 in AL32UTF8: the server
    raised ORA-12899 while basis counted characters."""
    rows = frame(OracleDoc, {"id": 1, "day": DAY, "note": "a" * 3998 + EMOJI, "body": None})
    result = publish(oracle_scratch, etl.Append(), rows, tmp_path, row=OracleDoc)
    assert result.record.status == "failed"
    assert result.record.reasons == ["docs: insert: note: values up to 4002 bytes in AL32UTF8, and the column holds 4000"]
