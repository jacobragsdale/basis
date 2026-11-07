"""What every route declares, and how the app serves it: its request read strictly, within its deadline, answering only
the errors it declares, each listed in the OpenAPI document."""

import asyncio
from collections.abc import Callable, Iterator, Mapping
from datetime import date, datetime, time
from decimal import Decimal
from enum import StrEnum
from http import HTTPStatus
from time import sleep
from typing import Annotated, Literal
from uuid import UUID

import pytest
from fastapi import Body, FastAPI, Query
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, JsonValue, RootModel, TypeAdapter

from basis import api
from basis.api import fakes, strict


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class Quote(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    security: str
    price: Decimal


class Leg(BaseModel):
    """Lax, as pydantic's default is: basis reads a request strictly whatever a model says."""

    rate: Decimal
    at: datetime


class Order(BaseModel):
    model_config = ConfigDict(frozen=True)

    security: str
    quantity: int
    price: Decimal
    hedged: bool = False
    legs: tuple[Leg, ...] = ()


class Stale(api.Error):
    status = HTTPStatus.CONFLICT
    title = "The quote is stale"


class Unlisted(api.Error):
    status = HTTPStatus.NOT_FOUND
    title = "No such security"


class Priced(api.Error, abstract=True): ...


WAREHOUSE = api.database("warehouse", deadline_s=1, max_concurrent=2)
PRICING = api.http("pricing", deadline_s=1)
PRICES = api.cache("prices", Quote, ttl_s=60, max_entries=10, deadline_s=1)
SLOW = "SELECT security, price FROM quotes WITH (HOLDLOCK)"
INSERT = "INSERT INTO orders (security) VALUES (%(security)s)"


def quote(security: str, size: int, side: Side, venue: Annotated[tuple[str, ...], Query()] = (), at: datetime | None = None) -> Quote:
    del size, side, venue, at
    return Quote(security=security, price=Decimal("101.25"))


def lot(number: int) -> Quote:
    return Quote(security=str(number), price=Decimal(1))


def place(order: Order) -> Order:
    return order


def stale(security: str) -> Quote:
    msg = f"{security}'s quote is a day old"
    raise Stale(msg)


def unlisted(security: str) -> Quote:
    msg = f"{security} is not listed"
    raise Unlisted(msg)


async def from_the_warehouse(warehouse: Annotated[api.Reader, api.given(WAREHOUSE)]) -> list[Quote]:
    return await warehouse.fetch(Quote, SLOW)


async def from_pricing(pricing: Annotated[api.Client, api.given(PRICING)]) -> Quote:
    return await pricing.get("/slowly", Quote)


async def from_the_cache(prices: Annotated[api.Cache[Quote], api.given(PRICES)]) -> Quote:
    async def load() -> Quote:
        await asyncio.sleep(0.5)
        return Quote(security="X", price=Decimal(1))

    return await prices.get("X", load)


async def write_then_dawdle(order: Order, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)]) -> Order:
    await warehouse.execute(INSERT, security=order.security)
    await asyncio.sleep(0.2)  # past its route's deadline, before the commit
    return order


def _service(*routes: api.Route) -> api.Service:
    return api.Service(name="quotes", settings=api.Settings, resources=[WAREHOUSE, PRICING, PRICES], routes=routes)


SERVICE = _service(
    api.get("/quotes/{security}", quote, deadline_s=1, response=Quote, errors=[]),
    api.get("/lots/{number}", lot, deadline_s=1, response=Quote, errors=[]),
    api.post("/orders", place, deadline_s=1, response=Order, errors=[]),
    api.get("/stale/{security}", stale, deadline_s=1, response=Quote, errors=[Stale]),
    api.get("/unlisted/{security}", unlisted, deadline_s=1, response=Quote, errors=[Stale]),
    api.get("/warehouse", from_the_warehouse, deadline_s=0.05, response=list[Quote], errors=[]),
    api.get("/pricing", from_pricing, deadline_s=0.05, response=Quote, errors=[]),
    api.get("/cache", from_the_cache, deadline_s=0.05, response=Quote, errors=[]),
    api.post("/dawdle", write_then_dawdle, deadline_s=0.1, response=Order, errors=[]),
)


def _waits(binds: Mapping[str, api.Bind]) -> list[Mapping[str, object]]:
    del binds
    sleep(0.5)  # on the database's thread, as a driver blocks
    return []


def _pricing() -> FastAPI:
    pricing = FastAPI()

    @pricing.get("/slowly")
    async def slowly() -> dict[str, str]:
        await asyncio.sleep(0.5)
        return {"security": "X", "price": "1"}

    return pricing


@pytest.fixture
def warehouse() -> fakes.Database:
    return fakes.Database(answers={SLOW: fakes.Respond(_waits), INSERT: 1})


@pytest.fixture
def client(warehouse: fakes.Database) -> Iterator[TestClient]:
    given: fakes.Fakes = {WAREHOUSE: warehouse, PRICING: fakes.Http(_pricing()), PRICES: fakes.Cache()}
    with TestClient(api.build(SERVICE, settings=api.Settings.given(), fakes=given).asgi) as started:
        yield started


def _ok(handler: Callable[..., object], **changed: object) -> Callable[[], api.Route]:
    declared: dict[str, object] = {"deadline_s": 1, "response": Quote, "errors": []} | changed
    return lambda: api.get("/q/{security}", handler, **declared)  # ty: ignore[invalid-argument-type] -- each case declares one thing wrong on purpose


def no_annotation(security: str):  # noqa: ANN201 -- the case under test
    return Quote(security=security, price=Decimal(1))


def nothing(security: str) -> None:
    del security


def by_time(security: str, at: time) -> Quote:
    del at
    return Quote(security=security, price=Decimal(1))


def by_mixed(security: str, pair: Annotated[tuple[int, str], Query()]) -> Quote:
    del pair
    return Quote(security=security, price=Decimal(1))


def two_bodies(security: str, order: Order, other: Order) -> Quote:
    del order, other
    return Quote(security=security, price=Decimal(1))


def embedded(security: str, order: Annotated[Order, Body(embed=True)]) -> Quote:
    del order
    return Quote(security=security, price=Decimal(1))


@pytest.mark.parametrize(
    ("declare", "message"),
    [
        (_ok(stale, deadline_s=0), "give it deadline_s, the seconds the route has to answer, above 0"),
        (_ok(stale, deadline_s=float("inf")), "give it deadline_s"),
        (_ok(stale, deadline_s=True), "give it deadline_s"),
        (_ok(stale, deadline_s="1"), "give it deadline_s"),
        (_ok(no_annotation), r"no_annotation has no return annotation: annotate it `-> Quote`"),
        (_ok(stale, response=Order), "stale returns Quote, and its route declares response=Order"),
        (_ok(nothing, response=None), "a route answers no body exactly when its status is 204"),
        (_ok(stale, status=HTTPStatus.NO_CONTENT), "a route answers no body exactly when its status is 204"),
        (_ok(stale, errors=Stale), r"give errors=\[...\], each api.Error its handler may raise"),
        (_ok(stale, errors=[ValueError]), "declares <class 'ValueError'> among its errors"),
        (_ok(stale, errors=[Priced]), "a base that is never raised itself"),
        (_ok(stale, errors=[api.DeadlineExceededError]), "which basis raises itself"),
        (_ok(stale, errors=[Stale, Stale]), "declares Stale twice"),
        (lambda: api.get("/lots/{number:int}", lot, deadline_s=1, response=Quote, errors=[]), r"converts \{number:int\} in its path: name the parameter alone, as \{number\}"),
        (_ok(by_time), "query parameter at is time, which basis cannot read strictly from text"),
        (_ok(by_mixed), "query parameter pair is tuple"),
        (lambda: api.post("/q/{security}", two_bodies, deadline_s=1, response=Quote, errors=[]), "takes 2 bodies, order, other"),
        (lambda: api.post("/q/{security}", embedded, deadline_s=1, response=Quote, errors=[]), "drop embed=True"),
    ],
    ids=[
        "no-deadline", "endless-deadline", "deadline-as-a-bool", "deadline-as-text", "no-return-annotation", "another-response", "none-without-204", "204-with-a-body",
        "errors-not-a-list", "not-an-error", "abstract-error", "basis-own-error", "error-twice", "path-converter", "time-in-query", "mixed-tuple-in-query", "two-bodies",
        "embedded-body",
    ],
)  # fmt: skip
def test_a_route_cannot_skip_its_declarations(declare: Callable[[], object], message: str) -> None:
    with pytest.raises(api.ServiceError, match=message):
        declare()


def test_basis_adds_the_errors_a_route_can_meet_to_those_it_declares() -> None:
    routes = {route.name: route.answers for route in SERVICE.routes}
    assert (routes["stale"], routes["from_the_warehouse"], routes["from_pricing"]) == ((Stale,), (api.DeadlineExceededError,), (api.DeadlineExceededError, api.CallFailedError))


class _Rating(StrEnum):
    A = "A"
    B = "B"


@pytest.mark.parametrize(
    ("kind", "text", "accepted"),
    [
        (int, "10", True), (int, "-3", True), (int, "0", True), (int, "010", False), (int, "+1", False), (int, "1.0", False), (int, " 1", False), (int, "1_000", False),
        (Decimal, "0.045", True), (Decimal, "-2.50", True), (Decimal, "1e3", False), (Decimal, ".5", False), (Decimal, "1.", False), (Decimal, "NaN", False),
        (float, "2e-3", True), (float, "1.5", True), (float, "1e400", False), (float, "inf", False),
        (bool, "true", True), (bool, "false", True), (bool, "True", False), (bool, "1", False), (bool, "yes", False),
        (date, "2026-11-06", True), (date, "20261106", False), (date, "2026-11-06T00:00:00", False),
        (datetime, "2026-11-06T14:30:00Z", True), (datetime, "2026-11-06T09:30:00.123456-05:00", True), (datetime, "2026-11-06T14:30:00", False),
        (datetime, "2026-11-06T14:30:00.1234567Z", False), (datetime, "2026-11-06 14:30:00Z", False),
        (UUID, "019a5c3e-0000-7000-8000-000000000001", True), (UUID, "019a5c3e000070008000000000000001", False), (UUID, "{019a5c3e-0000-7000-8000-000000000001}", False),
        (Literal[1, 2, 4], "2", True), (Literal[1, 2, 4], "02", False), (Literal["30/360", "ACT/360"], "act/360", False), (_Rating, "A", True), (_Rating, "a", False),
        (str, " anything ", True),
    ],
)  # fmt: skip
def test_query_and_path_text_reads_by_its_types_grammar(kind: object, text: str, accepted: bool) -> None:  # noqa: FBT001 -- a table's column
    reads = strict.text(kind)
    assert reads is not None
    assert reads.accepts(text) is accepted


@pytest.mark.parametrize(
    ("path", "params", "errors"),
    [
        ("/quotes/X", {"size": "10", "side": "buy", "sise": "10"}, [("query", "/sise", "Unknown query parameter: this route takes size, side, venue, at")]),
        ("/quotes/X", {"size": "010", "side": "buy"}, [("query", "/size", "Input should be an integer, as 10 or -3: digits, with a minus sign only before them and no leading zero")]),
        ("/quotes/X", [("size", "10"), ("size", "11"), ("side", "buy")], [("query", "/size", "Given 2 times: give it once")]),
        ("/quotes/X", {"size": "10", "side": "BUY"}, [("query", "/side", "Input should be one of buy, sell")]),
        ("/quotes/X", {"size": "10", "side": "buy", "at": "2026-11-06T14:30:00"}, [("query", "/at", "Input should be a time with its offset, as 2026-11-06T14:30:00Z or 2026-11-06T09:30:00-05:00")]),
        ("/lots/007", {}, [("path", "/number", "Input should be an integer, as 10 or -3: digits, with a minus sign only before them and no leading zero")]),
    ],
    ids=["unknown-key", "leading-zero", "given-twice", "enum-case", "naive-time", "path"],
)
def test_query_and_path_parameters_are_read_strictly(client: TestClient, path: str, params: object, errors: list[tuple[str, str, str]]) -> None:
    response = client.get(path, params=params)  # ty: ignore[invalid-argument-type] -- a mapping or pairs, as httpx takes them
    assert (response.status_code, response.json()["type"]) == (HTTPStatus.UNPROCESSABLE_CONTENT, "invalid-request")
    assert [(error["location"], error["pointer"], error["detail"]) for error in response.json()["errors"]] == errors


def test_a_list_takes_its_key_once_for_each_value(client: TestClient) -> None:
    response = client.get("/quotes/X", params=[("size", "10"), ("side", "sell"), ("venue", "A"), ("venue", "B"), ("at", "2026-11-06T14:30:00Z")])
    assert response.status_code == HTTPStatus.OK


ORDER = {"security": "US912810TM64", "quantity": 10, "price": "12345678901234567890.123456789", "legs": [{"rate": "0.0450", "at": "2026-11-06T14:30:00Z"}]}


def test_a_body_reaches_the_handler_exactly(client: TestClient) -> None:
    response = client.post("/orders", json=ORDER)
    assert response.json() == ORDER | {"hedged": False}


@pytest.mark.parametrize(
    ("changed", "pointer", "kind"),
    [
        ({"quantity": "10"}, "/quantity", "Input should be a valid integer"),
        ({"quantity": 10.0}, "/quantity", "Input should be a valid integer"),
        ({"security": 912810}, "/security", "Input should be a valid string"),
        ({"hedged": "true"}, "/hedged", "Input should be a valid boolean"),
        ({"price": 101.25}, "/price", 'Input should be a string of the decimal, as "0.045"'),
        ({"legs": [{"rate": 0.045, "at": "2026-11-06T14:30:00Z"}]}, "/legs/0/rate", 'Input should be a string of the decimal, as "0.045"'),
        ({"legs": [{"rate": "0.045", "at": "2026-11-06T14:30:00"}]}, "/legs/0/at", "Input should be a time with its offset"),
        ({"side": "buy"}, "/side", "Extra inputs are not permitted"),
        ({"legs": [{"rate": "0.045", "at": "2026-11-06T14:30:00Z", "notional": "1"}]}, "/legs/0/notional", "Extra inputs are not permitted"),
    ],
    ids=["int-as-text", "int-as-a-float", "str-as-a-number", "bool-as-text", "decimal-as-a-number", "nested-decimal-as-a-number", "naive-time", "unknown-key", "nested-unknown-key"],
)
def test_a_body_is_read_strictly(client: TestClient, changed: dict[str, object], pointer: str, kind: str) -> None:
    response = client.post("/orders", json=ORDER | changed)
    [error] = response.json()["errors"]
    assert (response.status_code, error["location"], error["pointer"]) == (HTTPStatus.UNPROCESSABLE_CONTENT, "body", pointer)
    assert error["detail"].startswith(kind)


def test_a_body_is_json_only_when_the_request_says_so(client: TestClient) -> None:
    response = client.post("/orders", content=b'{"security": "X", "quantity": 1, "price": "1"}', headers={"content-type": "text/plain"})
    [error] = response.json()["errors"]
    assert (error["location"], error["detail"]) == ("body", "The body is text/plain, not JSON: send it as application/json")


def test_a_declared_error_answers_its_problem(client: TestClient) -> None:
    response = client.get("/stale/X")
    assert (response.status_code, response.json()["type"]) == (HTTPStatus.CONFLICT, "stale")


def test_an_error_the_route_does_not_declare_is_a_500_whose_log_names_it(client: TestClient, logged: Callable[[], list[dict[str, object]]]) -> None:
    response = client.get("/unlisted/X")
    assert (response.status_code, response.json()["title"]) == (HTTPStatus.INTERNAL_SERVER_ERROR, "Internal Server Error")
    [failure] = [record for record in logged() if "traceback" in record]
    assert failure["error"] == "basis.api.routes.UndeclaredError: unlisted raised Unlisted, which its route does not declare: add it to the route's errors=[...], or raise one it declares"
    assert str(failure["root_cause"]).endswith("Unlisted: X is not listed")


@pytest.mark.parametrize(
    ("path", "route", "resource"),
    [("/warehouse", "from_the_warehouse", "warehouse"), ("/pricing", "from_pricing", "pricing"), ("/cache", "from_the_cache", "prices")],
    ids=["database", "another-service", "cache"],
)
def test_a_routes_deadline_caps_each_call_its_resources_make(client: TestClient, path: str, route: str, resource: str) -> None:
    """Each resource's own deadline is 1s, and the route's 0.05s: the route's binds."""
    response = client.get(path)
    assert (response.status_code, response.json()["detail"]) == (HTTPStatus.GATEWAY_TIMEOUT, f"{resource} did not answer within what was left of route {route}'s deadline of 0.05s")


def test_a_write_never_commits_once_its_routes_deadline_has_passed(client: TestClient, warehouse: fakes.Database) -> None:
    response = client.post("/dawdle", json={"security": "X", "quantity": 1, "price": "1"})
    assert (response.status_code, response.json()["detail"]) == (
        HTTPStatus.GATEWAY_TIMEOUT,
        "route write_then_dawdle's deadline of 0.1s passed before warehouse's commit, so the request's writes there are rolled back, never committed",
    )
    assert (warehouse.committed, warehouse.rolled_back) == ([], [fakes.Statement(INSERT, {"security": "X"})])


def test_each_error_a_route_answers_with_is_in_the_openapi_document_as_a_problem() -> None:
    given: fakes.Fakes = {WAREHOUSE: fakes.Database(), PRICING: fakes.Http(_pricing())}
    document = _DOCUMENT.validate_python(api.build(SERVICE, settings=api.Settings.given(), fakes=given).asgi.openapi())
    problem = {"application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}}
    assert _at(document, "paths", "/warehouse", "get", "responses") == {
        "200": _at(document, "paths", "/warehouse", "get", "responses", "200"),
        "422": {"description": "invalid-request: The request is not valid", "content": problem},
        "504": {"description": "deadline-exceeded: A call took longer than its deadline", "content": problem},
    }
    assert _at(document, "paths", "/stale/{security}", "get", "responses", "409") == {"description": "stale: The quote is stale", "content": problem}
    schemas = _at(document, "components", "schemas")
    assert isinstance(schemas, dict)
    assert {"Problem", "FieldError"} <= set(schemas)
    assert "HTTPValidationError" not in schemas


_DOCUMENT: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def _at(document: JsonValue, *keys: str) -> JsonValue:
    for key in keys:
        assert isinstance(document, dict)
        document = document[key]
    return document


def test_a_decimal_is_a_string_in_a_root_models_list_too() -> None:
    _, errors = strict.body(TypeAdapter(RootModel[list[Decimal]]), b'["1.10", 2.5]', "application/json")
    assert [(error["loc"], error["type"]) for error in errors] == [(("body", 1), "decimal_string")]
