import logging
import socket
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal

import mssql_python
import oracledb
import pyarrow as pa
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import Field, ValidationError

from basis.core import traces
from basis.core.databases import mssql, open_database, oracle
from basis.core.databases.base import end_reading
from basis.core.databases.failures import failures
from basis.core.databases.mssql import MssqlReader, MssqlWriter, fetches_char_as_utf8
from basis.core.databases.oracle import OracleReader, connect
from basis.core.errors import Conflict, Denied, Failed, Rejected, ServiceError, Timeout, Unavailable
from basis.core.model import Model
from basis.core.settings import GivenSettings, OracleSettings, publish_refusal, variable

MSSQL = {"kind": "mssql", "connection_string": "Server=nowhere", "isolation": "snapshot", "timeout_s": 5, "max_rows": 10}
ORACLE = {"kind": "oracle", "dsn": "nowhere:1521/X", "user": "reader", "password": "x", "mode": "thin", "isolation": "read_only", "timeout_s": 5, "max_rows": 10}


@pytest.mark.parametrize(
    ("database", "write", "writable"),
    [
        (MSSQL, None, False),
        (MSSQL, {"connection_string": "Server=nowhere"}, False),
        (MSSQL, {"connection_string": "Server=nowhere", "ledger": "etl.publishes"}, True),
        (ORACLE, {"user": "writer", "password": "x"}, False),
        (ORACLE, {"user": "writer", "password": "x", "ledger": "ETL.PUBLISHES"}, True),
    ],
    ids=["mssql-reads-only", "mssql-without-a-ledger", "mssql-publishes", "oracle-without-a-ledger", "oracle-publishes"],
)
def test_a_run_publishes_only_through_a_write_login_with_a_ledger(database: dict[str, object], write: dict[str, object] | None, writable: bool) -> None:  # noqa: FBT001 -- a parameter of the table
    found = open_database(GivenSettings(databases={"warehouse": database | {"write": write}}), "warehouse")
    assert found.writable is writable


@pytest.mark.parametrize(
    ("database", "write", "refusal"),
    [
        (MSSQL, None, ("has no write settings", "set BASIS_DATABASES__WAREHOUSE__WRITE__CONNECTION_STRING and BASIS_DATABASES__WAREHOUSE__WRITE__LEDGER")),
        (ORACLE, None, ("has no write settings", "set BASIS_DATABASES__WAREHOUSE__WRITE__USER, BASIS_DATABASES__WAREHOUSE__WRITE__PASSWORD and BASIS_DATABASES__WAREHOUSE__WRITE__LEDGER")),
        (MSSQL, {"connection_string": "Server=nowhere"}, ("has a write login but no ledger, which a publish writes in its transaction", "set BASIS_DATABASES__WAREHOUSE__WRITE__LEDGER")),
        (MSSQL, {"connection_string": "Server=nowhere", "ledger": "etl.publishes"}, None),
    ],
    ids=["mssql-reads-only", "oracle-reads-only", "without-a-ledger", "publishes"],
)
def test_a_publish_refusal_names_each_variable_to_set(database: dict[str, object], write: dict[str, object] | None, refusal: tuple[str, str] | None) -> None:
    found = GivenSettings(databases={"warehouse": database | {"write": write}}).database("warehouse")
    assert publish_refusal("warehouse", found) == refusal


@pytest.mark.parametrize("database", [MSSQL | {"write": {"connection_string": "Server=nowhere"}}, ORACLE | {"write": {"user": "writer", "password": "x"}}], ids=["mssql", "oracle"])
def test_a_publish_without_a_ledger_names_the_setting(database: dict[str, object]) -> None:
    found = open_database(GivenSettings(databases={"warehouse": database}), "warehouse")
    with pytest.raises(LookupError, match=r"has a write login but no ledger, which a publish writes in its transaction: set BASIS_DATABASES__WAREHOUSE__WRITE__LEDGER$"), found.writing():
        pass


def test_a_rollback_that_fails_leaves_the_error_that_ended_the_transaction(caplog: pytest.LogCaptureFixture) -> None:
    """After a call timeout breaks the connection, its rollback fails too; the timeout is what the caller must see."""

    def broken() -> None:
        msg = "DPY-4011: the database or network closed the connection"
        raise ConnectionError(msg)

    def read() -> None:
        try:
            msg = "DPY-4024: call timeout of 240000 ms exceeded"
            raise TimeoutError(msg)
        finally:
            end_reading("warehouse", broken)

    with caplog.at_level(logging.WARNING), pytest.raises(TimeoutError, match="call timeout"):
        read()
    assert [record.getMessage() for record in caplog.records] == ["could not roll back a transaction that only read; closing its connection"]


def test_oracle_in_thick_mode_loads_its_client_libraries_from_the_folder_given(tmp_path: Path) -> None:
    settings = OracleSettings.model_validate(ORACLE | {"mode": "thick", "client_lib_dir": tmp_path})

    with pytest.raises(oracledb.DatabaseError, match=f"DPI-1047: Cannot locate .*{tmp_path}"):  # an empty folder: no client there
        connect(settings, user=settings.user, password=settings.password.get_secret_value())


@pytest.mark.parametrize(
    ("platform", "encoding", "utf8"),
    [
        ("linux", "UTF-8", True),
        ("linux", "ANSI_X3.4-1968", True),
        ("linux", "US-ASCII", False),
        ("linux", "ISO-8859-1", False),
        ("darwin", "UTF-8", False),
        ("win32", "cp1252", False),
        ("win32", "utf-8", False),
    ],
    ids=["linux-utf8", "linux-c-locale", "linux-other-ascii-name", "linux-latin1", "macos", "windows", "windows-utf8"],
)
def test_sql_server_char_data_is_fetched_as_utf8_only_where_that_is_verified_exact(platform: str, encoding: str, utf8: bool) -> None:  # noqa: FBT001 -- a parameter of the table
    """On Linux, under a UTF-8 locale or glibc's C locale; elsewhere, and under any other charset, each connection keeps
    the driver's default (docs/spikes/sqlserver-json.md)."""
    assert fetches_char_as_utf8(platform, encoding) is utf8


@pytest.fixture
def hung_database() -> Generator[str]:
    """An address that accepts connections and never answers, as a hung database does."""
    with socket.create_server(("127.0.0.1", 0)) as server:
        yield f"127.0.0.1,{server.getsockname()[1]}"


def test_a_login_the_database_never_answers_ends_at_its_timeout(hung_database: str) -> None:
    """The login waits `timeout_s`, here 1 second, rather than the 30 seconds that are the most a login ever waits.
    Oracle's login is bounded the same way, in a thread the read stops waiting for, which a test cannot tidy away."""
    database = MSSQL | {"connection_string": f"Server=tcp:{hung_database};Database=X;UID=u;PWD=p", "timeout_s": 1}
    found = open_database(GivenSettings(databases={"books": database}), "books")
    start = time.monotonic()

    with pytest.raises(Unavailable, match=r"^database books could not be reached, or lost the connection: Driver Error: ") as raised, found.statements(writes=False):
        pass

    assert time.monotonic() - start < 10
    assert (raised.value.resource, raised.value.status, raised.value.__notes__) == ("books", None, ["while connecting to SQL Server tcp:" + hung_database + ", database X"])


_SERVER = "[Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (mssql_python.OperationalError("Timeout expired", "[Microsoft][ODBC Driver 18 for SQL Server]Query timeout expired"), Timeout),
        (mssql_python.OperationalError("Client unable to establish connection", "[Microsoft][ODBC Driver 18 for SQL Server]TCP Provider: Error code 0x2749"), Unavailable),
        (mssql_python.OperationalError("Communication link failure", "[Microsoft][ODBC Driver 18 for SQL Server]TCP Provider: Error code 0x2746"), Unavailable),
        (mssql_python.OperationalError("Connection timeout expired", "[Microsoft][ODBC Driver 18 for SQL Server]Login timeout expired"), Unavailable),
        (mssql_python.OperationalError("Invalid authorization specification", f"{_SERVER}Login failed for user 'reader'."), Denied),
        (mssql_python.ProgrammingError("Syntax error or access violation", f"{_SERVER}The SELECT permission was denied on the object 't', database 'd', schema 'dbo'."), Denied),
        (mssql_python.ProgrammingError("Syntax error or access violation", f"{_SERVER}CREATE TABLE permission denied in database 'd'."), Denied),
        (mssql_python.ProgrammingError("Syntax error or access violation", f"{_SERVER}Incorrect syntax near 'FORM'."), Failed),
        (mssql_python.IntegrityError("Integrity constraint violation", f"{_SERVER}Violation of PRIMARY KEY constraint 'pk_t'. Cannot insert duplicate key in object 'dbo.t'."), Conflict),
        (mssql_python.IntegrityError("Integrity constraint violation", f"{_SERVER}Cannot insert duplicate key row in object 'dbo.t' with unique index 'ix'."), Conflict),
        (mssql_python.IntegrityError("Integrity constraint violation", f'{_SERVER}The INSERT statement conflicted with the FOREIGN KEY constraint "fk_t".'), Rejected),
        (mssql_python.IntegrityError("Integrity constraint violation", f"{_SERVER}Cannot insert the value NULL into column 'n', table 'd.dbo.t'; column does not allow nulls."), Rejected),
        (mssql_python.OperationalError("Serialization failure", f"{_SERVER}Transaction (Process ID 93) was deadlocked on lock resources with another process."), Conflict),
        (mssql_python.ProgrammingError("Base table or view not found", f"{_SERVER}Invalid object name 'nowhere'."), Failed),
        (ValueError("not the driver's"), Failed),
    ],
    ids=[
        "query-timeout", "refused", "connection-lost", "login-timeout", "login", "select-permission", "create-permission", "syntax",
        "primary-key", "unique-index", "foreign-key", "not-null", "deadlock", "no-table", "not-the-drivers",
    ],
)  # fmt: skip
def test_a_sql_server_error_is_the_kind_its_sqlstate_and_its_message_say(error: BaseException, kind: type[ServiceError]) -> None:
    assert mssql.kind_of(error) is kind


def _oracle(message: str, code: int = 0) -> oracledb.Error:
    return oracledb.DatabaseError(oracledb._Error(message, code=code))  # noqa: SLF001 -- the driver's own error object, as it raises it


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (_oracle("DPY-4024: call timeout of 1000 ms exceeded"), Timeout),
        (_oracle("DPY-4011: the database or network closed the connection\nsocket timed out while recovering from previous socket timeout"), Timeout),
        (_oracle("ORA-03156: OCI call timed out", 3156), Timeout),
        (_oracle("DPY-4011: the database or network closed the connection"), Unavailable),
        (_oracle("ORA-03113: end-of-file on communication channel", 3113), Unavailable),
        (_oracle("DPY-6005: cannot connect to database (CONNECTION_ID=x). [Errno 111] Connection refused"), Unavailable),
        (_oracle("DPY-1001: not connected to database"), Unavailable),
        (_oracle("ORA-12541: Cannot connect. No listener at host 127.0.0.1 port 1521.", 12541), Unavailable),
        (_oracle("ORA-01017: invalid credential or not authorized; logon denied", 1017), Denied),
        (_oracle("ORA-01031: insufficient privileges", 1031), Denied),
        (_oracle("ORA-00001: unique constraint (ETL.PK_T) violated", 1), Conflict),
        (_oracle("ORA-00060: deadlock detected while waiting for resource", 60), Conflict),
        (_oracle("ORA-02291: integrity constraint (ETL.FK_T) violated - parent key not found", 2291), Rejected),
        (_oracle('ORA-01400: cannot insert NULL into ("ETL"."T"."N")', 1400), Rejected),
        (_oracle("ORA-00900: invalid SQL statement", 900), Failed),
        (oracle.HungLoginError("Oracle at x accepted a connection but did not log in within 5s"), Unavailable),
        (ValueError("not the driver's"), Failed),
    ],
    ids=[
        "call-timeout", "call-timeout-unanswered", "thick-call-timeout", "closed", "end-of-file", "refused", "not-connected", "no-listener",
        "login", "privilege", "unique", "deadlock", "foreign-key", "not-null", "syntax", "hung-login", "not-the-drivers",
    ],
)  # fmt: skip
def test_an_oracle_error_is_the_kind_its_code_says(error: BaseException, kind: type[ServiceError]) -> None:
    assert oracle.kind_of(error) is kind


def test_a_drivers_error_is_raised_as_its_kind_with_the_drivers_message_as_its_reason_and_its_notes() -> None:
    error = mssql_python.IntegrityError("Integrity constraint violation", f"{_SERVER}Violation of PRIMARY KEY constraint 'pk_t'.\nThe duplicate key value is (1).")
    error.add_note("while writing etl.prices")
    with pytest.raises(Conflict) as raised, failures("warehouse", (mssql_python.Error,), mssql.kind_of):
        raise error
    found = raised.value
    assert (found.resource, found.status, found.detail, found.__cause__, found.__notes__) == (
        "warehouse", None, "database warehouse refused the statement for a unique key, or chose it as a deadlock's victim", error, ["while writing etl.prices"],
    )  # fmt: skip
    assert found.reason == "Driver Error: Integrity constraint violation; DDBC Error: [Microsoft][SQL Server]Violation of PRIMARY KEY constraint 'pk_t'. The duplicate key value is (1)."
    assert str(found) == f"{found.detail}: {found.reason}"
    with pytest.raises(ValueError, match="not the driver's"), failures("warehouse", (mssql_python.Error,), mssql.kind_of):
        raise ValueError("not the driver's")  # noqa: EM101, TRY003 -- an error of the test's own


def test_a_drivers_error_while_ending_a_failed_block_leaves_its_first_failure(caplog: pytest.LogCaptureFixture) -> None:
    """After a call timeout breaks the connection, its rollback fails too; the timeout is what the caller must see."""
    timed_out = Timeout("database warehouse's statement did not finish within its timeout", resource="warehouse", reason="DPY-4024: call timeout of 1000 ms exceeded")

    @contextmanager
    def rolled_back() -> Generator[None]:
        try:
            yield
        finally:
            closed = _oracle("DPY-1001: not connected to database")  # the rollback's, on the connection the timeout broke
            raise closed

    with caplog.at_level(logging.WARNING), pytest.raises(Timeout) as raised, failures("warehouse", (oracledb.Error,), oracle.kind_of), rolled_back():
        raise timed_out
    assert raised.value is timed_out
    assert [record.getMessage() for record in caplog.records] == ["database warehouse failed again while ending what its failure stopped; the first failure is the one raised"]


class Email(Model):
    kind: Literal["email"]
    to: str


class Page(Model):
    kind: Literal["page"]
    rota: str


class Alerting(GivenSettings):
    alerts: tuple[Annotated[Email | Page, Field(discriminator="kind")], ...] = ()


@pytest.mark.parametrize(
    ("settings", "values", "variables"),
    [
        (GivenSettings, {"databases": {"oracle": {key: value for key, value in ORACLE.items() if key != "dsn"}}}, ["BASIS_DATABASES__ORACLE__DSN"]),
        (GivenSettings, {"databases": {"warehouse": MSSQL | {"write": {}}}}, ["BASIS_DATABASES__WAREHOUSE__WRITE__CONNECTION_STRING"]),
        (GivenSettings, {"databases": {"warehouse": MSSQL | {"timeout_s": 0}}}, ["BASIS_DATABASES__WAREHOUSE__TIMEOUT_S"]),
        (Alerting, {"alerts": [{"kind": "email", "to": "desk@example.com"}, {"kind": "page"}]}, ["BASIS_ALERTS__1__ROTA"]),
    ],
    ids=["a-database-named-as-its-kind", "a-write-block", "a-limit", "a-union-in-a-list"],
)
def test_a_variable_is_named_without_the_tag_of_any_discriminated_union(settings: type[GivenSettings], values: dict[str, object], variables: list[str]) -> None:
    with pytest.raises(ValidationError) as raised:
        settings.model_validate(values)
    assert [variable(error["loc"], settings) for error in raised.value.errors()] == variables


class _Cursor:
    """A driver's cursor, as SQL Server's reader and writer use it: each statement it executes, and its rows as Arrow."""

    def __init__(self, executed: list[str]) -> None:
        self.executed = executed
        self.rowcount = 1

    def execute(self, sql: str, binds: object = None) -> None:
        del binds
        self.executed.append(sql)

    def executemany(self, sql: str, rows: object) -> None:
        del rows
        self.executed.append(sql)

    def arrow_reader(self, *, batch_size: int) -> pa.RecordBatchReader:
        del batch_size
        batch = pa.record_batch({"price": [1, 2]})
        return pa.RecordBatchReader.from_batches(batch.schema, [batch])

    def fetchone(self) -> tuple[int]:
        return (0,)

    def close(self) -> None:
        pass


class _Connection:
    def __init__(self) -> None:
        self.executed: list[str] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self.executed)

    def commit(self) -> None:
        self.executed.append("commit")

    def fetch_df_batches(self, sql: str, binds: object, *, size: int, fetch_decimals: bool) -> list[pa.Table]:
        del binds, size, fetch_decimals
        self.executed.append(sql)
        return [pa.table({"PRICE": [1, 2, 3]})]


def test_each_statement_a_reader_or_a_writer_runs_has_a_span_on_the_runs_tracer() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    mssql, oracle = _Connection(), _Connection()
    with traces.tracing(provider.get_tracer("test")):
        MssqlReader("source", mssql, 10).fetch("SELECT price FROM prices", None, what="prices")  # ty: ignore[invalid-argument-type] -- a fake connection
        writer = MssqlWriter("warehouse", mssql, 10, "etl.publishes")  # ty: ignore[invalid-argument-type] -- a fake connection
        assert writer.published("r1") is False
        writer.commit()
        OracleReader("ledger", oracle, 10).fetch("SELECT price FROM prices", None, what="prices")  # ty: ignore[invalid-argument-type] -- a fake connection
    MssqlReader("outside", mssql, 10).fetch("SELECT 1", None, what="outside a run")  # ty: ignore[invalid-argument-type] -- a fake connection
    spans = [(span.name, dict(span.attributes or {})) for span in exporter.get_finished_spans()]
    assert spans == [
        ("source SELECT", {"db.system.name": "microsoft.sql_server", "basis.resource": "source", "db.query.text": "SELECT price FROM prices", "db.response.returned_rows": 2}),
        ("warehouse SELECT", {"db.system.name": "microsoft.sql_server", "basis.resource": "warehouse", "db.query.text": "SELECT COUNT(*) FROM etl.publishes WHERE run_id = %(run_id)s"}),
        ("warehouse commit", {"db.system.name": "microsoft.sql_server", "basis.resource": "warehouse"}),
        ("ledger SELECT", {"db.system.name": "oracle.db", "basis.resource": "ledger", "db.query.text": "SELECT price FROM prices", "db.response.returned_rows": 3}),
    ]
