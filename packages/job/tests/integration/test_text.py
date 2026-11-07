"""Text through SQL Server, as basis reads and publishes it: emoji, CJK, accented Latin and characters outside
Unicode's Basic Multilingual Plane arrive exactly, from a `varchar(max)` or `varchar(n)` column with a UTF-8 collation
and an `nvarchar(max)` one, and accented Latin from a `varchar` column in code page 1252. Under mssql-python 1.17.0's
defaults some arrived without them, and no error (docs/spikes/sqlserver-json.md, section 2). Windows keeps those
defaults (`basis.core.databases.mssql.fetches_char_as_utf8`), so these pass on Linux, where the tier runs, and fail
there."""

import uuid
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import polars as pl
import pytest
from integration.scratch import Scratch, configured, mssql

import basis.job as etl
from basis.core.databases import open_database
from basis.core.databases.mssql import MssqlReader
from basis.core.interval import Interval
from basis.core.settings import GivenSettings
from basis.core.storage import Root
from basis.job import runs
from basis.job.job import Job
from basis.job.outputs import FrameWrite
from basis.job.testing import frame

pytestmark = pytest.mark.integration

SOURCE = "etl_it_texts"
"""The table sql/mssql_texts.sql reads."""
COLUMNS = (
    "id int NOT NULL PRIMARY KEY, varchar_max varchar(max) COLLATE Latin1_General_100_CI_AS_SC_UTF8 NULL,"
    " varchar_n varchar(200) COLLATE Latin1_General_100_CI_AS_SC_UTF8 NULL, nvarchar_max nvarchar(max) NULL,"
    " latin varchar(40) COLLATE SQL_Latin1_General_CP1_CI_AS NULL"
)
SELECT = "SELECT id, varchar_max, varchar_n, nvarchar_max, latin FROM {table} ORDER BY id"
EXACT = "SELECT id, CAST(varchar_max AS nvarchar(max)), CAST(varchar_n AS nvarchar(200)), nvarchar_max, CAST(latin AS nvarchar(40)) FROM {table} ORDER BY id"
"""What a table holds, read as nvarchar, which the driver's defaults read exactly."""
TEXTS = (
    '["\U0001f600"]',  # spike 0.6's case, which arrived as [""] through Arrow
    "ab\U0001f600cd",
    "a\U0001f600é日" * 3,
    "Zoë, naïve, Ångström; 日本語, 한국어; \U0001f680; \U00020000",  # accented Latin, CJK, an emoji, and CJK Extension B
)
LONG = "a" * 4094 + "\U0001f600b"
"""An emoji across the driver's first chunk of 4,095 UTF-16 units, which `fetch*` reads by. No publish here writes it:
mssql-python 1.17.0's `executemany` counts one row for a batch with a string past 4,000 characters, so basis's count
check fails the publish, and nothing commits."""
LATIN = "Zoë, café, naïve, Ångström, £5"
"""Text code page 1252 holds."""


class Text(etl.Row):
    id: int
    varchar_max: str | None
    varchar_n: str | None
    nvarchar_max: str | None
    latin: str | None


def texts() -> pl.DataFrame:
    return frame(Text, *({"id": index, "varchar_max": text, "varchar_n": text, "nvarchar_max": text, "latin": LATIN} for index, text in enumerate(TEXTS, 1)))


@pytest.fixture
def source() -> Iterator[Scratch]:
    """A SQL Server table of `texts()`, written by the driver's defaults, which bind text exactly."""
    scratch = mssql(configured(), uuid.uuid4().hex[:8]).with_ledger()
    scratch.drop(SOURCE)
    scratch.execute(f"CREATE TABLE {SOURCE} ({COLUMNS})")
    insert(scratch, texts())
    yield scratch
    scratch.drop(SOURCE, scratch.ledger())


def insert(scratch: Scratch, values: pl.DataFrame) -> None:
    connection = scratch.connect()
    try:
        connection.cursor().executemany(f"INSERT INTO {SOURCE} VALUES (?, ?, ?, ?, ?)", values.rows())
        connection.commit()
    finally:
        connection.close()


def copying_job(table: str, write: FrameWrite) -> Job:
    class Inputs(etl.Inputs):
        texts = etl.Query(Text, database="warehouse", sql="sql/mssql_texts.sql", empty="fail")

    class Outputs(etl.Outputs):
        texts = etl.Table(Text, database="warehouse", table=table, write=write, key=("id",))

    def transform(inputs: Inputs, _params: etl.Params) -> Outputs:
        return Outputs(texts=inputs.texts)

    return Job("texts_it", "tests.texts_it", etl.Params, Inputs, Outputs, transform, None, Path(__file__).parent)


@pytest.mark.parametrize("write", [etl.Replace(), etl.Upsert()], ids=["insert", "staged"])
def test_a_job_reads_and_publishes_text_exactly(source: Scratch, tmp_path: Path, write: FrameWrite) -> None:
    table = f"etl_it_texts_{source.suffix}"
    source.execute(f"CREATE TABLE {table} ({COLUMNS})")
    params = tmp_path / "params.json"
    params.write_text("{}", encoding="utf-8")
    settings = GivenSettings(databases={"warehouse": source.settings})
    request = runs.Request(params, Interval.parse("2026-11-05..2026-11-06"), publish=True)
    try:
        result = runs.run(copying_job(table, write), settings, request, snapshot_root=Root.at(tmp_path))
        assert result.record.status == "published", result.record
        assert result.inputs["texts"].sort("id").rows() == texts().rows()
        assert source.rows(EXACT.format(table=table)) == texts().rows()
    finally:
        source.drop(table)


def test_an_apis_query_reads_text_exactly(source: Scratch) -> None:
    with open_database(GivenSettings(databases={"warehouse": source.settings}), "warehouse").statements(writes=False) as statements:
        read = pl.from_arrow(statements.query(SELECT.format(table=SOURCE), {}, timeout_s=60))
    assert isinstance(read, pl.DataFrame)
    assert read.rows() == texts().rows()


def test_a_cursor_on_a_basis_connection_reads_text_exactly(source: Scratch) -> None:
    """Without Arrow, as `fetchall` reads, which basis does of the ledger, and a caller may of its own connection."""
    long = frame(Text, {"id": len(TEXTS) + 1, "varchar_max": LONG, "nvarchar_max": LONG, "latin": LATIN})
    insert(source, long)
    with open_database(GivenSettings(databases={"warehouse": source.settings}), "warehouse").reading() as reader:
        assert isinstance(reader, MssqlReader)
        with closing(reader.connection.cursor()) as cursor:
            cursor.execute(SELECT.format(table=SOURCE))
            read = [tuple(row) for row in cursor.fetchall()]
    assert read == texts().rows() + long.rows()
