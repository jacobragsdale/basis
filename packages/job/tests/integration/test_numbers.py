"""Numbers through real databases. Every numeric type a query reads arrives as a float, the nearest double to its
value: a decimal through its text, since Arrow's own cast of 2.675 misses it, a float32 through its shortest text, and
an integer exactly, while it is whole and of 2^53 or less, beyond which the read fails, naming the column. A float a
run publishes binds by its column's type: into a decimal as its shortest text, which the column's scale rounds, as
2.675 is 2.68 in a `DECIMAL(10,2)`; into an integer as a whole number's text; into a float's column as a float, NaN
as NULL; and an infinity, or a fraction an integer column would drop, is refused before anything is written."""

import random
from collections.abc import Iterator, Sequence
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pytest
from integration.scratch import Scratch, run

import basis.job as etl
from basis.core import frames
from basis.core.databases import mssql, open_database, oracle
from basis.core.frames import NumberError
from basis.core.settings import GivenSettings
from basis.job.job import Job
from basis.job.testing import frame

pytestmark = pytest.mark.integration

SAMPLE = [Decimal(value).scaleb(-2) for value in random.Random(11).sample(range(-(10**16), 10**16), 2_000)]  # noqa: S311 -- reproducible test data, not secrets
"""Values of a `decimal(18,2)`, each of which reads as `float(str(value))`."""


@pytest.fixture
def numbers(scratch: Scratch) -> Iterator[tuple[Scratch, str]]:
    table = f"numbers_{scratch.suffix}"
    yield scratch, table
    scratch.drop(table)


def read(scratch: Scratch, sql: str) -> pd.DataFrame:
    with open_database(GivenSettings(databases={"source": scratch.settings | {"isolation": "read_committed", "max_rows": 10_000}}), "source").reading() as reader:
        return reader.fetch(sql, None, what="the numbers")


MSSQL_TYPES = {"d3810": "decimal(38,10)", "n182": "numeric(18,2)", "mo": "money", "sm": "smallmoney", "f": "float", "r": "real", "i": "int", "b": "bigint", "s": "smallint", "t": "tinyint"}
ORACLE_TYPES = {"d3810": "NUMBER(38,10)", "n182": "NUMBER(18,2)", "n": "NUMBER", "bd": "BINARY_DOUBLE", "bf": "BINARY_FLOAT", "i": "NUMBER(10)", "b": "NUMBER(19)"}


def test_every_numeric_type_reads_as_the_nearest_double_to_its_value(numbers: tuple[Scratch, str]) -> None:
    """2.675 from a decimal reads as 2.675 exactly, so `== 2.675` holds, where Arrow's cast gave 2.6750000000000003;
    a `real` or `BINARY_FLOAT` 0.1 reads as 0.1, not 0.10000000149011612; and every value of a random sample of a
    `decimal(18,2)` reads as `float(str(value))`."""
    scratch, table = numbers
    types = MSSQL_TYPES if scratch.dialect == "mssql" else ORACLE_TYPES
    scratch.execute(f"CREATE TABLE {table} (id {'int' if scratch.dialect == 'mssql' else 'NUMBER(10)'} NOT NULL, {', '.join(f'{name} {kind}' for name, kind in types.items())})")
    first = {
        name: Decimal("0.1") if kind in {"real", "BINARY_FLOAT"} else Decimal(12) if name in {"i", "b", "s", "t"} else Decimal("2.67") if name == "n182" else Decimal("2.675")
        for name, kind in types.items()
    }
    rows = [(0, *first.values())] + [(index, *(value if name in {"d3810", "n182", "mo", "n"} else None for name in types)) for index, value in enumerate(SAMPLE[:1000], 1)]
    insert(scratch, table, ["id", *types], rows)
    found = read(scratch, f"SELECT {', '.join(types)} FROM {table} ORDER BY id").rename(columns=str.lower)  # Oracle's are upper case, until a row's conform
    assert all(str(dtype) == "float64" for dtype in found.dtypes)
    assert found.iloc[0].to_dict() == {name: float(str(value)) for name, value in first.items()}
    assert found.iloc[0]["d3810"] == 2.675
    for name in ("d3810", "n182") + (("mo",) if scratch.dialect == "mssql" else ("n",)):
        assert found[name].iloc[1:].tolist() == [float(str(value)) for value in SAMPLE[:1000]], name


def insert(scratch: Scratch, table: str, names: list[str], rows: Sequence[tuple[object, ...]]) -> None:
    marks = ", ".join("?" * len(names)) if scratch.dialect == "mssql" else ", ".join(f":{index}" for index in range(1, len(names) + 1))
    connection = scratch.connect()
    try:
        connection.cursor().executemany(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({marks})", rows)
        connection.commit()
    finally:
        connection.close()


def test_a_whole_number_beyond_2_53_fails_the_read_naming_its_column(numbers: tuple[Scratch, str]) -> None:
    scratch, table = numbers
    scratch.execute(f"CREATE TABLE {table} (id {'bigint' if scratch.dialect == 'mssql' else 'NUMBER(19)'} NOT NULL)")
    insert(scratch, table, ["id"], [(2**53 + 1,)])
    with pytest.raises(NumberError, match=r"(?i)the numbers: column id holds whole numbers beyond 2\^53, which a float holds only roughly: select it as text if it names something"):
        read(scratch, f"SELECT id FROM {table}")
    as_text = "CAST(id AS varchar(20))" if scratch.dialect == "mssql" else "TO_CHAR(id)"
    assert read(scratch, f"SELECT {as_text} AS id FROM {table}").rename(columns=str.lower)["id"].tolist() == [str(2**53 + 1)]


class Priced(etl.Row):
    id: str
    day: date
    amount: float | None
    whole: float | None
    ratio: float | None


def publishing(table: str) -> Job:
    class Inputs(etl.Inputs):
        prices = etl.File(Priced, path="prices.parquet", empty="fail")

    class Outputs(etl.Outputs):
        priced = etl.Table(Priced, database="warehouse", table=table, write=etl.Append())

    def transform(inputs: Inputs, _params: etl.Params) -> Outputs:
        return Outputs(priced=inputs.prices)

    return Job("numbers_it", "tests.numbers_it", etl.Params, Inputs, Outputs, transform, None, Path(__file__).parent)


@pytest.fixture
def priced(scratch: Scratch) -> Iterator[tuple[Scratch, str]]:
    table = f"priced_{scratch.suffix}"
    if scratch.dialect == "mssql":
        scratch.execute(f"CREATE TABLE {table} (id int NOT NULL, day date NOT NULL, amount decimal(10,2) NULL, whole int NULL, ratio float NULL)")
    else:
        scratch.execute(f"CREATE TABLE {table} (id NUMBER(10) NOT NULL, day DATE NOT NULL, amount NUMBER(10,2), whole NUMBER(10), ratio BINARY_DOUBLE)")
    yield scratch, table
    scratch.drop(table)


DAY = date(2026, 11, 5)


def test_a_float_binds_by_its_columns_type_and_the_scale_of_a_decimal_rounds_its_shortest_text(priced: tuple[Scratch, str], tmp_path: Path) -> None:
    """2.675's double is 2.67499999999999982236431605997495353221893310546875, which a `DECIMAL(10,2)` given the float
    keeps as 2.67; given its shortest text, the column rounds it half away from zero, to 2.68, as the number reads."""
    scratch, table = priced
    rows = frame(
        Priced,
        {"id": "1", "day": DAY, "amount": 2.675, "whole": 3.0, "ratio": 0.1},
        {"id": "2", "day": DAY, "amount": -2.675, "whole": -7.0, "ratio": None},
        {"id": "3", "day": DAY, "amount": 1e-7, "whole": None, "ratio": 1 / 3},
    )
    result = run(publishing(table), {"warehouse": scratch}, rows, tmp_path)
    assert result.record.status == "published", result.record
    stored = [(row[0], Decimal(str(row[1])).quantize(Decimal("0.01")), row[2], row[3]) for row in scratch.rows(f"SELECT id, amount, whole, ratio FROM {table} ORDER BY id")]
    assert stored == [(1, Decimal("2.68"), 3, 0.1), (2, Decimal("-2.68"), -7, None), (3, Decimal("0.00"), None, 1 / 3)]
    assert Decimal("2.675").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) == stored[0][1]


@pytest.mark.parametrize("name", ["ratio", "amount"])
def test_an_infinite_float_is_refused_naming_its_column_before_anything_is_written(priced: tuple[Scratch, str], tmp_path: Path, name: str) -> None:
    """A float column holds no infinity: refused as its input loads, as a transform's output is, and checked against the
    table too."""
    scratch, table = priced
    rows = frame(Priced, {"id": "1", "day": DAY, "amount": 1.0, "whole": 1.0, "ratio": 1.0}).assign(**{name: [float("-inf")]})
    result = run(publishing(table), {"warehouse": scratch}, rows, tmp_path)
    assert result.record.status == "failed"
    assert f"{name}: infinite values" in (result.record.error or "")
    assert scratch.rows(f"SELECT COUNT(*) FROM {table}") == [(0,)]


@pytest.mark.parametrize(
    ("changed", "reason"),
    [
        ({"amount": 1e8}, "priced: insert: amount: values up to 100000000.00, and {decimal}(10,2) holds less than 100000000"),
        ({"id": "A1"}, "priced: insert: id: 1 values that are not whole numbers, which {whole} column id cannot store, such as 'A1'"),
    ],
    ids=["beyond-a-decimals-precision", "an-identifier-that-is-no-number"],
)
def test_a_float_or_an_identifier_a_column_cannot_store_is_refused_before_anything_is_written(priced: tuple[Scratch, str], tmp_path: Path, changed: dict[str, object], reason: str) -> None:
    scratch, table = priced
    types = {"float": "FLOAT", "decimal": "DECIMAL", "whole": "INT"} if scratch.dialect == "mssql" else {"float": "BINARY_DOUBLE", "decimal": "NUMBER", "whole": "NUMBER"}
    rows = frame(Priced, {"id": "1", "day": DAY, "amount": 1.0, "whole": 1.0, "ratio": 1.0})
    for name, value in changed.items():
        rows[name] = pd.Series([value], dtype=object)
    result = run(publishing(table), {"warehouse": scratch}, rows, tmp_path)
    assert result.record.status == "failed"
    assert result.record.reasons == [reason.format(**types)]
    assert scratch.rows(f"SELECT COUNT(*) FROM {table}") == [(0,)]


def test_a_fraction_an_integer_column_would_drop_is_refused_in_sql_server(priced: tuple[Scratch, str], tmp_path: Path) -> None:
    """SQL Server's integer types take a whole float alone; Oracle has none, and its NUMBER(10) rounds as a decimal does."""
    scratch, table = priced
    result = run(publishing(table), {"warehouse": scratch}, frame(Priced, {"id": "1", "day": DAY, "whole": 2.5}), tmp_path)
    if scratch.dialect == "mssql":
        assert result.record.reasons == ["priced: insert: whole: 1 values with a fraction, which INT column whole does not keep: round them in the transform, as the data needs"]
        assert scratch.rows(f"SELECT COUNT(*) FROM {table}") == [(0,)]
    else:
        assert result.record.status == "published", result.record
        assert scratch.rows(f"SELECT whole FROM {table}") == [(3,)]


def test_a_copys_batches_stream_the_rows_a_fetch_reads_in_batches_of_fetch_rows(numbers: tuple[Scratch, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """A copy (step 12.6) streams a query's rows batch by batch; each dialect's batches hold what its fetch reads."""
    scratch, table = numbers
    for module in (mssql, oracle):
        monkeypatch.setattr(module, "FETCH_ROWS", 400)  # 1,500 rows in four batches
    scratch.execute(f"CREATE TABLE {table} (id {'int' if scratch.dialect == 'mssql' else 'NUMBER(10)'} NOT NULL, d {'decimal(18,2)' if scratch.dialect == 'mssql' else 'NUMBER(18,2)'})")
    insert(scratch, table, ["id", "d"], [(index, value) for index, value in enumerate(SAMPLE[:1500])])
    sql = f"SELECT id, d FROM {table} ORDER BY id"
    with open_database(GivenSettings(databases={"source": scratch.settings | {"isolation": "read_committed", "max_rows": 10_000}}), "source").reading() as reader:
        fetched = reader.fetch(sql, None, what="the numbers")
        batches = list(reader.batches(sql, None, what="the numbers"))
    assert sum(batch.num_rows for batch in batches) == 1500
    assert len(batches) >= 4
    assert all(batch.num_rows <= 400 for batch in batches)
    streamed = frames.from_arrow(frames.floats(pa.Table.from_batches(batches), what="the numbers"))
    assert streamed.equals(fetched)
