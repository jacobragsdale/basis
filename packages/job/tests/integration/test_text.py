"""Text through SQL Server, as basis reads and publishes it: emoji, CJK, accented Latin and characters outside
Unicode's Basic Multilingual Plane arrive exactly, from a `varchar(max)` or `varchar(n)` column with a UTF-8 collation,
an `nvarchar(max)` one and a `json` one, and accented Latin from `varchar` columns in code page 1252, two of them
filled to their width. Under mssql-python 1.17.0's defaults some arrived without them, and no error
(docs/spikes/sqlserver-json.md, section 2)."""

import sys
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

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        sys.platform != "linux",
        reason="off Linux, basis-core keeps mssql-python's default char fetch, which loses these characters, until the checklist in docs/spikes/sqlserver-json.md shows it exact",
    ),
]

COLUMNS = (
    "id int NOT NULL PRIMARY KEY, varchar_max varchar(max) COLLATE Latin1_General_100_CI_AS_SC_UTF8 NULL,"
    " varchar_n varchar(200) COLLATE Latin1_General_100_CI_AS_SC_UTF8 NULL, nvarchar_max nvarchar(max) NULL,"
    " latin varchar(40) COLLATE SQL_Latin1_General_CP1_CI_AS NULL, e_full varchar(10) COLLATE SQL_Latin1_General_CP1_CI_AS NULL,"
    " euro_full varchar(10) COLLATE SQL_Latin1_General_CP1_CI_AS NULL"
)
NAMES = "id, varchar_max, varchar_n, nvarchar_max, latin, e_full, euro_full"
EXACT = (
    "SELECT id, CAST(varchar_max AS nvarchar(max)), CAST(varchar_n AS nvarchar(200)), nvarchar_max, CAST(latin AS nvarchar(40)),"
    " CAST(e_full AS nvarchar(10)), CAST(euro_full AS nvarchar(10)) FROM {table} ORDER BY id"
)
"""What a table holds, read as nvarchar, which the driver's defaults read exactly."""
TEXTS = (
    '["\U0001f600"]',  # spike 0.6's case, which arrived as [""] through Arrow
    "ab\U0001f600cd",
    "a\U0001f600é日" * 3,
    "Zoë, naïve, Ångström; 日本語, 한국어; \U0001f680; \U00020000",  # accented Latin, CJK, an emoji, and CJK Extension B
)
LONG = "a" * 4094 + "\U0001f600b"
"""An emoji across the driver's first chunk of 4,095 UTF-16 units, which `fetch*` reads by. test_text_writes publishes
strings this long and longer."""
LATIN = "Zoë, café, naïve, Ångström, £5"
"""Text code page 1252 holds."""
FULL = {"e_full": "é" * 10, "euro_full": "€" * 10}
"""A `varchar(10)` in code page 1252 filled: `é` takes 2 bytes in UTF-8, and `€` 3, so a fetch as UTF-8 that sized its
buffer by the column's 10 bytes would cut them."""


class Text(etl.Row):
    id: int
    varchar_max: str | None
    varchar_n: str | None
    nvarchar_max: str | None
    latin: str | None
    e_full: str | None
    euro_full: str | None


def texts() -> pl.DataFrame:
    return frame(Text, *({"id": index, "varchar_max": text, "varchar_n": text, "nvarchar_max": text, "latin": LATIN} | FULL for index, text in enumerate(TEXTS, 1)))


def source_table(scratch: Scratch) -> str:
    return f"etl_it_texts_{scratch.suffix}"


@pytest.fixture
def source() -> Iterator[Scratch]:
    """A SQL Server table of `texts()`, written by the driver's defaults, which bind this text exactly only because no
    column's longest value is ASCII: one whose longest is binds as `varchar`, through the database's code page, which
    is why basis binds every string as `nvarchar` (test_text_writes)."""
    scratch = mssql(configured(), uuid.uuid4().hex[:8]).with_ledger()
    scratch.execute(f"CREATE TABLE {source_table(scratch)} ({COLUMNS})")
    insert(scratch, texts())
    yield scratch
    scratch.drop(source_table(scratch), scratch.ledger())


def insert(scratch: Scratch, values: pl.DataFrame) -> None:
    connection = scratch.connect()
    try:
        connection.cursor().executemany(f"INSERT INTO {source_table(scratch)} VALUES ({', '.join('?' * values.width)})", values.rows())
        connection.commit()
    finally:
        connection.close()


def database(scratch: Scratch) -> GivenSettings:
    return GivenSettings(databases={"warehouse": scratch.settings})


def copying_job(source: str, table: str, write: FrameWrite, folder: Path) -> Job:
    query = folder / "texts.sql"
    query.write_text(f"SELECT {NAMES} FROM {source}\n", encoding="utf-8")

    class Inputs(etl.Inputs):
        texts = etl.Query(Text, database="warehouse", sql=str(query), empty="fail")

    class Outputs(etl.Outputs):
        texts = etl.Table(Text, database="warehouse", table=table, write=write, key=("id",))

    def transform(inputs: Inputs, _params: etl.Params) -> Outputs:
        return Outputs(texts=inputs.texts)

    return Job("texts_it", "tests.texts_it", etl.Params, Inputs, Outputs, transform, None, Path(__file__).parent)


@pytest.mark.parametrize("write", [etl.Replace(), etl.Upsert()], ids=["insert", "staged"])
def test_a_job_reads_and_publishes_text_exactly(source: Scratch, tmp_path: Path, write: FrameWrite) -> None:
    table = f"etl_it_texts_copy_{source.suffix}"
    source.execute(f"CREATE TABLE {table} ({COLUMNS})")
    params = tmp_path / "params.json"
    params.write_text("{}", encoding="utf-8")
    request = runs.Request(params, Interval.parse("2026-11-05..2026-11-06"), publish=True)
    try:
        result = runs.run(copying_job(source_table(source), table, write, tmp_path), database(source), request, snapshot_root=Root.at(tmp_path))
        assert result.record.status == "published", result.record
        assert result.inputs["texts"].sort("id").rows() == texts().rows()
        assert source.rows(EXACT.format(table=table)) == texts().rows()
    finally:
        source.drop(table)


def test_an_apis_query_reads_text_exactly(source: Scratch) -> None:
    with open_database(database(source), "warehouse").statements(writes=False) as statements:
        read = pl.from_arrow(statements.query(f"SELECT {NAMES} FROM {source_table(source)} ORDER BY id", {}, timeout_s=60))
    assert isinstance(read, pl.DataFrame)
    assert read.rows() == texts().rows()


def test_a_cursor_on_a_basis_connection_reads_text_exactly(source: Scratch) -> None:
    """Without Arrow, as `fetchall` reads, which basis does of the ledger, and a caller may of its own connection."""
    long = frame(Text, {"id": len(TEXTS) + 1, "varchar_max": LONG, "nvarchar_max": LONG, "latin": LATIN} | FULL)
    insert(source, long)
    with open_database(database(source), "warehouse").reading() as reader:
        assert isinstance(reader, MssqlReader)
        with closing(reader.connection.cursor()) as cursor:
            cursor.execute(f"SELECT {NAMES} FROM {source_table(source)} ORDER BY id")
            read = [tuple(row) for row in cursor.fetchall()]
    assert read == texts().rows() + long.rows()


def test_a_json_column_reads_exactly() -> None:
    """The server sends a `json` column to this driver as `varchar(max)` UTF-8, whose default read lost `["😀"]`'s emoji
    (spike 0.6). Only SQL Server 2025 (17.x) and Azure SQL have the type."""
    scratch = mssql(configured(), uuid.uuid4().hex[:8])
    [(version, json_type)] = scratch.rows("SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(32)), TYPE_ID('json')")
    if json_type is None:
        pytest.skip(f"SQL Server {version} has no json type, which SQL Server 2025 (17.x) added")
    table = f"etl_it_json_{scratch.suffix}"
    document = TEXTS[0]
    scratch.execute(f"CREATE TABLE {table} (id int NOT NULL PRIMARY KEY, doc json NULL)")
    try:
        connection = scratch.connect()
        try:
            connection.cursor().execute(f"INSERT INTO {table} (id, doc) VALUES (1, ?)", (document,))
            connection.commit()
        finally:
            connection.close()
        with open_database(database(scratch), "warehouse").reading() as reader:
            assert isinstance(reader, MssqlReader)
            arrowed = reader.fetch(f"SELECT doc FROM {table}", None, what="a json column").item()
            with closing(reader.connection.cursor()) as cursor:
                cursor.execute(f"SELECT doc FROM {table}")
                fetched = [tuple(row) for row in cursor.fetchall()]
        assert (arrowed, fetched) == (document, [(document,)])
    finally:
        scratch.drop(table)
