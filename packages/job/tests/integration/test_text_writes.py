"""Text as basis publishes it to SQL Server, through every write mode, read back exactly. Under mssql-python 1.17.0's
own typing, which types each column of an `executemany` by its longest value:

- a batch whose longest value was ASCII bound the column as `varchar`, and the server converted every value through
  the database's code page, so `日本` arrived as `??`, `Ω` as `?` and `😀` as `:-D`, with no error, into an `nvarchar`
  and a UTF-8 `varchar` alike;
- a value with more UTF-16 units than that longest one, as six emoji beside `hello world`, raised;
- a value past 4,000 UTF-16 units made the driver write each row by itself and count only the last, so the count
  check refused the publish.

test_text's batches passed only because their longest values were not ASCII. Each batch here also carries a decimal, a
bigint, a bit, a float and a datetime, which basis binds by their kinds, in a row past 4,000 units too; and a decimal of
38 digits, whose text is longer than 38 characters, made the driver's own typing raise.

A `varchar` in code page 1252 takes the Latin text the code page holds, and a run refuses text it cannot hold, before
anything is written, since the server would replace each such character without an error. And a table whose unique
index ignores duplicate keys drops a row, which the server counts: the count check refuses that publish, whether the
row it drops went in a batch or by itself, through the insert and through the upsert's stage (docs/spikes/sqlserver-json.md)."""

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import polars as pl
import pytest
from integration.scratch import Scratch, configured, mssql

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
    "id int NOT NULL PRIMARY KEY, day date NOT NULL, code int NOT NULL, amount decimal(38,10) NULL, flag bit NULL, ratio float NULL,"
    " at datetime2(6) NULL, unicode nvarchar(max) NULL, utf8 varchar(max) COLLATE Latin1_General_100_CI_AS_SC_UTF8 NULL,"
    " latin varchar(max) COLLATE SQL_Latin1_General_CP1_CI_AS NULL"
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
UNICODE: dict[str, list[str | None]] = {
    "ascii-longest": ["hello world", "日本", "Ω", EMOJI, None],
    "more-units-than-the-longest": ["hello world", EMOJI * 6],
    "either-side-of-4000-units": ["日" * 3999, UNITS_4000, UNITS_4001, "x"],
    "100000-characters": [("日本" + EMOJI) * 33_333 + "a", "hello world", "日本"],
}
"""Batches of text only Unicode columns hold: the code page 1252 column is null."""
LATIN: dict[str, list[str | None]] = {
    "ascii-longest": ["hello world", "Zoë", "€", "café", None],
    "either-side-of-4000-units": ["é" * 3999, "é" * 4000, "é" * 4001, "x"],
    "100000-characters": ["Zoë, café, €5; " * 6_666 + "Zoë, café, €5", "hello world"],
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


def docs(texts: list[str | None], *, latin: bool, amount: Decimal = AMOUNT) -> pl.DataFrame:
    """A row for each text, in the Unicode columns and, where `latin`, the code page 1252 one, with every other kind."""
    return frame(
        Doc,
        *(
            {"id": index, "day": DAY, "code": index, "amount": amount if index % 2 else None, "flag": index % 2 == 0}
            | {"ratio": index / 10, "at": AT, "unicode": text, "utf8": text, "latin": text if latin else None}
            for index, text in enumerate(texts, 1)
        ),
    )


def expected(texts: list[str | None], *, latin: bool, amount: Decimal = AMOUNT) -> list[tuple[object, ...]]:
    return [(index, DAY, index, amount if index % 2 else None, index % 2 == 0, index / 10, AT.replace(tzinfo=None), text, text, text if latin else None) for index, text in enumerate(texts, 1)]


@pytest.fixture
def scratch() -> Iterator[Scratch]:
    """A SQL Server scratch database with a ledger and a table of `Doc`s of this test's own, dropped after."""
    found = mssql(configured(), uuid.uuid4().hex[:8]).with_ledger()
    found.execute(f"CREATE TABLE {table_of(found)} ({COLUMNS})")
    yield found
    found.drop(table_of(found), found.ledger())


def table_of(scratch: Scratch) -> str:
    return f"etl_it_docs_{scratch.suffix}"


def publish(scratch: Scratch, write: FrameWrite, values: pl.DataFrame, root: Path) -> runs.Result:
    data = root / "data"
    data.mkdir()
    values.write_parquet(data / "docs.parquet")

    class Inputs(etl.Inputs):
        docs = etl.File(Doc, path="docs.parquet", empty="fail")

    class Outputs(etl.Outputs):
        docs = etl.Table(Doc, database="warehouse", table=table_of(scratch), write=write, key=("id",))

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
def test_text_publishes_exactly_and_is_counted_by_the_server(scratch: Scratch, tmp_path: Path, write: FrameWrite, texts: list[str | None], *, latin: bool) -> None:
    result = publish(scratch, write, docs(texts, latin=latin), tmp_path)
    assert result.record.status == "published", result.record
    assert result.record.publishes["warehouse"].counts == counted(write, len(texts))
    assert scratch.rows(EXACT.format(table=table_of(scratch))) == expected(texts, latin=latin)


@pytest.mark.parametrize("write", WRITES, ids=lambda write: type(write).__name__)
def test_a_decimal_of_38_digits_publishes_exactly(scratch: Scratch, tmp_path: Path, write: FrameWrite) -> None:
    texts: list[str | None] = ["hello world", UNITS_4001]
    result = publish(scratch, write, docs(texts, latin=False, amount=WIDEST), tmp_path)
    assert result.record.status == "published", result.record
    assert scratch.rows(EXACT.format(table=table_of(scratch))) == expected(texts, latin=False, amount=WIDEST)


@pytest.mark.parametrize("write", WRITES, ids=lambda write: type(write).__name__)
def test_a_varchar_in_code_page_1252_refuses_text_it_cannot_hold_before_anything_is_written(scratch: Scratch, tmp_path: Path, write: FrameWrite) -> None:
    operation = "upsert" if isinstance(write, etl.Upsert) else "insert"
    result = publish(scratch, write, docs(UNICODE["ascii-longest"], latin=True), tmp_path)
    assert result.record.status == "failed"
    refusal = (
        f"docs: {operation}: latin: 3 values hold characters code page cp1252 cannot store, such as '日', which the database would replace without"
        " an error: store them in a Unicode column, NVARCHAR or a VARCHAR with a UTF-8 collation"
    )
    assert result.record.reasons == [refusal]
    assert scratch.rows(f"SELECT COUNT(*) FROM {table_of(scratch)}") == [(0,)]


@pytest.mark.parametrize("write", WRITES, ids=lambda write: type(write).__name__)
@pytest.mark.parametrize("dropped", [2, 3], ids=["in-a-batch", "by-itself"])
def test_a_row_the_server_drops_and_counts_fails_the_count_check(scratch: Scratch, tmp_path: Path, write: FrameWrite, dropped: int) -> None:
    """Row 3 holds 4,001 UTF-16 units, so it goes by itself, and the others in a batch. The unique index on `code`
    ignores a duplicate key, with a warning and no error, and the table already holds `dropped`'s code, so the server
    writes, and counts, one row fewer than the run computed."""
    table = table_of(scratch)
    scratch.execute(
        f"CREATE UNIQUE INDEX etl_it_docs_code_{scratch.suffix} ON {table} (code) WITH (IGNORE_DUP_KEY = ON)",
        f"INSERT INTO {table} (id, day, code) VALUES (99, '2026-11-01', {dropped})",  # outside the interval ReplaceWhere replaces
    )
    texts: list[str | None] = ["hello world", "日本", UNITS_4001, "x", "a" * 100_000]
    result = publish(scratch, write, docs(texts, latin=False), tmp_path)
    what = "upsert" if isinstance(write, etl.Upsert) else "insert"
    assert result.record.status == "failed"
    error = result.record.publishes["warehouse"].error or ""
    assert f"RuntimeError: the {what} into {table} wrote 4 rows, and the run computed 5: nothing is committed" == error
    assert scratch.rows(f"SELECT id, code FROM {table}") == [(99, dropped)]
