"""Reading from real databases: each dialect's types arrive as the row declares them, the interval binds by name, a
naive datetime is read in its column's zone, and a query that returns more than its database's cap fails."""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from builders import Price, prices
from integration.scratch import Scratch, configured, mssql

import basis.job as etl
from basis.core.interval import Interval
from basis.core.settings import GivenSettings
from basis.core.storage import Root
from basis.job import ddl, runs
from basis.job.example import job as example
from basis.job.example import rows as example_rows
from basis.job.job import Job, read
from basis.job.outputs import Target

pytestmark = pytest.mark.integration


@pytest.fixture
def source(scratch: Scratch) -> Iterator[Scratch]:
    scratch.drop("etl_it_source")
    scratch.execute(ddl.create_table(Target(Price, "source", "etl_it_source", etl.Append()), scratch.dialect))
    yield scratch
    scratch.drop("etl_it_source")


def reading_job(dialect: str) -> Job:
    class Inputs(etl.Inputs):
        prices = etl.Query(Price, database="source", sql=f"sql/{dialect}_prices.sql", empty="allow")

    class Outputs(etl.Outputs):
        prices = etl.Table(Price, database="unused", table="prices", write=etl.Append())

    def transform(inputs: Inputs, _params: etl.Params) -> Outputs:
        return Outputs(prices=inputs.prices)

    return Job("reads", "tests.reads", etl.Params, Inputs, Outputs, transform, None, Path(__file__).parent)


def insert(scratch: Scratch, *rows: tuple[object, ...]) -> None:
    connection = scratch.connect()
    try:
        placeholders = ", ".join("?" * 5) if scratch.dialect == "mssql" else ", ".join(f":{index}" for index in range(1, 6))
        connection.cursor().executemany(f"INSERT INTO etl_it_source (day, security_id, price, note, booked_at) VALUES ({placeholders})", list(rows))
        connection.commit()
    finally:
        connection.close()


def load(scratch: Scratch, tmp_path: Path, *, max_rows: int = 100) -> runs.Result:
    """Read the source table, which the test made moments ago: Oracle's read-only transactions refuse a table defined in
    the seconds before (ORA-01466), so Oracle reads it with read_committed, and SQL Server with snapshot."""
    params = tmp_path / "params.json"
    params.write_text("{}", encoding="utf-8")
    isolation = {"isolation": "read_committed"} if scratch.dialect == "oracle" else {}
    settings = GivenSettings(databases={"source": scratch.settings | {"max_rows": max_rows} | isolation})
    return runs.run(reading_job(scratch.dialect), settings, runs.Request(params, Interval.parse("2026-11-05..2026-11-06")), snapshot_root=Root.at(tmp_path))


def test_a_query_reads_the_intervals_rows_as_the_row_declares_them(source: Scratch, tmp_path: Path) -> None:
    insert(
        source,
        (date(2026, 11, 5), "S1", Decimal("100.1234"), "é note", datetime(2026, 11, 5, 9, 30)),
        (date(2026, 11, 5), "S2", Decimal("0.5"), None, None),
        (date(2026, 11, 6), "S3", Decimal(1), None, None),
    )
    result = load(source, tmp_path)
    assert result.record.status == "checked", result.record
    frame = result.inputs["prices"].sort_values("security_id", ignore_index=True)
    expected = prices({"security_id": "S1", "price": 100.1234, "note": "é note", "booked_at": datetime(2026, 11, 5, 9, 30)}, {"security_id": "S2", "price": 0.5})
    assert frame.equals(expected)
    assert frame["booked_at"].tolist()[0] == datetime(2026, 11, 5, 14, 30, tzinfo=UTC)


def test_a_naive_time_its_zone_shows_twice_fails_the_run_with_the_fix(source: Scratch, tmp_path: Path) -> None:
    insert(source, (date(2026, 11, 5), "S1", Decimal(1), None, datetime(2026, 11, 1, 1, 30)))  # New York shows 1:30 twice that day
    result = load(source, tmp_path)
    assert result.record.status == "failed"
    assert result.record.error is not None
    assert "naive times America/New_York shows twice, as its clocks go back, such as 2026-11-01 01:30:00" in result.record.error
    assert "etl.Zone('America/New_York', ambiguous='earliest')" in result.record.error


def test_a_query_that_returns_more_than_its_cap_fails_the_run(source: Scratch, tmp_path: Path) -> None:
    insert(source, *((date(2026, 11, 5), f"S{index}", Decimal(1), None, None) for index in range(3)))
    result = load(source, tmp_path, max_rows=2)
    assert result.record.status == "failed"
    assert result.record.error is not None
    assert "returned more than 2 rows" in result.record.error


def test_the_example_job_publishes_to_sql_server_and_reruns_to_the_same_tables(tmp_path: Path) -> None:
    scratch = mssql(configured(), "example").with_ledger()  # the example writes fixed table names, in SQL Server only
    job = read(example)
    names = ("dbo.trades", "dbo.accounts", "daily_positions", "account_activity")
    scratch.drop(*names)
    for name, target in {"dbo.trades": Target(example_rows.Trade, "source", "dbo.trades", etl.Append()), "dbo.accounts": Target(example_rows.Account, "source", "dbo.accounts", etl.Append())}.items():
        scratch.execute(ddl.create_table(target, "mssql"))
        del name
    for table in job.outputs.declared().values():
        scratch.execute(ddl.create_table(table.target, "mssql"))
    fixture = Path(example.__file__).parent / "tests" / "fixture"
    connection = scratch.connect()
    try:
        cursor = connection.cursor()
        cursor.executemany("INSERT INTO dbo.accounts (account_id, name, closed) VALUES (?, ?, ?)", [("A1", "Alpha Fund", False), ("A2", "Beta Fund", False), ("A3", "Closed Fund", True)])
        trades = [line.split(",") for line in (fixture / "trades.csv").read_text(encoding="utf-8").splitlines()[1:]]
        cursor.executemany(
            "INSERT INTO dbo.trades (trade_id, account_id, security_id, trade_date, side, quantity, price, booked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(t[0], t[1], t[2], date.fromisoformat(t[3]), t[4], Decimal(t[5]), Decimal(t[6]), datetime.fromisoformat(t[7])) for t in trades],
        )
        connection.commit()
    finally:
        connection.close()
    params = Path(example.__file__).parent / "params" / "default.json"
    settings = GivenSettings(databases={"source": scratch.settings, "warehouse": scratch.settings})
    request = runs.Request(params, Interval.parse("2026-11-05..2026-11-06"), publish=True)
    try:
        for _ in range(2):
            result = runs.run(job, settings, request, snapshot_root=Root.at(tmp_path))
            assert result.record.status == "published", result.record
            assert scratch.rows("SELECT COUNT(*) FROM daily_positions") == [(3,)]
        assert scratch.rows("SELECT net_quantity, gross_notional, trades FROM daily_positions WHERE account_id = 'A1' AND security_id = 'XS0001'") == [(60.0, 14185.0, 2.0)]
        assert scratch.rows("SELECT account_id, last_booked_at FROM account_activity ORDER BY account_id") == [("A1", datetime(2026, 11, 5, 19, 2)), ("A2", datetime(2026, 11, 5, 16, 45))]
    finally:
        scratch.drop(*names, scratch.ledger())


def test_an_oracle_read_only_read_of_a_table_defined_moments_ago_says_why_it_failed(scratch: Scratch, tmp_path: Path) -> None:
    if scratch.dialect != "oracle":
        return
    scratch.drop("etl_it_source")
    scratch.execute(ddl.create_table(Target(Price, "source", "etl_it_source", etl.Append()), "oracle"))
    params = tmp_path / "params.json"
    params.write_text("{}", encoding="utf-8")
    settings = GivenSettings(databases={"source": scratch.settings})
    result = runs.run(reading_job("oracle"), settings, runs.Request(params, Interval.parse("2026-11-05..2026-11-06")), snapshot_root=Root.at(tmp_path))
    scratch.drop("etl_it_source")
    assert result.record.status == "failed"
    assert result.record.error is not None
    assert "ORA-01466" in result.record.error
