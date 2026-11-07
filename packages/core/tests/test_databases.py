import logging
import socket
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

import mssql_python
import oracledb
import pandas as pd
import pyarrow as pa
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import Field, ValidationError

from basis.core import frames, traces
from basis.core.databases import Counts, mssql, open_database, oracle
from basis.core.databases.base import end_reading
from basis.core.databases.failures import Boundary, ending, failures
from basis.core.databases.mssql import MssqlReader, MssqlWriter, fetches_char_as_utf8
from basis.core.databases.oracle import OracleReader, connect
from basis.core.destination import TableSpec
from basis.core.errors import Conflict, Denied, Failed, Rejected, ServiceError, Timeout, Unavailable
from basis.core.model import Model
from basis.core.settings import GivenSettings, OracleSettings, publish_refusal, variable
from basis.core.tables import Changes, Column

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
        connect(settings, database="warehouse", user=settings.user, password=settings.password.get_secret_value())


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
        (mssql_python.ProgrammingError("Syntax error or access violation", f'{_SERVER}Cannot open database "d" requested by the login. The login failed.'), Denied),
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
        "query-timeout", "refused", "connection-lost", "login-timeout", "login", "select-permission", "create-permission", "database-4060", "syntax",
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


def test_a_block_whose_cleanup_fails_too_raises_its_own_failure(caplog: pytest.LogCaptureFixture) -> None:
    """After a call timeout breaks the connection, its rollback fails too; the timeout is what the caller must see. A
    rollback that fails after a block that did not is the failure, and is raised."""
    timed_out = Timeout("database warehouse's statement did not finish within its timeout", resource="warehouse", reason="DPY-4024: call timeout of 1000 ms exceeded")
    boundary = Boundary("warehouse", (oracledb.Error,), oracle.kind_of)

    def roll_back() -> None:
        closed = _oracle("DPY-1001: not connected to database")  # on the connection the timeout broke
        boundary.call(_raises, closed)

    with caplog.at_level(logging.WARNING), pytest.raises(Timeout) as raised, ending("warehouse", roll_back, "roll back"):
        raise timed_out
    assert raised.value is timed_out
    assert [record.getMessage() for record in caplog.records] == ["database warehouse could not roll back; the failure that ended its work, if any, is the one raised"]
    with pytest.raises(Unavailable, match="database warehouse could not be reached, or lost the connection: DPY-1001"), ending("warehouse", roll_back, "roll back"):
        pass


def _raises(error: BaseException) -> None:
    raise error


class _Failing:
    """A driver's cursor whose statements fail as the test lists them, one error for each, in order."""

    def __init__(self, errors: list[BaseException | None]) -> None:
        self.errors = errors
        self.rowcount = 1

    def execute(self, sql: str, *binds: object) -> None:
        del sql, binds
        if (error := self.errors.pop(0)) is not None:
            raise error

    def fetchone(self) -> tuple[int]:
        return (0,)

    def close(self) -> None:
        pass


class _FailingConnection:
    def __init__(self, cursor: _Failing) -> None:
        self._cursor = cursor

    def cursor(self) -> _Failing:
        return self._cursor


def _writer(*errors: BaseException | None) -> MssqlWriter:
    """basis-core's SQL Server writer, through the boundary, on a driver whose statements fail as `errors` says."""
    raw = _FailingConnection(_Failing(list(errors)))
    return MssqlWriter("warehouse", mssql.Connection(raw, Boundary("warehouse", (mssql_python.Error,), mssql.kind_of)), 10, "etl.publishes")  # ty: ignore[invalid-argument-type] -- a driver without a database


_DUPLICATE = mssql_python.IntegrityError("Integrity constraint violation", f"{_SERVER}Violation of PRIMARY KEY constraint 'pk_publishes'. Cannot insert duplicate key in object 'etl.publishes'.")
_DENIED = mssql_python.ProgrammingError("Syntax error or access violation", f"{_SERVER}The SELECT permission was denied on the object 'publishes', database 'd', schema 'etl'.")


def test_a_writers_statement_raises_its_kind_as_it_fails_so_its_caller_may_catch_it() -> None:
    writer = _writer(_DUPLICATE, None)
    with pytest.raises(Conflict) as raised:
        writer.published("r1")
    assert (raised.value.resource, writer.published("r1")) == ("warehouse", False)  # caught where it was raised, and the transaction goes on


def test_a_statement_that_fails_while_its_caller_handles_another_raises_its_own_kind() -> None:
    writer = _writer(_DUPLICATE, _DENIED)

    def record_or_update() -> None:
        try:
            writer.published("r1")
        except Conflict:
            writer.published("r1")  # as an UPDATE would be, in place of the INSERT a duplicate key refused

    with pytest.raises(Denied) as raised:
        record_or_update()
    cause = raised.value.__cause__  # the driver's error, raised while the Conflict was handled
    assert cause is not None
    assert type(cause.__context__) is Conflict


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

    def fetch_df_batches(self, sql: str, binds: object, *, size: int) -> list[pa.Table]:
        del binds, size
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


@dataclass(frozen=True)
class _Target:
    """A target as a part declares one, with only what SQL Server's writer reads."""

    columns: tuple[Column, ...]
    table: str = "docs"
    database: str = "warehouse"
    key: tuple[str, ...] | None = ("id",)

    def column(self, name: str) -> Column:
        return next(column for column in self.columns if column.name == name)


DOCS = _Target((Column(name="id", kind="int", nullable=False), Column(name="body", kind="str", nullable=True), Column(name="amount", kind="decimal", nullable=True, precision=18, scale=4)))
DOCS_SPEC = TableSpec("mssql", "docs", {})  # unread by SQL Server's writer, which binds by the target's kinds


class _Server:
    """SQL Server as its writer reaches it, through cursors that record each call: an `executemany` counts each row as
    `reported` says, as the driver's count, the stage holds what its batches gave it but `lost` rows, and every other
    statement counts one row."""

    def __init__(self, *, reported: Callable[[int], int] = lambda rows: rows, lost: int = 0) -> None:
        self.calls: list[tuple[str, object]] = []
        self.reported, self.lost = reported, lost
        self.staged = 0
        self.rowcount = 0

    def cursor(self) -> _Server:
        return self

    def setinputsizes(self, sizes: object) -> None:
        self.calls.append(("setinputsizes", sizes))

    def executemany(self, sql: str, rows: list[tuple[object, ...]]) -> None:
        self.calls.append(("executemany", rows))
        self.staged += len(rows) if "#etl_stage" in sql else 0
        self.rowcount = self.reported(len(rows))

    def execute(self, sql: str, binds: tuple[object, ...] = ()) -> None:
        self.calls.append(("execute", binds or sql.split(None, 1)[0]))
        self.staged += 1 if binds and "#etl_stage" in sql else 0
        self.rowcount = 0 if sql.startswith(("UPDATE", "DROP")) else self.staged if sql.startswith("INSERT INTO docs") and "SELECT" in sql else 1

    def fetchone(self) -> tuple[int]:
        return (self.staged - self.lost,)

    def close(self) -> None:
        pass


def _docs(*rows: tuple[int, str | None]) -> pd.DataFrame:
    values = {"id": [row[0] for row in rows], "body": [row[1] for row in rows], "amount": [Decimal("1.5")] * len(rows)}
    return pd.DataFrame({column.name: pd.Series(values[column.name], dtype=frames.dtype(column)) for column in DOCS.columns})


def test_every_column_binds_by_its_kind_and_a_row_past_4000_utf16_units_goes_by_itself_in_its_place() -> None:
    """mssql-python 1.17.0 typed each column by its longest value, binding one whose longest is ASCII as `varchar`,
    which the database's code page converts, and counted only the last row of a batch holding a longer string. A long
    row binds its long string as `nvarchar(max)`, its others as a batch does, and keeps its place among the rows."""
    server = _Server()
    writer = MssqlWriter("warehouse", server, 10, "etl.publishes")  # ty: ignore[invalid-argument-type] -- a fake connection
    frame = _docs((1, "hello world"), (2, "日本\U0001f600"), (3, "a" * 3999 + "\U0001f600"), (4, "\U0001f600" * 2000), (5, None))
    assert writer.apply(DOCS, Changes(insert=frame), DOCS_SPEC) == Counts(inserted=5)
    bigint, text = (mssql_python.SQL_BIGINT, 19, 0), (mssql_python.SQL_VARCHAR, 41, 0)
    assert server.calls == [
        ("setinputsizes", [bigint, (mssql_python.SQL_WVARCHAR, 11, 0), text]),
        ("executemany", [(1, "hello world", "1.5000"), (2, "日本\U0001f600", "1.5000")]),
        ("setinputsizes", [bigint, (mssql_python.SQL_WVARCHAR, 0, 0), text]),
        ("execute", (3, "a" * 3999 + "\U0001f600", "1.5000")),
        ("setinputsizes", [bigint, (mssql_python.SQL_WVARCHAR, 4000, 0), text]),
        ("executemany", [(4, "\U0001f600" * 2000, "1.5000"), (5, None, "1.5000")]),
    ]


def test_each_run_of_rows_is_a_span_and_a_run_one_by_one_counts_its_statements() -> None:
    """A trace shows what each row past 4,000 UTF-16 units costs: its run's span, over the statements it ran."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    writer = MssqlWriter("warehouse", _Server(), 10, "etl.publishes")  # ty: ignore[invalid-argument-type] -- a fake connection
    with traces.tracing(provider.get_tracer("test")):
        writer.apply(DOCS, Changes(insert=_docs((1, "x"), (2, "a" * 4001), (3, "a" * 5000), (4, "y"))), DOCS_SPEC)
    runs = [{key: value for key, value in (span.attributes or {}).items() if key in {"db.operation.batch.size", "basis.statements"}} for span in exporter.get_finished_spans()]
    assert runs == [{"db.operation.batch.size": 1}, {"basis.statements": 2}, {"db.operation.batch.size": 1}]


@pytest.mark.parametrize(("lost", "error"), [(0, None), (1, "staging for docs wrote 2 rows, and the run computed 3: nothing is committed")], ids=["counted", "short"])
def test_a_stage_is_counted_by_the_server_whatever_the_driver_reports(lost: int, error: str | None) -> None:
    server = _Server(reported=lambda _: 1, lost=lost)  # as the driver counted a batch holding a long string
    writer = MssqlWriter("warehouse", server, 10, "etl.publishes")  # ty: ignore[invalid-argument-type] -- a fake connection
    frame = _docs((1, "x"), (2, "a" * 4001), (3, "y"))
    if error is None:
        assert writer.apply(DOCS, Changes(upsert=frame), DOCS_SPEC) == Counts(upserted=3)
        return
    with pytest.raises(RuntimeError) as raised:
        writer.apply(DOCS, Changes(upsert=frame), DOCS_SPEC)
    assert str(raised.value) == error


ORACLE_SETS = {"NLS_CHARACTERSET": "AL32UTF8", "NLS_NCHAR_CHARACTERSET": "AL16UTF16"}


class _Describing:
    """An Oracle connection whose `all_tab_cols` has one column, `note`, and whose character sets are `sets`."""

    def __init__(self, column: tuple[object, ...], sets: dict[str, str]) -> None:
        self.column, self.sets, self.sql = column, sets, ""

    def cursor(self) -> _Describing:
        return self

    def execute(self, sql: str, binds: object = None) -> None:
        del binds
        self.sql = sql

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self.sets.items()) if "nls_database_parameters" in self.sql else [("NOTE", *self.column)]

    def close(self) -> None:
        pass


@pytest.mark.parametrize(
    ("column", "sets", "storage"),
    [
        (("VARCHAR2", 1000, None, None, "Y", None, "NO", "NO", 4000, "C"), ORACLE_SETS, (1000, "characters", 4000, "utf-8", "AL32UTF8")),
        (("VARCHAR2", 10, None, None, "Y", None, "NO", "NO", 10, "B"), ORACLE_SETS | {"NLS_CHARACTERSET": "WE8MSWIN1252"}, (10, "bytes", None, "cp1252", "WE8MSWIN1252")),
        (("CHAR", 5, None, None, "Y", None, "NO", "NO", 5, "C"), ORACLE_SETS | {"NLS_CHARACTERSET": "WE8ISO8859P1"}, (5, "characters", 5, "latin-1", "WE8ISO8859P1")),
        (("VARCHAR2", 10, None, None, "Y", None, "NO", "NO", 30, "C"), ORACLE_SETS | {"NLS_CHARACTERSET": "UTF8"}, (10, "characters", 30, "utf-8", "UTF8")),
        (("NVARCHAR2", 10, None, None, "Y", None, "NO", "NO", 20, "C"), ORACLE_SETS | {"NLS_CHARACTERSET": "WE8MSWIN1252"}, (10, "characters", 20, "utf-16-be", "AL16UTF16")),
        (("CLOB", 0, None, None, "Y", None, "NO", "NO", 4000, None), ORACLE_SETS | {"NLS_CHARACTERSET": "WE8MSWIN1252"}, (None, "characters", None, "cp1252", "WE8MSWIN1252")),
        (("VARCHAR2", 10, None, None, "Y", None, "NO", "NO", 10, "C"), ORACLE_SETS | {"NLS_CHARACTERSET": "EE8ISO8859P2"}, (10, "characters", 10, None, "EE8ISO8859P2")),
        (("NUMBER", None, 18, 4, "Y", None, "NO", "NO", 22, None), ORACLE_SETS, (None, "characters", None, None, None)),
    ],
    ids=["varchar2-char-al32utf8", "varchar2-byte-we8mswin1252", "char-we8iso8859p1", "varchar2-utf8", "nvarchar2-national", "clob", "a-set-basis-does-not-know", "number"],
)
def test_an_oracle_column_stores_text_in_its_character_set(column: tuple[object, ...], sets: dict[str, str], storage: tuple[object, ...]) -> None:
    """Oracle replaces a character its set cannot hold without an error, as SQL Server's code pages do: a `CHAR`,
    `VARCHAR2` or `CLOB` stores text in the database's set, an `NCHAR`, `NVARCHAR2` or `NCLOB` in the national one, and
    a column in characters holds its bytes too. No live test can show it: CI's Oracle is AL32UTF8, which holds every
    character."""
    spec = OracleReader("ledger", _Describing(column, sets), 10).describe("prices")  # ty: ignore[invalid-argument-type] -- a fake connection
    assert spec is not None
    found = spec.columns["note"]
    assert (found.length, found.unit, found.max_bytes, found.encoding, found.charset) == storage


def test_a_decimals_text_is_plain_since_sql_server_cannot_convert_one_with_an_exponent() -> None:
    """Arrow writes a decimal below 10^-6 as `1E-10`, which SQL Server refuses to convert to a decimal."""
    text = pa.chunked_array([pa.array(["1E-10", "1.000E-7", "0E-10", "-12.5", None])])
    assert mssql._plain(text).to_pylist() == ["0.0000000001", "0.0000001000", "0.0000000000", "-12.5", None]  # noqa: SLF001 -- the writer's own step
