"""basis-core's databases against real SQL Server and Oracle (packages/job/tests/integration/README.md sets them up):
each driver's failure is its kind as the statement raises it, inside a transaction's block too, so a caller catches a
`Conflict` where it happens; a rollback that fails after the block's own failure leaves that failure the one raised;
and SQL Server's messages are English whatever language a login defaults to, so a duplicate key is still a
`Conflict`. Each test makes its own ledger table and drops it after."""

import logging
import uuid
from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import mssql_python
import oracledb
import pytest
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from basis.core import ddl
from basis.core.databases import Counts, Database, LedgerEntry, open_database
from basis.core.databases.mssql import MssqlWriter
from basis.core.databases.oracle import OracleWriter
from basis.core.errors import Conflict
from basis.core.settings import GivenSettings

pytestmark = pytest.mark.integration

type Dialect = Literal["mssql", "oracle"]
NAME = "warehouse"


class ScratchDatabases(BaseSettings):
    """Where the integration tier may write: scratch databases only, never a shared one."""

    model_config = SettingsConfigDict(env_prefix="BASIS_TEST_", extra="ignore")

    mssql: SecretStr | None = None
    oracle_dsn: str | None = None
    oracle_user: str | None = None
    oracle_password: SecretStr | None = None


@dataclass
class Scratch:
    """A scratch database with a ledger table of this test's own, its settings through one login that reads and
    writes, and raw SQL around it."""

    dialect: Dialect
    settings: dict[str, object]
    connect: Callable[[], mssql_python.Connection | oracledb.Connection]
    ledger: str

    def execute(self, *statements: str) -> None:
        connection = self.connect()
        try:
            cursor = connection.cursor()
            for statement in statements:
                cursor.execute(statement)
            connection.commit()
        finally:
            connection.close()

    def database(self, **changed: object) -> Database:
        return open_database(GivenSettings(databases={NAME: self.settings | changed}), NAME)


def _mssql(ledger: str) -> Scratch:
    databases = ScratchDatabases()
    if databases.mssql is None:
        pytest.fail("set BASIS_TEST_MSSQL to a connection string for a scratch SQL Server database (see packages/job/tests/integration/README.md)")
    connection_string = databases.mssql.get_secret_value()
    settings: dict[str, object] = {
        "kind": "mssql", "connection_string": connection_string, "isolation": "read_committed", "timeout_s": 10, "max_rows": 1000,
        "write": {"connection_string": connection_string, "ledger": ledger},
    }  # fmt: skip
    return Scratch("mssql", settings, lambda: mssql_python.connect(connection_string, autocommit=False), ledger)


def _oracle(ledger: str) -> Scratch:
    databases = ScratchDatabases()
    if databases.oracle_dsn is None or databases.oracle_user is None or databases.oracle_password is None:
        pytest.fail("set BASIS_TEST_ORACLE_DSN, _USER and _PASSWORD for a scratch Oracle schema (see packages/job/tests/integration/README.md)")
    dsn, user, password = databases.oracle_dsn, databases.oracle_user, databases.oracle_password.get_secret_value()
    settings: dict[str, object] = {
        "kind": "oracle", "dsn": dsn, "user": user, "password": password, "mode": "thin", "isolation": "read_committed", "timeout_s": 10, "max_rows": 1000,
        "write": {"user": user, "password": password, "ledger": ledger},
    }  # fmt: skip
    return Scratch("oracle", settings, lambda: oracledb.connect(user=user, password=password, dsn=dsn), ledger)


@pytest.fixture(params=["mssql", "oracle"])
def scratch(request: pytest.FixtureRequest) -> Generator[Scratch]:
    ledger = f"basis_ledger_{uuid.uuid4().hex[:8]}"
    found = (_mssql if request.param == "mssql" else _oracle)(ledger)
    found.execute(*(statement for statement in ddl.ledger(ledger, found.dialect).split(";\n") if statement.strip()))
    yield found
    found.execute(f"DROP TABLE {ledger}")


RUN_ID = "019a5c3e-0000-7000-8000-000000000001"  # 36 characters, as every run's, which the ledger's CHAR(36) holds unpadded
ENTRY = LedgerEntry(RUN_ID, "prices", "prices", "prices", None, rerun_safe=True, counts=Counts(), published_at=datetime(2026, 11, 7, tzinfo=UTC), version="0.0.0")


def test_a_duplicate_key_is_a_conflict_its_caller_catches_inside_the_writing_block(scratch: Scratch) -> None:
    caught: list[Conflict] = []
    with scratch.database().writing() as writer:
        writer.record(ENTRY)
        try:
            writer.record(ENTRY)
        except Conflict as recorded:
            caught.append(recorded)
        assert writer.published(RUN_ID) is True  # the transaction goes on after the caught statement
    assert [(found.resource, found.status) for found in caught] == [(NAME, None)]


def test_a_rollback_that_fails_after_the_blocks_failure_leaves_the_statements_kind(scratch: Scratch, caplog: pytest.LogCaptureFixture) -> None:
    """The writer's connection is gone when the block ends, so its rollback fails too: the Conflict is the one raised,
    and the rollback's failure is logged."""

    def record_twice() -> None:
        with scratch.database().writing() as writer:
            writer.record(ENTRY)
            assert isinstance(writer, MssqlWriter | OracleWriter)
            try:
                writer.record(ENTRY)
            except Conflict:
                writer.connection.raw.close()  # as a connection lost after its statement failed
                raise

    with caplog.at_level(logging.WARNING), pytest.raises(Conflict):
        record_twice()
    assert f"database {NAME} could not roll back; the failure that ended its work, if any, is the one raised" in [record.getMessage() for record in caplog.records]


def test_a_logins_own_language_leaves_a_duplicate_key_a_conflict(scratch: Scratch) -> None:
    """A login whose default language is German gets SQL Server's messages in German, but basis's connections set the
    session's language to English, so a duplicate key is still told apart from another constraint."""
    if scratch.dialect != "mssql":
        return
    login, password = f"basis_deutsch_{uuid.uuid4().hex[:8]}", "Basis-deutsch-1234"
    scratch.execute(
        f"CREATE LOGIN {login} WITH PASSWORD = '{password}', CHECK_POLICY = OFF, DEFAULT_LANGUAGE = Deutsch",
        f"CREATE USER {login} FOR LOGIN {login}",
        f"GRANT SELECT, INSERT ON {scratch.ledger} TO {login}",
    )
    connection_string = str(scratch.settings["connection_string"])
    parts = [part for part in connection_string.split(";") if part.split("=", 1)[0].strip().lower() not in {"uid", "pwd", "user id", "password"}]
    german = ";".join([*parts, f"UID={login}", f"PWD={password}"])
    try:
        raw = mssql_python.connect(german, autocommit=True)
        try:
            cursor = raw.cursor()
            cursor.execute("SELECT @@LANGUAGE")
            found = cursor.fetchone()
        finally:
            raw.close()
        language = found[0] if found is not None else None
        assert language == "Deutsch"  # the login's own sessions are German, without basis
        database = scratch.database(write={"connection_string": german, "ledger": scratch.ledger})

        def record_twice() -> None:
            with database.writing() as writer:
                writer.record(ENTRY)
                writer.record(ENTRY)

        with pytest.raises(Conflict):
            record_twice()
    finally:
        kill = (
            f"DECLARE @kill nvarchar(max) = N''; SELECT @kill += N'KILL ' + CAST(session_id AS nvarchar(10)) + N';' FROM sys.dm_exec_sessions WHERE login_name = N'{login}';"
            " EXEC (@kill)"
        )  # the driver pools its connections, which stay logged in after they close
        admin = mssql_python.connect(connection_string, autocommit=True)  # KILL refuses to run in a transaction
        try:
            for sql in (kill, f"DROP USER {login}", f"DROP LOGIN {login}"):
                admin.cursor().execute(sql)
        finally:
            admin.close()
