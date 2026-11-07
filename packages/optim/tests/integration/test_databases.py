"""optim's reads of a real database, SQL Server and Oracle, through basis-core's dialects and the same `BASIS_TEST_*`
variables and scratch databases as basis-job's (packages/job/tests/integration/README.md): a dataset read per
portfolio loads a batch of portfolios in one transaction that only reads, its ids bound as one JSON array that the SQL
unpacks; its columns arrive named as its row model names them, and a driver's decimals as floats. Every test makes its
own table and drops it after."""

import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from scratch import Dialect, Scratch, scratch_database

from basis.core.databases import open_database
from basis.core.settings import GivenSettings
from optim.data.loader import Loader, load_data
from optim.datasets import HOLDINGS
from optim.sources.database import DatabaseSource

pytestmark = pytest.mark.integration

AS_OF = date(2026, 10, 16)
# Each dialect's unpacking of a batch's ids, bound as one JSON array, as the shared datasets' SQL unpacks them.
SQL = {
    "mssql": """SELECT p.account_id AS portfolio_id, p.security_id, p.quantity FROM {table} p
WHERE p.account_id IN (SELECT id FROM OPENJSON(%(portfolios)s) WITH (id VARCHAR(64) '$')) AND p.as_of_date = %(as_of_date)s""",
    "oracle": """SELECT p.account_id AS portfolio_id, p.security_id, p.quantity FROM {table} p
WHERE p.account_id IN (SELECT id FROM JSON_TABLE(:portfolios, '$[*]' COLUMNS (id VARCHAR2(64) PATH '$'))) AND p.as_of_date = :as_of_date""",
}


def reading(dialect: Dialect) -> Scratch:
    # Oracle reads at read_committed, which optim's settings refuse for a run: a read-only transaction refuses a table
    # made seconds before (ORA-01466), as each test's is, so this reads Oracle as basis-job's and basis-api's
    # integration tests do
    return scratch_database(dialect, isolation="snapshot" if dialect == "mssql" else "read_committed")


@pytest.fixture(params=["mssql", "oracle"])
def positions(request: pytest.FixtureRequest) -> Iterator[tuple[Scratch, str]]:
    """A positions table in the books' shape, which calls a portfolio an account, holding three portfolios' rows."""
    found, table = reading(request.param), f"positions_{uuid.uuid4().hex[:8]}"
    text, day = ("VARCHAR2", "DATE '2026-10-16'") if found.dialect == "oracle" else ("VARCHAR", "'2026-10-16'")
    found.execute(
        f"CREATE TABLE {table} (account_id {text}(64), security_id {text}(64), quantity DECIMAL(18, 4), as_of_date DATE)",
        *(
            f"INSERT INTO {table} VALUES ('{account}', '{security}', {quantity}, {day})"
            for account, security, quantity in (("P1", "USD", "1000000"), ("P1", "A", "5000"), ("P2", "USD", "250000.5"), ("P3", "USD", "1"))
        ),
    )
    yield found, table
    found.drop(table)


def test_a_batch_of_portfolios_reads_its_rows_alone_with_its_ids_bound_as_json(positions: tuple[Scratch, str], tmp_path: Path) -> None:
    scratch, table = positions
    (sql := tmp_path / "holdings.sql").write_text(SQL[scratch.dialect].format(table=table), encoding="utf-8")
    holdings = replace(HOLDINGS, source=scratch.dialect, query=sql)
    settings = GivenSettings(databases={scratch.dialect: scratch.settings})
    loader = Loader({scratch.dialect: DatabaseSource(open_database(settings, scratch.dialect), timeout_s=60)})

    frames, _, portfolio_ids = load_data(loader, {"holdings": holdings}, AS_OF, ["P1", "P2"])

    loaded = frames["holdings"].sort_values(["portfolio_id", "security_id"]).reset_index(drop=True)
    assert portfolio_ids == ["P1", "P2"]
    assert loaded.to_dict("records") == [
        {"portfolio_id": "P1", "security_id": "A", "quantity": 5000.0},
        {"portfolio_id": "P1", "security_id": "USD", "quantity": 1_000_000.0},
        {"portfolio_id": "P2", "security_id": "USD", "quantity": 250_000.5},
    ]
    assert loaded["quantity"].dtype == "float64"  # the driver's DECIMAL, validated into the row model's float
