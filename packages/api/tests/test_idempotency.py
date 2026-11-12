"""A request that can be sent twice (`idempotent.py`): a route that writes declares `idempotency="required"` or
`"optional"`, and its database an `api.idempotency` table; a request's `Idempotency-Key` is kept in the request's
transaction with what it answered, so the same request again is answered the same without running its handler; the
same key with another request is refused (422), a key in use by a request not yet committed is a conflict (409), and a
route that requires one refuses a request without it (400), as the IETF's Idempotency-Key draft has it."""

import json
import time
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Annotated

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from basis import api
from basis.api import fakes, idempotent, openapi, origins, tokens
from basis.api.identity import Identity
from basis.api.service import Idempotent
from basis.core.dialects import Dialect

KEYS = api.idempotency("orders_requests", keep_s=3600)
WAREHOUSE = api.database("warehouse", deadline_s=1, max_concurrent=2, idempotency=KEYS)
INSERT = "INSERT INTO orders (security, quantity) VALUES (%(security)s, %(quantity)s)"
IDENTITY = {"issuer": "https://sign-in.example", "audience": "orders", "algorithms": ["ES256"], "leeway_s": 0, "keys": {"kind": "development"}}


class Order(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    security: str
    quantity: int


class Placed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    number: int
    security: str


class Halted(api.Error):
    status = HTTPStatus.CONFLICT
    title = "Trading is halted"


PLACED: list[Order] = []


async def place(order: Order, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)], user: Annotated[api.User | None, api.given(api.User)]) -> Placed:
    del user
    if order.security == "HALTED":
        msg = "trading in it is halted"
        raise Halted(msg)
    await warehouse.execute(INSERT, security=order.security, quantity=order.quantity)
    PLACED.append(order)
    return Placed(number=len(PLACED), security=order.security)


def _service(kind: Idempotent = "required", keys: api.Idempotency = KEYS) -> api.Service:
    warehouse = WAREHOUSE if keys is KEYS else api.database("warehouse", deadline_s=1, max_concurrent=2, idempotency=keys)

    async def placing(order: Order, warehouse_: Annotated[api.Writer, api.given(warehouse)], user: Annotated[api.User | None, api.given(api.User)]) -> Placed:
        return await place(order, warehouse_, user)

    placing.__name__ = "place"
    route = api.post("/orders", placing, status=HTTPStatus.CREATED, deadline_s=1, response=Placed, errors=[Halted], idempotency=kind)
    return api.Service(name="orders", settings=api.Settings, resources=[warehouse], user=api.User, routes=[route])


ORDER = {"security": "US0001", "quantity": 100}


@pytest.fixture
def database() -> fakes.Database:
    PLACED.clear()
    return fakes.Database(answers={INSERT: 1})


def _client(database: fakes.Database, service: api.Service | None = None) -> TestClient:
    return TestClient(api.build(service or _service(), settings=api.Settings.given(identity=IDENTITY), fakes={WAREHOUSE: database}).asgi)


def _signed(subject: str) -> dict[str, str]:
    now = datetime.now(UTC)
    return {"authorization": f"Bearer {tokens.sign(Identity.model_validate(IDENTITY), subject, issued=now, expires=now + timedelta(hours=1))}"}


def test_the_same_request_again_is_answered_as_the_first_without_running_again(database: fakes.Database) -> None:
    with _client(database) as client:
        first = client.post("/orders", json=ORDER, headers={"idempotency-key": "order-1"})
        again = client.post("/orders", json=ORDER, headers={"idempotency-key": "order-1"})
    assert first.status_code == again.status_code == HTTPStatus.CREATED
    assert first.json() == again.json() == {"number": 1, "security": "US0001"}
    assert again.headers["content-type"] == "application/json"
    assert len(PLACED) == 1
    assert [statement.sql for statement in database.committed] == [INSERT]
    [kept] = database.idempotency
    assert (kept.idempotency_key, kept.route, kept.status_code) == ("order-1", "place", HTTPStatus.CREATED)


def test_another_key_is_another_request(database: fakes.Database) -> None:
    with _client(database) as client:
        assert client.post("/orders", json=ORDER, headers={"idempotency-key": "order-1"}).json()["number"] == 1
        assert client.post("/orders", json=ORDER, headers={"idempotency-key": "order-2"}).json()["number"] == 2


def test_a_key_sent_again_with_another_request_is_refused(database: fakes.Database) -> None:
    with _client(database) as client:
        client.post("/orders", json=ORDER, headers={"idempotency-key": "order-1"}).raise_for_status()
        reused = client.post("/orders", json=ORDER | {"quantity": 200}, headers={"idempotency-key": "order-1"})
    assert reused.status_code == HTTPStatus.UNPROCESSABLE_CONTENT
    problem = reused.json()
    assert problem["type"] == "idempotency-key-reused"
    assert problem["errors"] == [{"detail": problem["errors"][0]["detail"], "parameter": "Idempotency-Key"}]
    assert len(PLACED) == 1


def test_a_route_that_requires_a_key_refuses_a_request_without_one(database: fakes.Database) -> None:
    with _client(database) as client:
        missing = client.post("/orders", json=ORDER)
    assert (missing.status_code, missing.json()["type"]) == (HTTPStatus.BAD_REQUEST, "idempotency-key-missing")
    assert PLACED == []


def test_a_route_where_a_key_is_optional_runs_each_request_without_one(database: fakes.Database) -> None:
    with _client(database, _service("optional")) as client:
        for _ in range(2):
            client.post("/orders", json=ORDER).raise_for_status()
    assert len(PLACED) == 2
    assert database.idempotency == []


@pytest.mark.parametrize("key", ["", "x" * 256, "naïve", "with space"], ids=["empty", "too-long", "not-ascii", "a-space"])
def test_a_key_that_is_not_one_to_255_visible_characters_is_an_invalid_request(database: fakes.Database, key: str) -> None:
    with _client(database) as client:
        refused = client.post("/orders", json=ORDER, headers=[(b"idempotency-key", key.encode())])
    assert refused.status_code == HTTPStatus.UNPROCESSABLE_CONTENT
    assert refused.json()["type"] == "invalid-request"
    assert [entry.get("parameter") for entry in refused.json()["errors"]] == ["Idempotency-Key"]


def test_a_request_that_fails_keeps_no_key_so_it_runs_again(database: fakes.Database) -> None:
    with _client(database) as client:
        halted = {"security": "HALTED", "quantity": 1}
        for _ in range(2):
            assert client.post("/orders", json=halted, headers={"idempotency-key": "order-1"}).status_code == HTTPStatus.CONFLICT
    assert database.idempotency == []


def test_a_key_in_use_by_a_request_not_yet_committed_is_a_conflict(database: fakes.Database) -> None:
    database.fails(api.Conflict, statement=fakes.IDEMPOTENCY_ADD)
    with _client(database) as client:
        busy = client.post("/orders", json=ORDER, headers={"idempotency-key": "order-1"})
    assert (busy.status_code, busy.json()["type"]) == (HTTPStatus.CONFLICT, "idempotency-key-in-use")
    assert PLACED == []


def test_the_fake_refuses_a_key_another_transaction_holds_until_it_ends(database: fakes.Database) -> None:
    kept = idempotent.Kept(id="a" * 64, idempotency_key="k", route="place", subject=None, fingerprint="f" * 64, created_at=datetime.now(UTC), status_code=None, body=None)
    first, second = database.transaction(writes=True), database.transaction(writes=True)
    first.idempotency().add(kept, timeout_s=1)
    with pytest.raises(api.Conflict):
        second.idempotency().add(kept, timeout_s=1)
    assert second.idempotency().find(kept.id, timeout_s=1) is None  # not committed, so another transaction sees nothing
    first.roll_back()
    second.idempotency().add(kept, timeout_s=1)
    second.commit()
    assert database.idempotency == [kept]


def test_keys_are_each_users_own(database: fakes.Database) -> None:
    with _client(database) as client:
        for subject in ("trader-1", "trader-2"):
            client.post("/orders", json=ORDER, headers={"idempotency-key": "order-1"} | _signed(subject)).raise_for_status()
    assert len(PLACED) == 2


def test_a_key_kept_longer_than_keep_s_is_forgotten(database: fakes.Database) -> None:
    service = _service(keys=api.idempotency("orders_requests", keep_s=0.05))
    [warehouse] = service.resources
    with TestClient(api.build(service, settings=api.Settings.given(identity=IDENTITY), fakes={warehouse: database}).asgi) as client:
        client.post("/orders", json=ORDER, headers={"idempotency-key": "order-1"}).raise_for_status()
        time.sleep(0.1)
        assert client.post("/orders", json=ORDER, headers={"idempotency-key": "order-1"}).json()["number"] == 2
    assert len(database.idempotency) == 1


def test_a_route_declares_idempotency_only_where_it_writes_a_database_that_keeps_keys() -> None:
    reader = api.database("warehouse", deadline_s=1, max_concurrent=2)

    async def read(warehouse: Annotated[api.Reader, api.given(reader)]) -> Placed:
        del warehouse
        return Placed(number=1, security="US0001")

    with pytest.raises(api.DeclarationError, match="idempotency='required' or 'optional'"):
        api.post("/orders", read, deadline_s=1, response=Placed, errors=[], anonymous=True, idempotency="always")  # ty: ignore[invalid-argument-type] -- the refusal under test
    with pytest.raises(api.DeclarationError, match="writes no database"):
        api.post("/orders", read, deadline_s=1, response=Placed, errors=[], anonymous=True, idempotency="required")

    async def write(warehouse: Annotated[api.Writer, api.given(reader)]) -> Placed:
        del warehouse
        return Placed(number=1, security="US0001")

    with pytest.raises(api.DeclarationError, match=r"idempotency=api\.idempotency"):
        api.Service(name="orders", settings=api.Settings, resources=[reader], routes=[api.post("/orders", write, deadline_s=1, response=Placed, errors=[], anonymous=True, idempotency="required")])
    with pytest.raises(api.DeclarationError, match="keep_s"):
        api.idempotency("orders_requests", keep_s=0)
    with pytest.raises(api.DeclarationError, match="name"):
        api.idempotency("orders requests", keep_s=60)


def test_the_document_gives_the_header_and_its_errors() -> None:
    for kind, required in (("required", True), ("optional", False)):
        operation = openapi.mapping(openapi.mapping(openapi.mapping(openapi.document(_service(kind))["paths"])["/orders"])["post"])
        [header] = [parameter for parameter in openapi.array(operation["parameters"]) if openapi.mapping(parameter).get("in") == "header"]
        assert header == {"name": "Idempotency-Key", "in": "header", "required": required, "schema": {"type": "string", "minLength": 1, "maxLength": 255, "pattern": "^[!-~]+$"}}
        listed = {openapi.mapping(entry)["type"]: openapi.mapping(entry)["status"] for entry in openapi.array(operation["x-basis-errors"])}
        assert listed["idempotency-key-reused"] == HTTPStatus.UNPROCESSABLE_CONTENT
        assert listed["idempotency-key-in-use"] == HTTPStatus.CONFLICT
        assert ("idempotency-key-missing" in listed) is required


def test_a_page_of_an_allowed_origin_may_send_the_key() -> None:
    assert "Idempotency-Key" in origins.HEADERS


@pytest.mark.parametrize("dialect", ["mssql", "oracle"])
def test_the_ddl_is_the_keys_table(dialect: Dialect) -> None:
    created = idempotent.ddl("warehouse", "orders_requests", dialect)
    assert created.startswith("CREATE TABLE orders_requests")
    for column in idempotent.COLUMNS:
        assert column in created


def test_a_fingerprint_is_the_request_as_sent() -> None:
    body = json.dumps(ORDER).encode()
    same = idempotent.fingerprint("POST", "/orders", "", body)
    assert same == idempotent.fingerprint("POST", "/orders", "", body)
    assert same != idempotent.fingerprint("POST", "/orders", "", body + b" ")
    assert same != idempotent.fingerprint("POST", "/orders", "dry=true", body)
