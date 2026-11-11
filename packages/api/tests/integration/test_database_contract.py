"""The contract of a database's statements, run against the fake database in every run and, in the integration tier,
against SQL Server and Oracle (packages/job/tests/integration/README.md sets them up): each kind of failure a database raises
reaches the caller the same from both, with the same resource, no status and the same detail, so a test that makes the
fake fail sees what production would. The fake fails because the test says so; the real database, because the
statement does what makes it fail."""

import threading
import time
import uuid
from collections.abc import Callable, Generator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Literal, Protocol

import mssql_python
import oracledb
import pytest
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from basis.api import fakes
from basis.core.databases import Statements, open_database
from basis.core.databases.base import Bind
from basis.core.databases.failures import failure
from basis.core.errors import Conflict, Denied, Failed, Rejected, ServiceError, Timeout, Unavailable
from basis.core.settings import GivenSettings

type Dialect = Literal["fake", "mssql", "oracle"]
type Case = Literal["unique-key", "not-null", "timeout", "syntax", "login", "permission", "unreachable"]
NAME = "warehouse"
KIND: dict[Case, type[ServiceError]] = {
    "unique-key": Conflict, "not-null": Rejected, "timeout": Timeout, "syntax": Failed, "login": Denied, "permission": Denied, "unreachable": Unavailable,
}  # fmt: skip
DIALECTS = [pytest.param("fake", id="fake"), pytest.param("mssql", id="mssql", marks=pytest.mark.integration), pytest.param("oracle", id="oracle", marks=pytest.mark.integration)]


class Port(Protocol):
    def statements(self, *, writes: bool) -> AbstractContextManager[Statements]: ...


class ScratchDatabases(BaseSettings):
    """Where the integration tier may write: scratch databases only, never a shared one."""

    model_config = SettingsConfigDict(env_prefix="BASIS_TEST_", extra="ignore")

    mssql: SecretStr | None = None
    oracle_dsn: str | None = None
    oracle_user: str | None = None
    oracle_password: SecretStr | None = None


@dataclass
class Contract:
    """One database the suite runs against: how to reach it as basis does, with settings changed for a case, the SQL
    of each case, and how to make each case's statement fail where it is the fake."""

    dialect: Dialect
    settings: dict[str, object] = field(default_factory=dict[str, object])
    table: str = field(default_factory=lambda: f"basis_contract_{uuid.uuid4().hex[:8]}")
    fake: fakes.Database = field(default_factory=fakes.Database)
    cleanup: list[Callable[[], None]] = field(default_factory=list[Callable[[], None]])

    def port(self, **changed: object) -> Port:
        if self.dialect == "fake":
            self.fake.opened_as(NAME)
            return self.fake
        return open_database(GivenSettings(databases={NAME: self.settings | changed}), NAME)

    def sql(self, case: Case) -> str:
        sleep = "WAITFOR DELAY '00:00:05'" if self.dialect != "oracle" else "BEGIN DBMS_SESSION.SLEEP(5); END;"
        permission = f"SELECT security FROM {self.table}" if self.dialect != "oracle" else "ALTER SYSTEM FLUSH SHARED_POOL"
        return {
            "unique-key": f"INSERT INTO {self.table} (security, price) VALUES ('S1', 1)",
            "not-null": f"INSERT INTO {self.table} (security, price) VALUES ('S2', NULL)",
            "timeout": sleep,
            "syntax": f"SELEC security FORM {self.table}",
            "login": f"SELECT security FROM {self.table}",
            "permission": permission,
            "unreachable": f"SELECT security FROM {self.table}",
        }[case]


def _execute(connect: Callable[[], mssql_python.Connection | oracledb.Connection], *statements: str) -> None:
    connection = connect()
    try:
        cursor = connection.cursor()
        for statement in statements:
            cursor.execute(statement)
        connection.commit()
    finally:
        connection.close()


def _mssql(found: Contract) -> Callable[[], mssql_python.Connection]:
    databases = ScratchDatabases()
    if databases.mssql is None:
        pytest.fail("set BASIS_TEST_MSSQL to a connection string for a scratch SQL Server database (see packages/job/tests/integration/README.md)")
    connection_string = databases.mssql.get_secret_value()
    found.settings = {
        "kind": "mssql", "connection_string": connection_string, "isolation": "read_committed", "timeout_s": 10, "max_rows": 1000,
        "write": {"connection_string": connection_string},
    }  # fmt: skip
    return lambda: mssql_python.connect(connection_string, autocommit=False)


def _oracle(found: Contract) -> Callable[[], oracledb.Connection]:
    databases = ScratchDatabases()
    if databases.oracle_dsn is None or databases.oracle_user is None or databases.oracle_password is None:
        pytest.fail("set BASIS_TEST_ORACLE_DSN, _USER and _PASSWORD for a scratch Oracle schema (see packages/job/tests/integration/README.md)")
    dsn, user, password = databases.oracle_dsn, databases.oracle_user, databases.oracle_password.get_secret_value()
    found.settings = {
        "kind": "oracle", "dsn": dsn, "user": user, "password": password, "mode": "thin", "isolation": "read_committed", "timeout_s": 10, "max_rows": 1000,
        "write": {"user": user, "password": password},
    }  # fmt: skip
    return lambda: oracledb.connect(user=user, password=password, dsn=dsn)


@pytest.fixture(params=DIALECTS)
def contract(request: pytest.FixtureRequest) -> Generator[Contract]:
    found = Contract(request.param)
    dialect = found.dialect
    if dialect == "fake":
        yield found
        return
    connect = _mssql(found) if dialect == "mssql" else _oracle(found)
    text, number = ("varchar(12)", "decimal(18,6)") if dialect == "mssql" else ("VARCHAR2(12)", "NUMBER(18,6)")
    _execute(connect, f"CREATE TABLE {found.table} (security {text} PRIMARY KEY, price {number} NOT NULL)", f"INSERT INTO {found.table} (security, price) VALUES ('S1', 1)")
    found.cleanup.append(lambda: _execute(connect, f"DROP TABLE {found.table}"))
    try:
        yield found
    finally:
        for step in reversed(found.cleanup):
            step()


def _denied_login(contract: Contract) -> Mapping[str, object]:
    """The settings of a login that exists and may read nothing of the table: SQL Server's own, which the test makes
    and drops. Oracle's permission case is a statement the scratch user has no privilege for, so it needs none."""
    if contract.dialect != "mssql":
        return {}
    login, password = f"basis_denied_{uuid.uuid4().hex[:8]}", "Basis-denied-1234"
    connection_string = str(contract.settings["connection_string"])

    def connect() -> mssql_python.Connection:
        return mssql_python.connect(connection_string, autocommit=True)

    _execute(connect, f"CREATE LOGIN {login} WITH PASSWORD = '{password}', CHECK_POLICY = OFF", f"CREATE USER {login} FOR LOGIN {login}")
    kill = (
        f"DECLARE @kill nvarchar(max) = N''; SELECT @kill += N'KILL ' + CAST(session_id AS nvarchar(10)) + N';' FROM sys.dm_exec_sessions WHERE login_name = N'{login}';"
        " EXEC (@kill)"
    )  # the driver pools its connections, which stay logged in after they close
    contract.cleanup.append(lambda: _execute(connect, kill, f"DROP USER {login}", f"DROP LOGIN {login}"))
    parts = [part for part in connection_string.split(";") if part.split("=", 1)[0].strip().lower() not in {"uid", "pwd", "user id", "password"}]
    return _logging_in(";".join([*parts, f"UID={login}", f"PWD={password}"]))


def _changed(contract: Contract, case: Case) -> Mapping[str, object]:
    """The settings a case reaches the database with, where it is not the scratch login's own."""
    if case == "timeout":
        return {"timeout_s": 1}
    if case == "permission":
        return _denied_login(contract)
    settings = contract.settings
    changes: dict[tuple[Case, Dialect], Callable[[], Mapping[str, object]]] = {
        ("login", "mssql"): lambda: _logging_in(";".join(part for part in str(settings["connection_string"]).split(";") if not part.strip().lower().startswith("pwd=")) + ";PWD=wrong"),
        ("login", "oracle"): lambda: {"password": "wrong", "write": {"user": settings["user"], "password": "wrong"}},
        ("unreachable", "mssql"): lambda: _logging_in("Server=127.0.0.1,1;Database=x;UID=u;PWD=p;Encrypt=yes;TrustServerCertificate=yes"),  # nothing listens on port 1
        ("unreachable", "oracle"): lambda: {"dsn": "127.0.0.1:1/FREEPDB1"},
    }
    found = changes.get((case, contract.dialect))
    return found() if found is not None else {}


def _logging_in(connection_string: str) -> Mapping[str, object]:
    """SQL Server's settings for one connection string, through which a case both reads and writes."""
    return {"connection_string": connection_string, "write": {"connection_string": connection_string}}


@pytest.mark.parametrize("case", list(KIND))
def test_each_kind_a_database_raises_is_the_same_from_the_fake_and_the_real_one(contract: Contract, case: Case) -> None:
    kind, sql = KIND[case], contract.sql(case)
    if contract.dialect == "fake":  # a login's failure is every statement's, and fails the login itself, as a real database's does
        contract.fake.fails(kind, statement=None if case in {"login", "unreachable"} else sql)
    port = contract.port(**_changed(contract, case))
    with pytest.raises(ServiceError) as raised, port.statements(writes=True) as statements:
        statements.execute(sql, {}, timeout_s=1 if case == "timeout" else 10)
    found = raised.value
    expected = failure(NAME, kind, RuntimeError("the reason, which each says in its own words"))
    assert (type(found), found.resource, found.status, found.detail) == (kind, NAME, None, expected.detail)
    assert found.reason not in {None, ""}


def test_a_deadlock_is_a_conflict_from_the_fake_and_the_real_one(contract: Contract) -> None:
    """Two transactions, each holding a row the other wants, of which the database ends one, its victim."""
    if contract.dialect == "fake":
        contract.fake.fails(Conflict, statement="UPDATE")
        with pytest.raises(Conflict) as raised, contract.port().statements(writes=True) as statements:
            statements.execute("UPDATE", {}, timeout_s=10)
        assert raised.value.detail == failure(NAME, Conflict, RuntimeError()).detail
        return
    table, port = contract.table, contract.port()
    _insert = f"INSERT INTO {table} (security, price) VALUES ('S9', 9)"
    with port.statements(writes=True) as statements:
        statements.execute(_insert, {}, timeout_s=10)
        statements.commit()
    raised: list[ServiceError] = []

    def holding(first: str, second: str, ready: threading.Barrier) -> None:
        try:
            with port.statements(writes=True) as statements:
                statements.execute(f"UPDATE {table} SET price = price WHERE security = '{first}'", {}, timeout_s=10)
                ready.wait()
                statements.execute(f"UPDATE {table} SET price = price WHERE security = '{second}'", {}, timeout_s=10)
        except ServiceError as error:
            raised.append(error)

    ready = threading.Barrier(2)
    one = threading.Thread(target=holding, args=("S1", "S9", ready))
    other = threading.Thread(target=holding, args=("S9", "S1", ready))
    started = time.monotonic()
    one.start()
    other.start()
    one.join(timeout=30)
    other.join(timeout=30)
    assert [(type(error), error.resource, error.detail) for error in raised] == [(Conflict, NAME, failure(NAME, Conflict, RuntimeError()).detail)]
    assert time.monotonic() - started < 30


@pytest.mark.parametrize("given", ["missing", "extra"])
def test_binds_that_do_not_match_the_statement_fail_from_the_fake_and_the_real_one(contract: Contract, given: str) -> None:
    """SQL Server takes a bind its statement does not name, which Oracle refuses: the fake refuses it, as Oracle does,
    so a test catches it on the fake."""
    placeholder = ":security" if contract.dialect == "oracle" else "%(security)s"
    sql = f"SELECT price FROM {contract.table} WHERE security = {placeholder}"
    if contract.dialect == "fake":
        contract.fake.answers[sql] = [{"price": 1.0}]
    binds: dict[str, Bind] = {} if given == "missing" else {"security": "S1", "securty": "S1"}
    if contract.dialect == "mssql" and given == "extra":  # SQL Server's driver ignores it: the fake is the stricter, as Oracle is
        with contract.port().statements(writes=False) as statements:
            statements.query(sql, binds, timeout_s=10)
        return
    with pytest.raises(Failed) as raised, contract.port().statements(writes=False) as statements:
        statements.query(sql, binds, timeout_s=10)
    assert (raised.value.resource, raised.value.detail) == (NAME, failure(NAME, Failed, RuntimeError()).detail)


def test_a_statement_takes_its_binds_whichever_way_it_names_them_on_the_fake() -> None:
    fake = fakes.Database(answers={"SELECT 1 WHERE a = %(a)s": [{"x": 1}], "SELECT 1 FROM dual WHERE a = :a AND b = ':c'": [{"x": 1}]})
    fake.opened_as(NAME)
    with fake.statements(writes=False) as statements:
        assert statements.query("SELECT 1 WHERE a = %(a)s", {"a": 1}, timeout_s=1).num_rows == 1
        assert statements.query("SELECT 1 FROM dual WHERE a = :a AND b = ':c'", {"a": 1}, timeout_s=1).num_rows == 1
