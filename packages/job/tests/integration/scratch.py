"""Scratch databases for the integration tier: real SQL Server and Oracle, configured only by `BASIS_TEST_*` variables
that name scratch databases (see README.md)."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import mssql_python
import oracledb
import pandas as pd
import pytest
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from basis.core.interval import Interval
from basis.core.settings import GivenSettings
from basis.core.storage import Root
from basis.job import ddl, runs
from basis.job.job import Job

oracledb.defaults.fetch_lobs = False  # the raw reads back compare CLOB values as strings, not LOB handles


class TestDatabases(BaseSettings):
    """Where the integration tier may write: scratch databases only, never a shared one."""

    model_config = SettingsConfigDict(env_prefix="BASIS_TEST_", extra="ignore")

    mssql: SecretStr | None = None
    """A connection string to a scratch SQL Server database whose login may create tables."""
    oracle_dsn: str | None = None
    oracle_user: str | None = None
    oracle_password: SecretStr | None = None
    mongo: SecretStr | None = None
    """A connection string to a scratch Mongo server, whose collections a test creates and drops: step 5.5's."""


@dataclass(frozen=True)
class Scratch:
    """One scratch database, as a test sees it: its settings for a run, and raw SQL for setting up and reading back."""

    dialect: Literal["mssql", "oracle"]
    settings: dict[str, object]
    connect: Callable[[], mssql_python.Connection | oracledb.Connection]
    suffix: str

    def execute(self, *statements: str) -> None:
        connection = self.connect()
        try:
            cursor = connection.cursor()
            for script in statements:
                for statement in (part.strip() for part in script.split(";\n")):
                    if statement:
                        cursor.execute(statement.removesuffix(";"))
            connection.commit()
        finally:
            connection.close()

    def rows(self, sql: str) -> list[tuple[object, ...]]:
        connection = self.connect()
        try:
            cursor = connection.cursor()
            cursor.execute(sql)
            return [tuple(row) for row in cursor.fetchall()]
        finally:
            connection.close()

    def drop(self, *tables: str) -> None:
        for table in tables:
            mssql = f"IF OBJECT_ID('{table}') IS NOT NULL DROP TABLE {table}"
            oracle = f"BEGIN EXECUTE IMMEDIATE 'DROP TABLE {table} PURGE'; EXCEPTION WHEN OTHERS THEN NULL; END;"
            exists = mssql if self.dialect == "mssql" else oracle
            connection = self.connect()
            try:
                connection.cursor().execute(exists)
                connection.commit()
            finally:
                connection.close()

    def ledger(self) -> str:
        return f"etl_ledger_{self.suffix}"

    def with_ledger(self) -> Scratch:
        self.execute(ddl.ledger(self.ledger(), self.dialect))
        return self


def configured() -> TestDatabases:
    return TestDatabases()


def mssql(databases: TestDatabases, suffix: str) -> Scratch:
    if databases.mssql is None:
        pytest.fail("set BASIS_TEST_MSSQL to a connection string for a scratch SQL Server database (see tests/integration/README.md)")
    connection_string = databases.mssql.get_secret_value()
    settings: dict[str, object] = {"kind": "mssql", "connection_string": connection_string, "isolation": "snapshot", "timeout_s": 60, "max_rows": 100_000}
    settings["write"] = {"connection_string": connection_string, "ledger": f"etl_ledger_{suffix}"}
    return Scratch("mssql", settings, lambda: mssql_python.connect(connection_string, autocommit=False), suffix)


def oracle(databases: TestDatabases, suffix: str) -> Scratch:
    if databases.oracle_dsn is None or databases.oracle_user is None or databases.oracle_password is None:
        pytest.fail("set BASIS_TEST_ORACLE_DSN, _USER and _PASSWORD for a scratch Oracle schema (see tests/integration/README.md)")
    dsn, user, password = databases.oracle_dsn, databases.oracle_user, databases.oracle_password.get_secret_value()
    settings: dict[str, object] = {"kind": "oracle", "dsn": dsn, "user": user, "password": password, "mode": "thin", "isolation": "read_only", "timeout_s": 60, "max_rows": 100_000}
    settings["write"] = {"user": user, "password": password, "ledger": f"etl_ledger_{suffix}"}
    return Scratch("oracle", settings, lambda: oracledb.connect(user=user, password=password, dsn=dsn), suffix)


def run(job: Job, databases: dict[str, Scratch], frame: pd.DataFrame, root: Path, *, again: bool = False, interval: str = "2026-11-05..2026-11-06") -> runs.Result:  # noqa: PLR0913 -- a test's knobs, each by keyword
    """Run `job` on `frame` as its `prices` input, publishing to `databases` by name."""
    folder = root / "data"
    folder.mkdir(exist_ok=True)
    frame.to_parquet(folder / "prices.parquet", index=False)
    params = root / "params.json"
    params.write_text("{}", encoding="utf-8")
    request = runs.Request(params, Interval.parse(interval), folder, publish=True, again=again)
    settings = GivenSettings(databases={name: found.settings for name, found in databases.items()})
    return runs.run(job, settings, request, snapshot_root=Root.at(root / "snapshots"))
