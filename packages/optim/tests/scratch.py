"""Scratch databases for optim's integration tier: real SQL Server and Oracle, configured only by the `BASIS_TEST_*`
variables that name scratch databases, the same as basis-job's (packages/job/tests/integration/README.md). Each test
makes its own tables, named with a suffix of its own, and drops them after."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import mssql_python
import oracledb
import pytest
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    """A scratch database: its settings as basis-core reads them, through one login that reads and writes, and raw
    SQL for setting up and reading back."""

    dialect: Dialect
    settings: dict[str, object]
    connect: Callable[[], mssql_python.Connection | oracledb.Connection]

    def execute(self, *scripts: str) -> None:
        """Each script's statements, which end with `;` and a line break, as `CREATE TABLE` statements print."""
        connection = self.connect()
        try:
            cursor = connection.cursor()
            for script in scripts:
                for statement in (part.strip().removesuffix(";") for part in script.split(";\n")):
                    if statement:
                        cursor.execute(statement)
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
        """Each table that exists; a PL/SQL block keeps its `;`, which `execute` would strip."""
        connection = self.connect()
        try:
            cursor = connection.cursor()
            for table in tables:
                mssql = f"IF OBJECT_ID('{table}') IS NOT NULL DROP TABLE {table}"
                oracle = f"BEGIN EXECUTE IMMEDIATE 'DROP TABLE {table} PURGE'; EXCEPTION WHEN OTHERS THEN NULL; END;"
                cursor.execute(mssql if self.dialect == "mssql" else oracle)
            connection.commit()
        finally:
            connection.close()

    def environ(self, name: str) -> dict[str, str]:
        """The settings as the environment gives them, for the database `name`: `BASIS_DATABASES__NAME__*`."""
        prefix = f"BASIS_DATABASES__{name.upper()}__"
        flat = {key: value for key, value in self.settings.items() if key != "write"}
        if isinstance(write := self.settings.get("write"), dict):
            flat |= {f"write__{key}": value for key, value in write.items()}
        return {prefix + key.upper(): str(value) for key, value in flat.items()}


def scratch_database(dialect: Dialect, *, isolation: str, ledger: str | None = None) -> Scratch:
    """The scratch database of `dialect`, read at `isolation`, and written through the same login, with `ledger` as its
    ledger where given."""
    databases = ScratchDatabases()
    if dialect == "mssql":
        if databases.mssql is None:
            pytest.fail("set BASIS_TEST_MSSQL to a connection string for a scratch SQL Server database (see packages/job/tests/integration/README.md)")
        connection_string = databases.mssql.get_secret_value()
        settings: dict[str, object] = {"kind": "mssql", "connection_string": connection_string, "isolation": isolation, "timeout_s": 60, "max_rows": 1000}
        settings |= {"write": {"connection_string": connection_string, "ledger": ledger}} if ledger is not None else {}
        return Scratch("mssql", settings, lambda: mssql_python.connect(connection_string, autocommit=False))
    if databases.oracle_dsn is None or databases.oracle_user is None or databases.oracle_password is None:
        pytest.fail("set BASIS_TEST_ORACLE_DSN, _USER and _PASSWORD for a scratch Oracle schema (see packages/job/tests/integration/README.md)")
    dsn, user, password = databases.oracle_dsn, databases.oracle_user, databases.oracle_password.get_secret_value()
    oracle: dict[str, object] = {"kind": "oracle", "dsn": dsn, "user": user, "password": password, "mode": "thin", "isolation": isolation, "timeout_s": 60, "max_rows": 1000}
    oracle |= {"write": {"user": user, "password": password, "ledger": ledger}} if ledger is not None else {}
    return Scratch("oracle", oracle, lambda: oracledb.connect(user=user, password=password, dsn=dsn))
