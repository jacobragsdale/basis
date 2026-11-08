"""The example's repository, in the integration tier: its SQL against a real SQL Server, a scratch database's, through
`BASIS_TEST_MSSQL` as basis-job's integration tier sets it up (packages/job/tests/integration/README.md). Its tables,
and its outbox's as `basis ddl` prints it, are made for each test and dropped after. The app runs on the real
warehouse and fakes of everything else, so a route's handler passes the warehouse to the repository as it does when
served."""

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from http import HTTPStatus
from uuid import UUID

import mssql_python
import pytest
from basis import api
from basis.api import fakes, outboxes
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from basis.api.example import resources, service

pytestmark = pytest.mark.integration

ISIN = "US912810TM64"
AT = datetime(2026, 11, 6, 14, 30, tzinfo=UTC)
TABLES = ("settlements", "bonds", "accruals_outbox")
BONDS = (
    "CREATE TABLE bonds (security_id varchar(12) PRIMARY KEY, coupon_rate decimal(9,6) NOT NULL, convention varchar(8) NOT NULL, frequency int NOT NULL, issued date NOT NULL, maturity date NOT NULL)"
)
SETTLEMENTS = (
    "CREATE TABLE settlements (settlement_id uniqueidentifier PRIMARY KEY, security_id varchar(12) NOT NULL, face decimal(18,2) NOT NULL,"
    " settle date NOT NULL, accrued decimal(18,2) NOT NULL, recorded_at datetimeoffset NOT NULL)"
)


@dataclass(frozen=True)
class Warehouse:
    """The scratch database, as the app's settings name it, and its rows as the test reads them."""

    connection_string: str

    @property
    def settings(self) -> dict[str, object]:
        given = self.connection_string
        return {"kind": "mssql", "connection_string": given, "isolation": "snapshot", "timeout_s": 10, "max_rows": 1000, "write": {"connection_string": given}}

    def execute(self, *statements: str) -> None:
        connection = mssql_python.connect(self.connection_string, autocommit=True)
        try:
            cursor = connection.cursor()
            for statement in statements:
                cursor.execute(statement)
        finally:
            connection.close()

    def rows(self, sql: str) -> list[tuple[object, ...]]:
        connection = mssql_python.connect(self.connection_string)
        try:
            cursor = connection.cursor()
            cursor.execute(sql)
            return [tuple(row) for row in cursor.fetchall()]
        finally:
            connection.close()


@pytest.fixture
def warehouse() -> Iterator[Warehouse]:
    connection_string = os.environ.get("BASIS_TEST_MSSQL", "")
    if not connection_string:
        pytest.fail("set BASIS_TEST_MSSQL to a connection string for a scratch SQL Server database (see packages/job/tests/integration/README.md)")
    found = Warehouse(connection_string)
    drop = [f"IF OBJECT_ID('{table}') IS NOT NULL DROP TABLE {table}" for table in TABLES]
    found.execute(*drop)
    outbox = [statement for statement in outboxes.ddl("warehouse", "accruals_outbox", "mssql").split(";\n") if statement.strip()]
    found.execute(BONDS, SETTLEMENTS, *outbox, "INSERT INTO bonds VALUES ('US912810TM64', 0.045, '30/360', 2, '2024-11-15', '2034-11-15')")
    yield found
    found.execute(*drop)


class Ledger:
    """The ledger service, which takes every booking, with its `Idempotency-Key`."""

    def __init__(self) -> None:
        self.keys: list[str | None] = []
        self.app = FastAPI()

        @self.app.post("/bookings", status_code=HTTPStatus.CREATED)
        async def book(request: Request) -> None:
            self.keys.append(request.headers.get("idempotency-key"))


def app(warehouse: Warehouse, ledger: Ledger) -> api.App:
    given: fakes.Fakes = {
        resources.SOLVE: fakes.Compute(), resources.PRICING: fakes.Http(FastAPI()), resources.LEDGER: fakes.Http(ledger.app), resources.TERMS: fakes.Cache(),
        resources.CLOCK: fakes.Clock(at=AT), resources.IDS: fakes.Ids(),
    }  # fmt: skip
    return api.build(service.service, settings=resources.Settings.given(databases={"warehouse": warehouse.settings}), fakes=given)


def test_a_bonds_terms_are_read_from_the_warehouse(warehouse: Warehouse) -> None:
    with TestClient(app(warehouse, Ledger()).asgi) as client:
        bond = client.get(f"/bonds/{ISIN}").json()
        unknown = client.get("/bonds/US0000000000")
    assert bond == {"security_id": ISIN, "coupon_rate": "0.045000", "convention": "30/360", "frequency": 2, "issued": "2024-11-15", "maturity": "2034-11-15"}
    assert (unknown.status_code, unknown.json()["type"]) == (HTTPStatus.NOT_FOUND, "unknown-bond")


def test_a_settlement_and_its_booking_commit_together_and_the_booking_is_made_after(warehouse: Warehouse) -> None:
    ledger = Ledger()
    with TestClient(app(warehouse, ledger).asgi) as client:
        recorded = client.post("/settlements", json={"security_id": ISIN, "face": "1000000.00", "settle": "2026-11-06"})
    assert recorded.status_code == HTTPStatus.CREATED
    [(settlement_id, security, face, settle, accrued, recorded_at)] = warehouse.rows("SELECT settlement_id, security_id, face, settle, accrued, recorded_at FROM settlements")
    assert (UUID(str(settlement_id)), security, face, settle, accrued) == (UUID("00000000-0000-7000-8000-000000000001"), ISIN, Decimal("1000000.00"), date(2026, 11, 6), Decimal("21375.00"))
    assert recorded_at == AT
    assert warehouse.rows("SELECT idempotency_key, status FROM accruals_outbox") == [("00000000-0000-7000-8000-000000000001", "done")]
    assert ledger.keys == ["00000000-0000-7000-8000-000000000001"]


def test_a_settlement_on_a_bond_the_warehouse_does_not_hold_writes_nothing(warehouse: Warehouse) -> None:
    ledger = Ledger()
    with TestClient(app(warehouse, ledger).asgi) as client:
        refused = client.post("/settlements", json={"security_id": "US0000000000", "face": "100.00", "settle": "2026-11-06"})
    assert (refused.status_code, refused.json()["type"]) == (HTTPStatus.NOT_FOUND, "unknown-bond")
    assert (warehouse.rows("SELECT COUNT(*) FROM settlements"), warehouse.rows("SELECT COUNT(*) FROM accruals_outbox"), ledger.keys) == ([(0,)], [(0,)], [])
