"""basis-api's integration tier: a request's transaction in a real database, SQL Server and Oracle, through the same
`BASIS_TEST_*` variables and scratch databases as basis-job's (packages/job/tests/integration/README.md). A write
commits once the response validates, and rolls back when the handler raises or the response does not validate; a
request's reads see its own writes; a statement past its deadline is stopped on the server, and its request's writes
are rolled back; and the example's SQL runs on SQL Server. The outbox `basis ddl` prints is created in each: a call
whose writes roll back is never made, and one its service refuses after the commit reaches it through `basis relay`,
with its key, once the service answers again. Every test makes its own tables and drops them after."""

import contextlib
import json
import os
import sys
import textwrap
import time
import uuid
from collections.abc import Callable, Generator, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from http import HTTPStatus
from pathlib import Path
from typing import Annotated, Literal, override
from uuid import UUID

import mssql_python
import oracledb
import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NoOpTracer
from pydantic import BaseModel, ConfigDict, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from basis import api
from basis.api import fakes, outboxes, relay
from basis.api.example import service as example
from basis.api.outboxes import Row
from basis.api.resources import DatabaseResource
from basis.core import command
from basis.core.databases import Statements, open_database
from basis.core.settings import GivenSettings

type Records = Callable[[], list[dict[str, object]]]  # the `logged` fixture's

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


WAREHOUSE = api.database("warehouse", deadline_s=10, max_concurrent=4)
QUICK = api.database("quick", deadline_s=1, max_concurrent=4)


class Price(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    security: str
    price: Decimal


CACHED = api.cache("cached", Price, ttl_s=60, max_entries=10, deadline_s=1)


class Refused(api.Error):
    status = HTTPStatus.CONFLICT
    title = "The price is refused"


class Missing(api.Error):
    status = HTTPStatus.NOT_FOUND
    title = "No price for the security"


def _service(scratch: Scratch) -> api.Service:
    insert = f"INSERT INTO {scratch.table} (security, price) VALUES ({scratch.bind('security')}, {scratch.bind('price')})"
    select = f"SELECT security, price FROM {scratch.table} WHERE security = {scratch.bind('security')}"

    async def read(security: str, warehouse: Annotated[api.Reader, api.given(WAREHOUSE)]) -> Price:
        if (found := await warehouse.fetch_one(Price, select, security=security)) is None:
            raise Missing(security)
        return found

    async def write(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)]) -> Price:
        await warehouse.execute(insert, security=price.security, price=price.price)
        return price

    async def write_and_read(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)]) -> Price:
        await warehouse.execute(insert, security=price.security, price=price.price)
        if (found := await warehouse.fetch_one(Price, select, security=price.security)) is None:
            raise Missing(price.security)
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

    async def write_then_cache(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)], cached: Annotated[api.Cache[Price], api.given(CACHED)]) -> Price:
        """Writes, then loads a cache's value with a query that waits on a lock, which the cache's deadline, shorter
        than the database's, stops; and carries on past it."""
        await warehouse.execute(insert, security=price.security, price=price.price)

        async def load() -> Price:
            return await warehouse.fetch_one(Price, f"SELECT security, price FROM {scratch.table}_locked") or price

        with contextlib.suppress(api.DeadlineExceededError):
            await cached.get(price.security, load)
        return price

    long = 30  # seconds: longer than any resource's deadline, which is the one these tests reach
    routes = [
        api.get("/prices/{security}", read, deadline_s=long, response=Price, errors=[Missing]),
        api.post("/prices", write, deadline_s=long, response=Price, errors=[]),
        api.post("/read-your-writes", write_and_read, deadline_s=long, response=Price, errors=[Missing]),
        api.post("/refused", refuse, deadline_s=long, response=Price, errors=[Refused]),
        api.post("/misanswered", misanswer, deadline_s=long, response=Price, errors=[]),
        api.post("/slowly", write_slowly, deadline_s=long, response=Price, errors=[]),
        api.post("/cached-write", write_then_cache, deadline_s=long, response=Price, errors=[]),
    ]
    return api.Service(name="scratch", settings=api.Settings, resources=[WAREHOUSE, QUICK, CACHED], routes=routes)


def _app(scratch: Scratch, given: fakes.Fakes | None = None) -> api.App:
    settings = api.Settings.given(databases={"warehouse": scratch.settings, "quick": scratch.settings})
    return api.build(_service(scratch), settings=settings, fakes=given)


def _client(scratch: Scratch) -> TestClient:
    return TestClient(_app(scratch).asgi)


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


@contextlib.contextmanager
def _locked(scratch: Scratch, table: str) -> Generator[mssql_python.Connection | oracledb.Connection]:
    """A connection holding `table` locked, in a transaction of its own, which the caller rolls back to let go."""
    holder = scratch.connect()
    try:
        cursor = holder.cursor()
        cursor.execute(f"SELECT security FROM {table} WITH (TABLOCKX, HOLDLOCK)")
        cursor.fetchall()
        yield holder
    finally:
        holder.rollback()
        holder.close()


def test_a_write_never_answers_2xx_when_another_deadline_stops_its_query(mssql: Scratch) -> None:
    """The query that loads a cache's value waits on a lock until the cache's deadline stops it, and the handler carries
    on: the request answers 504, and its write rolls back once the query ends."""
    locked = f"{mssql.table}_locked"
    mssql.execute(f"CREATE TABLE {locked} (security varchar(12), price decimal(18,6))")
    try:
        with _locked(mssql, locked) as holder, _client(mssql) as client:
            response = client.post("/cached-write", json=PRICE)
            holder.rollback()  # the query ends, and the request's transaction rolls back after it
        assert (response.status_code, response.json()["detail"]) == (
            HTTPStatus.GATEWAY_TIMEOUT,
            "a statement in warehouse did not finish before a deadline, so the request's writes there are rolled back, never committed",
        )
    finally:
        mssql.drop(locked)
    assert _stored(mssql) == []


class _SlowCommit(fakes.Database):
    """The scratch database itself, through basis-core, whose commits wait `wait_s` before SQL Server's own, as one
    waits on a slow log."""

    def __init__(self, scratch: Scratch, wait_s: float) -> None:
        super().__init__()
        self.real = open_database(GivenSettings(databases={"quick": scratch.settings}), "quick")
        self.wait_s = wait_s

    @override
    @contextlib.contextmanager
    def statements(self, *, writes: bool) -> Generator[Statements]:
        with self.real.statements(writes=writes) as statements:
            yield _Slowed(statements, self.wait_s)


@dataclass
class _Slowed:
    statements: Statements
    wait_s: float

    def query(self, sql: str, binds: Mapping[str, api.Bind], *, timeout_s: float) -> object:
        return self.statements.query(sql, binds, timeout_s=timeout_s)

    def execute(self, sql: str, binds: Mapping[str, api.Bind], *, timeout_s: float) -> int:
        return self.statements.execute(sql, binds, timeout_s=timeout_s)

    def commit(self) -> None:
        time.sleep(self.wait_s)
        self.statements.commit()


def test_a_commit_past_its_deadline_is_a_504_and_the_log_says_it_committed(mssql: Scratch, logged: Records) -> None:
    app = _app(mssql, {QUICK: _SlowCommit(mssql, wait_s=1.5)})
    spans = InMemorySpanExporter()
    app.provider.add_span_processor(SimpleSpanProcessor(spans))
    with TestClient(app.asgi) as client:
        response = client.post("/slowly", json=PRICE)
        read = client.get("/prices/US0000000000")  # a read, which ends in a rollback, of a security nothing wrote
    assert (response.status_code, response.json()["detail"]) == (
        HTTPStatus.GATEWAY_TIMEOUT,
        "quick's commit did not finish within its deadline of 1s: whether the request's writes there committed, the log says when it ends",
    )
    assert _stored(mssql) == [("US912810TM64", Decimal("101.250000"))]
    assert [record["message"] for record in logged() if record.get("ended") == "commit"] == ["quick's commit finished after its deadline, and committed the request's writes"]
    ending = {span.name: dict(span.attributes or {}) for span in spans.get_finished_spans() if span.name in {"quick commit", "warehouse rollback"}}
    assert (read.status_code, ending) == (
        HTTPStatus.NOT_FOUND,
        {
            "quick commit": {"db.system.name": "fake", "basis.resource": "quick", "basis.deadline_s": 1},
            "warehouse rollback": {"db.system.name": "microsoft.sql_server", "basis.resource": "warehouse", "basis.deadline_s": 10},
        },
    )


def _blocked_by(scratch: Scratch, session: object) -> int:
    """How many requests still wait on `session`'s locks, once SQL Server has had a moment to cancel any whose timeout
    passed: the driver's timeout cancels a statement on the server, and stops it holding the request's connection."""
    for _ in range(50):
        [(waiting,)] = scratch.rows(f"SELECT COUNT(*) FROM sys.dm_exec_requests WHERE blocking_session_id = {int(str(session))}")
        if waiting == 0:
            return 0
        time.sleep(0.1)
    return int(str(waiting))


BONDS = (
    "CREATE TABLE bonds (security_id varchar(12) PRIMARY KEY, coupon_rate decimal(9,6) NOT NULL, convention varchar(8) NOT NULL, frequency int NOT NULL, issued date NOT NULL, maturity date NOT NULL)"
)
SETTLEMENTS = (
    "CREATE TABLE settlements (settlement_id uniqueidentifier PRIMARY KEY, security_id varchar(12) NOT NULL, face decimal(18,2) NOT NULL,"
    " settle date NOT NULL, accrued decimal(18,2) NOT NULL, recorded_at datetimeoffset NOT NULL)"
)


def test_the_examples_reads_and_writes_run_on_sql_server(mssql: Scratch) -> None:
    """The example's own tables, and its outbox's, as `basis ddl` prints it: a settlement's row and its booking with the
    ledger commit together, and the booking is made once they have."""
    tables = ("settlements", "bonds", "accruals_outbox")
    for table in tables:
        mssql.drop(table)
    outbox = [statement for statement in outboxes.ddl("warehouse", "accruals_outbox", "mssql").split(";\n") if statement.strip()]
    mssql.execute(BONDS, SETTLEMENTS, *outbox, "INSERT INTO bonds VALUES ('US912810TM64', 0.045, '30/360', 2, '2024-11-15', '2034-11-15')")
    at = datetime(2026, 11, 6, 14, 30, tzinfo=UTC)
    ledger = Ledger(status=HTTPStatus.CREATED)
    given: fakes.Fakes = {
        example.SOLVE: fakes.Compute(), example.PRICING: fakes.Http(FastAPI()), example.LEDGER: fakes.Http(ledger.app), example.TERMS: fakes.Cache(),
        example.CLOCK: fakes.Clock(at=at), example.IDS: fakes.Ids(),
    }  # fmt: skip
    try:
        app = api.build(example.service, settings=example.Settings.given(databases={"warehouse": mssql.settings}), fakes=given)
        with TestClient(app.asgi) as client:
            bond = client.get("/bonds/US912810TM64").json()
            recorded = client.post("/settlements", json={"security_id": "US912810TM64", "face": "1000000.00", "settle": "2026-11-06"})
        stored = mssql.rows("SELECT settlement_id, security_id, face, settle, accrued, recorded_at FROM settlements")
        booked = mssql.rows("SELECT idempotency_key, status FROM accruals_outbox")
    finally:
        for table in tables:
            mssql.drop(table)
    assert bond == {"security_id": "US912810TM64", "coupon_rate": "0.045000", "convention": "30/360", "frequency": 2, "issued": "2024-11-15", "maturity": "2034-11-15"}
    assert recorded.status_code == HTTPStatus.CREATED
    [(settlement_id, security, face, settle, accrued, recorded_at)] = stored
    assert (UUID(str(settlement_id)), security, face, settle, accrued) == (UUID("00000000-0000-7000-8000-000000000001"), "US912810TM64", Decimal("1000000.00"), date(2026, 11, 6), Decimal("21375.00"))
    assert isinstance(recorded_at, datetime)
    assert recorded_at == at
    assert booked == [("00000000-0000-7000-8000-000000000001", "done")]
    assert [key for key, _ in ledger.seen] == ["00000000-0000-7000-8000-000000000001"]


# The outbox


NOTE = "n" * 40_000
"""A call's body this long passes the sizes a driver binds inline, a VARCHAR2's 32,767 bytes and an NVARCHAR's 4,000
characters, so that it is written to and read from the outbox's CLOB or NVARCHAR(MAX) as a large value."""
LEDGER = api.http("ledger", deadline_s=5)


class Ledger:
    """The ledger service, as a fake in this process: each call it is sent, with its `Idempotency-Key`, and the status
    it answers, which a test changes."""

    def __init__(self, *, status: int) -> None:
        self.status = status
        self.seen: list[tuple[str | None, object]] = []
        self.app = FastAPI()

        @self.app.post("/bookings")
        async def bookings(request: Request) -> Response:
            self.seen.append((request.headers.get("idempotency-key"), json.loads(await request.body())))
            return Response(status_code=self.status)


@dataclass(frozen=True)
class Outboxed:
    """A scratch database with an outbox table, as `basis ddl` prints it, and a service whose routes send calls
    through it."""

    scratch: Scratch
    table: str
    service: api.Service

    @property
    def outbox(self) -> api.Outbox:
        [declared] = [resource.outbox for resource in self.service.resources if isinstance(resource, DatabaseResource) and resource.outbox is not None]
        return declared

    def app(self, ledger: Ledger) -> api.App:
        return api.build(self.service, settings=self.settings, fakes={LEDGER: fakes.Http(ledger.app)})

    @property
    def settings(self) -> api.Settings:
        return api.Settings.given(databases={"warehouse": self.scratch.settings})

    def run_relay(self, ledger: Ledger, *, after: timedelta) -> list[relay.Relayed]:
        """A run of `basis relay`, `after` from now: as far on as the next attempt of a call."""
        later = datetime.now(UTC) + after
        return relay.relay(relay.plan(self.service, self.settings, {LEDGER: fakes.Http(ledger.app)}), NoOpTracer(), now=lambda: later)

    def rows(self) -> list[Row]:
        columns = ", ".join(outboxes.COLUMNS)
        with open_database(GivenSettings(databases={"warehouse": self.scratch.settings}), "warehouse").statements(writes=False) as statements:
            found = statements.query(f"SELECT {columns} FROM {self.table} ORDER BY created_at", {}, timeout_s=10)
        return [Row.model_validate(values) for values in found.to_pylist()]


def _outboxed_service(scratch: Scratch, table: str) -> api.Service:
    warehouse = api.database("warehouse", deadline_s=10, max_concurrent=4, outbox=api.outbox(table, max_age_s=3600, backoff_s=60, max_backoff_s=600))
    insert = f"INSERT INTO {scratch.table} (security, price) VALUES ({scratch.bind('security')}, {scratch.bind('price')})"

    async def book(price: Price, warehouse_: Annotated[api.Writer, api.given(warehouse)], ledger: Annotated[api.Client, api.given(LEDGER)]) -> Price:
        await warehouse_.execute(insert, security=price.security, price=price.price)
        await ledger.send("POST", "/bookings", {"security": price.security, "price": str(price.price), "note": NOTE}, key=f"booking-{price.security}")
        return price

    async def book_then_refuse(price: Price, warehouse_: Annotated[api.Writer, api.given(warehouse)], ledger: Annotated[api.Client, api.given(LEDGER)]) -> Price:
        await book(price, warehouse_, ledger)
        msg = f"{price.security} is refused after all"
        raise Refused(msg)

    routes = [api.post("/book", book, deadline_s=30, response=Price, errors=[]), api.post("/refused", book_then_refuse, deadline_s=30, response=Price, errors=[Refused])]
    return api.Service(name="booking", settings=api.Settings, resources=[warehouse, LEDGER], routes=routes)


@pytest.fixture
def outboxed(scratch: Scratch) -> Iterator[Outboxed]:
    table = f"{scratch.table}_outbox"
    scratch.execute(*(statement for statement in outboxes.ddl("warehouse", table, scratch.dialect).split(";\n") if statement.strip()))
    yield Outboxed(scratch, table, _outboxed_service(scratch, table))
    scratch.drop(table)


BOOKED = {"security": "US912810TM64", "price": "101.25", "note": NOTE}
KEY = "booking-US912810TM64"


def test_a_route_whose_writes_roll_back_makes_no_call(outboxed: Outboxed) -> None:
    ledger = Ledger(status=HTTPStatus.CREATED)
    with TestClient(outboxed.app(ledger).asgi) as client:
        assert client.post("/refused", json=PRICE).status_code == HTTPStatus.CONFLICT
    [relayed] = outboxed.run_relay(ledger, after=timedelta(hours=2))
    assert (ledger.seen, outboxed.rows(), _stored(outboxed.scratch), relayed.accepted) == ([], [], [], 0)


def test_a_call_accepted_after_its_commit_is_done(outboxed: Outboxed) -> None:
    ledger = Ledger(status=HTTPStatus.CREATED)
    with TestClient(outboxed.app(ledger).asgi) as client:
        assert client.post("/book", json=PRICE).status_code == HTTPStatus.OK
    [row] = outboxed.rows()
    assert (ledger.seen, row.status, row.attempts, row.next_at, row.body) == ([(KEY, BOOKED)], "done", 1, None, json.dumps(BOOKED, separators=(",", ":")))


def test_a_call_that_fails_after_its_commit_reaches_the_service_through_the_relay_once_it_answers_again(outboxed: Outboxed) -> None:
    ledger = Ledger(status=HTTPStatus.SERVICE_UNAVAILABLE)
    with TestClient(outboxed.app(ledger).asgi) as client:
        response = client.post("/book", json=PRICE)
    assert (response.status_code, response.json()) == (HTTPStatus.OK, PRICE)  # the writes committed, whatever the ledger said
    assert _stored(outboxed.scratch) == [("US912810TM64", Decimal("101.250000"))]
    [after_commit] = outboxed.rows()
    assert (after_commit.status, after_commit.attempts, after_commit.last_error) == ("pending", 1, "ledger answered 503 to POST /bookings")

    [still_down] = outboxed.run_relay(ledger, after=timedelta(seconds=61))
    ledger.status = HTTPStatus.CREATED
    [answered] = outboxed.run_relay(ledger, after=timedelta(seconds=61 + 121))
    [row] = outboxed.rows()
    assert ((still_down.retried, still_down.accepted), (answered.retried, answered.accepted)) == ((1, 0), (0, 1))
    assert ledger.seen == [(KEY, BOOKED)] * 3  # after the commit, then the relay's two attempts, each with its key
    assert (row.status, row.attempts, row.next_at, row.idempotency_key) == ("done", 3, None, KEY)


def test_a_call_that_ages_out_is_made_no_more_and_fails_the_relay(outboxed: Outboxed) -> None:
    ledger = Ledger(status=HTTPStatus.SERVICE_UNAVAILABLE)
    with TestClient(outboxed.app(ledger).asgi) as client:
        assert client.post("/book", json=PRICE).status_code == HTTPStatus.OK
    found = outboxed.run_relay(ledger, after=timedelta(hours=1, seconds=1))
    [row] = outboxed.rows()
    assert (len(ledger.seen), row.status, row.next_at) == (1, "expired", None)
    failed = relay.failure(found, "booking")
    assert failed is not None
    assert f"POST /bookings to ledger with Idempotency-Key {KEY}" in failed[0]


def test_a_call_its_service_refuses_for_good_after_its_commit_is_made_no_more_and_the_relay_reports_it_once(outboxed: Outboxed) -> None:
    ledger = Ledger(status=HTTPStatus.UNPROCESSABLE_CONTENT)
    with TestClient(outboxed.app(ledger).asgi) as client:
        assert client.post("/book", json=PRICE).status_code == HTTPStatus.OK
    [after_commit] = outboxed.rows()
    assert (after_commit.status, after_commit.attempts, after_commit.last_error) == ("refused", 1, "ledger answered 422 to POST /bookings")
    first, again = outboxed.run_relay(ledger, after=timedelta(seconds=1)), outboxed.run_relay(ledger, after=timedelta(seconds=2))
    reported = relay.failure(first, "booking")
    assert reported is not None
    assert f"POST /bookings to ledger with Idempotency-Key {KEY}" in reported[0]
    assert "refused with: ledger answered 422 to POST /bookings" in reported[0]
    assert (relay.failure(again, "booking"), len(ledger.seen), [(row.status, row.next_at) for row in outboxed.rows()]) == (None, 1, [("refused", None)])


def test_two_relays_claim_a_call_once(outboxed: Outboxed) -> None:
    with TestClient(outboxed.app(Ledger(status=HTTPStatus.SERVICE_UNAVAILABLE)).asgi) as client:
        assert client.post("/book", json=PRICE).status_code == HTTPStatus.OK
    database = open_database(GivenSettings(databases={"warehouse": outboxed.scratch.settings}), "warehouse")
    later = datetime.now(UTC) + timedelta(seconds=61)
    claimed: list[bool] = []
    with database.statements(writes=True) as first, database.statements(writes=True) as second:
        tables = [outboxes.Sql(statements, outboxed.table, outboxed.scratch.dialect) for statements in (first, second)]
        [due], [seen_too] = (table.due(later, 10, timeout_s=10) for table in tables)
        claimed.append(outboxes.claim(tables[0], due, later + timedelta(seconds=30), timeout_s=10))
        first.commit()
        claimed.append(outboxes.claim(tables[1], seen_too, later + timedelta(seconds=30), timeout_s=10))
        late = outboxes.record(tables[1], outboxed.outbox, due, None, seen=0, attempts=1, at=later, reported=False, timeout_s=10)  # a request's record, too late
        second.commit()
    [row] = outboxed.rows()
    assert (claimed, late, row.attempts, row.status) == ([True, False], None, 2, "pending")


def test_an_app_whose_outbox_table_is_missing_does_not_start(scratch: Scratch) -> None:
    service = _outboxed_service(scratch, f"{scratch.table}_nowhere")
    app = api.build(service, settings=api.Settings.given(databases={"warehouse": scratch.settings}), fakes={LEDGER: fakes.Http(FastAPI())})
    with pytest.raises(api.DeclarationError, match=f"database warehouse's outbox {scratch.table}_nowhere cannot be read through its write login"), TestClient(app.asgi):
        pass


@pytest.mark.usefixtures("logging_restored")
def test_relay_fails_with_one_error_line_naming_a_call_that_aged_out(outboxed: Outboxed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """`basis relay` as the scheduler runs it: from the repo, with the API's settings in the environment."""
    with TestClient(outboxed.app(Ledger(status=HTTPStatus.SERVICE_UNAVAILABLE)).asgi) as client:
        assert client.post("/book", json=PRICE).status_code == HTTPStatus.OK
    outboxed.scratch.execute(f"UPDATE {outboxed.table} SET expires_at = created_at, next_at = created_at")  # aged out and due, as an hour on would leave it
    module = f"booking_{uuid.uuid4().hex[:8]}"
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "booking-api"\nversion = "0.1.0"\n\n[tool.basis]\nparts = ["api"]\n', encoding="utf-8")
    (tmp_path / f"{module}.py").write_text(
        textwrap.dedent(f"""
            from basis import api

            WAREHOUSE = api.database("warehouse", deadline_s=10, max_concurrent=4, outbox=api.outbox("{outboxed.table}", max_age_s=3600, backoff_s=60, max_backoff_s=600))
            LEDGER = api.http("ledger", deadline_s=5)


            def health() -> None: ...


            service = api.Service(name="booking", settings=api.Settings, resources=[WAREHOUSE, LEDGER], routes=[api.get("/up", health, deadline_s=1, response=None, errors=[], status=204)])
        """),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in [name for name in os.environ if name.startswith("BASIS_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("BASIS_DATABASES", json.dumps({"warehouse": outboxed.scratch.settings}))
    monkeypatch.setenv("BASIS_HTTP", json.dumps({"ledger": {"url": "http://127.0.0.1:9"}}))  # the discard port: an aged-out call is never made
    assert command.run(["relay", module], cwd=tmp_path, installed=command.installed()) == 1
    out, err = capsys.readouterr()
    expected = {"database": "warehouse", "table": outboxed.table, "accepted": 0, "retried": 0, "taken": 0, "refused": 0, "expired": 1, "failed": 0}
    assert json.loads(out)["outboxes"] == [expected]
    lines = err.splitlines()
    assert json.loads(lines[-2])["message"] == "finished relay"
    assert lines[-1].startswith(f"ERROR: service booking's outbox has a call aged out, and made no more: POST /bookings to ledger with Idempotency-Key {KEY}")
    assert [row.status for row in outboxed.rows()] == ["expired"]
    del sys.modules[module]
