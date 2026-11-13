"""basis-dash's integration tier: a query's load against real SQL Server and Oracle, through the same `BASIS_TEST_*`
variables and scratch databases as basis-job's (packages/job/tests/integration/README.md). The load binds its date in
the database's own dialect, through the read login, in a transaction that only reads, off the event loop, and conforms
what the driver typed to its row: a `NUMBER(18,6)` and a `decimal(18,6)` a float, Oracle's `DATE` and its upper-case
names a date and lower-case names. Each test makes its own table and drops it after."""

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

import mssql_python
import oracledb
import pandas as pd
import pytest
from dash import html
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

import basis.dash as bd
from basis.dash import testing

pytestmark = pytest.mark.integration

type Dialect = Literal["mssql", "oracle"]


class ScratchDatabases(BaseSettings):
    """Where the integration tier may write: scratch databases only, never a shared one."""

    model_config = SettingsConfigDict(env_prefix="BASIS_TEST_", extra="ignore")

    mssql: SecretStr | None = None
    oracle_dsn: str | None = None
    oracle_user: str | None = None
    oracle_password: SecretStr | None = None


@dataclass(frozen=True)
class Scratch:
    dialect: Dialect
    settings: dict[str, object]
    connect: Callable[[], mssql_python.Connection | oracledb.Connection]
    table: str

    def execute(self, *statements: str) -> None:
        connection = self.connect()
        try:
            cursor = connection.cursor()
            for statement in statements:
                cursor.execute(statement)
            connection.commit()
        finally:
            connection.close()


def _scratch(dialect: Dialect) -> Scratch:
    databases, table = ScratchDatabases(), f"basis_dash_positions_{uuid.uuid4().hex[:8]}"
    if dialect == "mssql":
        if databases.mssql is None:
            pytest.fail("set BASIS_TEST_MSSQL to a connection string for a scratch SQL Server database (see packages/job/tests/integration/README.md)")
        connection_string = databases.mssql.get_secret_value()
        settings: dict[str, object] = {"kind": "mssql", "connection_string": connection_string, "isolation": "snapshot", "timeout_s": 10, "max_rows": 1000}
        return Scratch("mssql", settings, lambda: mssql_python.connect(connection_string, autocommit=False), table)
    if databases.oracle_dsn is None or databases.oracle_user is None or databases.oracle_password is None:
        pytest.fail("set BASIS_TEST_ORACLE_DSN, _USER and _PASSWORD for a scratch Oracle schema (see packages/job/tests/integration/README.md)")
    dsn, user, password = databases.oracle_dsn, databases.oracle_user, databases.oracle_password.get_secret_value()
    settings = {"kind": "oracle", "dsn": dsn, "user": user, "password": password, "mode": "thin", "isolation": "read_committed", "timeout_s": 10, "max_rows": 1000}
    return Scratch("oracle", settings, lambda: oracledb.connect(user=user, password=password, dsn=dsn), table)


@pytest.fixture(params=["mssql", "oracle"])
def scratch(request: pytest.FixtureRequest) -> Iterator[Scratch]:
    found = _scratch(request.param)
    text, number = ("varchar(12)", "decimal(18,6)") if found.dialect == "mssql" else ("VARCHAR2(12)", "NUMBER(18,6)")
    found.execute(
        f"CREATE TABLE {found.table} (account {text} NOT NULL, par {number} NULL, as_of DATE NOT NULL)",
        f"INSERT INTO {found.table} (account, par, as_of) VALUES ('A1', 1000000.5, DATE '2026-11-13')"
        if found.dialect == "oracle"
        else f"INSERT INTO {found.table} (account, par, as_of) VALUES ('A1', 1000000.5, '2026-11-13'), ('A2', NULL, '2026-11-13'), ('A3', 1, '2026-11-12')",
    )
    if found.dialect == "oracle":
        found.execute(f"INSERT INTO {found.table} (account, par, as_of) VALUES ('A2', NULL, DATE '2026-11-13')", f"INSERT INTO {found.table} (account, par, as_of) VALUES ('A3', 1, DATE '2026-11-12')")
    yield found
    if found.dialect == "mssql":
        found.execute(f"IF OBJECT_ID('{found.table}') IS NOT NULL DROP TABLE {found.table}")
    else:
        found.execute(f"BEGIN EXECUTE IMMEDIATE 'DROP TABLE {found.table} PURGE'; EXCEPTION WHEN OTHERS THEN NULL; END;")


class Position(bd.Row):
    account: str
    par: float | None
    as_of: date


def test_a_query_loads_from_a_real_database_in_its_dialect(scratch: Scratch, tmp_path: Path) -> None:
    bind = "%(as_of)s" if scratch.dialect == "mssql" else ":as_of"
    sql = tmp_path / "positions.sql"
    sql.write_text(f"SELECT account, par, as_of FROM {scratch.table} WHERE as_of = {bind} ORDER BY account\n", encoding="utf-8")
    warehouse = bd.database("warehouse", dialect=scratch.dialect, deadline_s=10, max_concurrent=2)
    positions = bd.query("positions", warehouse, str(sql), Position, binds={"as_of": date}, ttl_s=60, stale_s=0)
    app = bd.App(name="positions", title="Positions", settings=bd.Settings, resources=[warehouse], pages=[bd.page("/", "Positions", layout=html.Div())])
    with testing.client(app, settings=bd.Settings.given(databases={"warehouse": scratch.settings})) as client:
        portal = client.http.portal
        assert portal is not None  # inside the client's block

        async def load() -> pd.DataFrame:
            return await bd.load(positions, as_of=date(2026, 11, 13))

        frame = portal.call(load)
    assert list(frame.columns) == ["account", "par", "as_of"]
    assert frame["account"].tolist() == ["A1", "A2"]
    assert frame["par"].iloc[0] == 1_000_000.5
    assert pd.isna(frame["par"].iloc[1])
    assert frame["as_of"].tolist() == [date(2026, 11, 13)] * 2
