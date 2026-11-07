"""basis-api's integration tier: a request's transaction in a real database, SQL Server and Oracle, through the same
`BASIS_TEST_*` variables and scratch databases as basis-job's (packages/job/tests/integration/README.md). A write
commits once the response validates, and rolls back when the handler raises or the response does not validate; a
request's reads see its own writes; a statement past its deadline is stopped on the server, and its request's writes
are rolled back; and the example's SQL runs on SQL Server. Every test makes its own tables and drops them after."""

import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated, Literal
from uuid import UUID

import mssql_python
import oracledb
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from basis import api
from basis.api import fakes
from basis.api.example import service as example

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
    """A scratch database: its settings for an API, through one login that reads and writes, and raw SQL around it."""

    dialect: Dialect
    settings: dict[str, object]
    connect: Callable[[], mssql_python.Connection | oracledb.Connection]
    table: str
    """A table of this test's own: `security` and `price`, which the test makes and drops."""

    def bind(self, name: str) -> str:
        return f"%({name})s" if self.dialect == "mssql" else f":{name}"

    def execute(self, *statements: str) -> None:
        connection = self.connect()
        try:
            cursor = connection.cursor()
            for statement in statements:
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

    def drop(self, table: str) -> None:
        if self.dialect == "mssql":
            self.execute(f"IF OBJECT_ID('{table}') IS NOT NULL DROP TABLE {table}")
        else:
            self.execute(f"BEGIN EXECUTE IMMEDIATE 'DROP TABLE {table} PURGE'; EXCEPTION WHEN OTHERS THEN NULL; END;")


def _mssql(suffix: str) -> Scratch:
    databases = ScratchDatabases()
    if databases.mssql is None:
        pytest.fail("set BASIS_TEST_MSSQL to a connection string for a scratch SQL Server database (see packages/job/tests/integration/README.md)")
    connection_string = databases.mssql.get_secret_value()
    settings: dict[str, object] = {
        "kind": "mssql", "connection_string": connection_string, "isolation": "snapshot", "timeout_s": 10, "max_rows": 1000,
        "write": {"connection_string": connection_string},  # no ledger: an API's requests write none
    }  # fmt: skip
    return Scratch("mssql", settings, lambda: mssql_python.connect(connection_string, autocommit=False), f"basis_api_prices_{suffix}")


def _oracle(suffix: str) -> Scratch:
    databases = ScratchDatabases()
    if databases.oracle_dsn is None or databases.oracle_user is None or databases.oracle_password is None:
        pytest.fail("set BASIS_TEST_ORACLE_DSN, _USER and _PASSWORD for a scratch Oracle schema (see packages/job/tests/integration/README.md)")
    dsn, user, password = databases.oracle_dsn, databases.oracle_user, databases.oracle_password.get_secret_value()
    # read_committed: a read-only transaction refuses a table made seconds before (ORA-01466), as each test's is
    settings: dict[str, object] = {
        "kind": "oracle", "dsn": dsn, "user": user, "password": password, "mode": "thin", "isolation": "read_committed", "timeout_s": 10,
        "max_rows": 1000, "write": {"user": user, "password": password},
    }  # fmt: skip
    return Scratch("oracle", settings, lambda: oracledb.connect(user=user, password=password, dsn=dsn), f"basis_api_prices_{suffix}")


def scratch_of(dialect: Dialect) -> Iterator[Scratch]:
    found = (_mssql if dialect == "mssql" else _oracle)(uuid.uuid4().hex[:8])
    numeric = "decimal(18,6)" if dialect == "mssql" else "NUMBER(18,6)"
    text = "varchar(12)" if dialect == "mssql" else "VARCHAR2(12)"
    found.execute(f"CREATE TABLE {found.table} (security {text} PRIMARY KEY, price {numeric} NOT NULL)")
    yield found
    found.drop(found.table)


@pytest.fixture(params=["mssql", "oracle"])
def scratch(request: pytest.FixtureRequest) -> Iterator[Scratch]:
    yield from scratch_of(request.param)


@pytest.fixture
def mssql() -> Iterator[Scratch]:
    yield from scratch_of("mssql")


WAREHOUSE = api.database("warehouse", deadline_s=10)
QUICK = api.database("quick", deadline_s=1)


class Price(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    security: str
    price: Decimal


class Refused(api.Error):
    status = HTTPStatus.CONFLICT
    title = "The price is refused"


def _service(scratch: Scratch) -> api.Service:
    insert = f"INSERT INTO {scratch.table} (security, price) VALUES ({scratch.bind('security')}, {scratch.bind('price')})"
    select = f"SELECT security, price FROM {scratch.table} WHERE security = {scratch.bind('security')}"

    async def read(security: str, warehouse: Annotated[api.Reader, api.given(WAREHOUSE)]) -> Price:
        if (found := await warehouse.fetch_one(Price, select, security=security)) is None:
            raise HTTPException(HTTPStatus.NOT_FOUND)
        return found

    async def write(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)]) -> Price:
        await warehouse.execute(insert, security=price.security, price=price.price)
        return price

    async def write_and_read(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)]) -> Price:
        await warehouse.execute(insert, security=price.security, price=price.price)
        if (found := await warehouse.fetch_one(Price, select, security=price.security)) is None:
            raise HTTPException(HTTPStatus.NOT_FOUND)
        return found

    async def refuse(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)]) -> Price:
        await warehouse.execute(insert, security=price.security, price=price.price)
        msg = f"{price.security} is refused after all"
        raise Refused(msg)

    async def misanswer(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)]) -> Price:
        await warehouse.execute(insert, security=price.security, price=price.price)
        return {"security": price.security, "price": "not a number"}  # ty: ignore[invalid-return-type] -- a response that does not validate, on purpose

    async def write_slowly(price: Price, quick: Annotated[api.Writer, api.given(QUICK)]) -> Price:
        await quick.execute(insert, security=price.security, price=price.price)
        return price

    routes = [
        api.get("/prices/{security}", read), api.post("/prices", write), api.post("/read-your-writes", write_and_read), api.post("/refused", refuse),
        api.post("/misanswered", misanswer), api.post("/slowly", write_slowly),
    ]  # fmt: skip
    return api.Service(name="scratch", settings=api.Settings, resources=[WAREHOUSE, QUICK], routes=routes)


def _client(scratch: Scratch) -> TestClient:
    settings = api.Settings.given(databases={"warehouse": scratch.settings, "quick": scratch.settings})
    return TestClient(api.build(_service(scratch), settings=settings).asgi)


def _stored(scratch: Scratch) -> list[tuple[object, ...]]:
    return scratch.rows(f"SELECT security, price FROM {scratch.table} ORDER BY security")


PRICE = {"security": "US912810TM64", "price": "101.25"}


def test_a_write_commits_once_the_response_validates(scratch: Scratch) -> None:
    with _client(scratch) as client:
        assert client.post("/prices", json=PRICE).status_code == HTTPStatus.OK
        read = client.get("/prices/US912810TM64").json()
    assert _stored(scratch) == [("US912810TM64", Decimal("101.250000"))]
    assert read == {"security": "US912810TM64", "price": "101.250000"}


@pytest.mark.parametrize(("path", "status"), [("/refused", HTTPStatus.CONFLICT), ("/misanswered", HTTPStatus.INTERNAL_SERVER_ERROR)], ids=["handler-raises", "response-does-not-validate"])
def test_a_write_rolls_back_when_the_request_fails(scratch: Scratch, path: str, status: int) -> None:
    with _client(scratch) as client:
        assert client.post(path, json=PRICE).status_code == status
    assert _stored(scratch) == []


def test_a_requests_reads_see_its_own_writes(scratch: Scratch) -> None:
    with _client(scratch) as client:
        assert client.post("/read-your-writes", json=PRICE).json() == {"security": "US912810TM64", "price": "101.250000"}
    assert _stored(scratch) == [("US912810TM64", Decimal("101.250000"))]


def test_a_statement_past_its_deadline_stops_on_the_server_and_its_write_rolls_back(mssql: Scratch) -> None:
    holder = mssql.connect()  # holds the table locked, in a transaction of its own, so the request's insert waits
    try:
        cursor = holder.cursor()
        cursor.execute(f"SELECT security FROM {mssql.table} WITH (TABLOCKX, HOLDLOCK)")
        cursor.fetchall()
        cursor.execute("SELECT @@SPID")
        [(holding,)] = cursor.fetchall()
        with _client(mssql) as client:
            started = time.monotonic()
            response = client.post("/slowly", json=PRICE)
            waited_s = time.monotonic() - started
            assert (response.status_code, response.json()["detail"]) == (HTTPStatus.GATEWAY_TIMEOUT, "quick did not answer within its deadline of 1s")
            assert waited_s < 2
            assert _blocked_by(mssql, holding) == 0
    finally:
        holder.rollback()
        holder.close()
    assert _stored(mssql) == []


def _blocked_by(scratch: Scratch, session: object) -> int:
    """How many requests still wait on `session`'s locks, once SQL Server has had a moment to cancel any whose timeout
    passed: the driver's timeout cancels a statement on the server, and stops it holding the request's connection."""
    for _ in range(50):
        [(waiting,)] = scratch.rows(f"SELECT COUNT(*) FROM sys.dm_exec_requests WHERE blocking_session_id = {int(str(session))}")
        if waiting == 0:
            return 0
        time.sleep(0.1)
    return int(str(waiting))


BONDS = "CREATE TABLE bonds (security_id varchar(12) PRIMARY KEY, coupon_rate decimal(9,6) NOT NULL, convention varchar(8) NOT NULL, frequency int NOT NULL, maturity date NOT NULL)"
SETTLEMENTS = (
    "CREATE TABLE settlements (settlement_id uniqueidentifier PRIMARY KEY, security_id varchar(12) NOT NULL, face decimal(18,2) NOT NULL,"
    " settle date NOT NULL, accrued decimal(18,2) NOT NULL, recorded_at datetimeoffset NOT NULL)"
)


def test_the_examples_reads_and_writes_run_on_sql_server(mssql: Scratch) -> None:
    for table in ("settlements", "bonds"):
        mssql.drop(table)
    mssql.execute(BONDS, SETTLEMENTS, "INSERT INTO bonds VALUES ('US912810TM64', 0.045, '30/360', 2, '2034-11-15')")
    at = datetime(2026, 11, 6, 14, 30, tzinfo=UTC)
    given: dict[api.Resource, fakes.Fake] = {example.PRICING: fakes.Http(FastAPI()), example.TERMS: fakes.Cache(), example.CLOCK: fakes.Clock(at=at), example.IDS: fakes.Ids()}
    try:
        app = api.build(example.service, settings=example.Settings.given(databases={"warehouse": mssql.settings}), fakes=given)
        with TestClient(app.asgi) as client:
            bond = client.get("/bonds/US912810TM64").json()
            recorded = client.post("/settlements", json={"security_id": "US912810TM64", "face": "1000000.00", "settle": "2026-11-06"})
        stored = mssql.rows("SELECT settlement_id, security_id, face, settle, accrued, recorded_at FROM settlements")
    finally:
        for table in ("settlements", "bonds"):
            mssql.drop(table)
    assert bond == {"security_id": "US912810TM64", "coupon_rate": "0.045000", "convention": "30/360", "frequency": 2, "maturity": "2034-11-15"}
    assert recorded.status_code == HTTPStatus.CREATED
    [(settlement_id, security, face, settle, accrued, recorded_at)] = stored
    assert (UUID(str(settlement_id)), security, face, settle, accrued) == (UUID("00000000-0000-7000-8000-000000000001"), "US912810TM64", Decimal("1000000.00"), date(2026, 11, 6), Decimal("21375.00"))
    assert isinstance(recorded_at, datetime)
    assert recorded_at == at
