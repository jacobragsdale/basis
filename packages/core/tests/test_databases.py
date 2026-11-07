import logging
import socket
import time
from collections.abc import Generator
from pathlib import Path
from typing import Annotated, Literal

import mssql_python
import oracledb
import pytest
from pydantic import Field, ValidationError

from basis.core.databases import open_database
from basis.core.databases.base import end_reading
from basis.core.databases.oracle import connect
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

    with pytest.raises(mssql_python.OperationalError), found.statements(writes=False):
        pass

    assert time.monotonic() - start < 10


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
