"""A service's OpenAPI document, as its app serves it and its repo commits it: operation ids from handler names,
strict models, decimals a request gives as strings of their digits, every error as problem details, and a dataset's
Arrow beside its JSON."""

import json
import re
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from http import HTTPStatus
from typing import Annotated

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from basis import api
from basis.api import openapi, strict
from basis.core.sync import MARKER


class Leg(BaseModel):
    """Lax, as pydantic's default is, and in a request and a response alike."""

    rate: Annotated[Decimal, Field(ge=0)]
    note: str | None = None


class Order(BaseModel):
    security: str
    price: Decimal
    limit: Decimal | None = None
    legs: tuple[Leg, ...] = ()
    marks: dict[str, Decimal] = Field(default_factory=dict)


class Placed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: int
    price: Decimal
    legs: tuple[Leg, ...]


class Window(BaseModel):
    model_config = ConfigDict(extra="forbid")

    face: Annotated[Decimal, Field(gt=0)]
    start: date


class Row(BaseModel):
    book: str
    exposure: Annotated[Decimal, Field(max_digits=18, decimal_places=2)]


class Rejected(api.Error):
    status = HTTPStatus.UNPROCESSABLE_CONTENT
    title = "The order is rejected"


class Gone(api.Error):
    status = HTTPStatus.GONE
    title = "The order is gone"


def place_order(order: Order) -> Placed:
    return Placed(order_id=1, price=order.price, legs=order.legs)


def window(given: Annotated[Window, Query()], rate: Decimal) -> Placed:
    del given, rate
    return Placed(order_id=1, price=Decimal(1), legs=())


def exposures() -> list[Row]:
    return []


SERVICE = api.Service(
    name="orders",
    settings=api.Settings,
    routes=[
        api.post("/orders", place_order, deadline_s=1, response=Placed, errors=[Rejected, Gone], status=HTTPStatus.CREATED),
        api.get("/window", window, deadline_s=1, response=Placed, errors=[]),
        api.get("/exposures", exposures, deadline_s=1, response=list[Row], errors=[], dataset=True),
    ],
)
DOCUMENT = openapi.document(SERVICE)
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def at(*keys: str) -> JsonValue:
    found: JsonValue = DOCUMENT
    for key in keys:
        assert isinstance(found, dict), keys
        found = found[key]
    return found


def schemas() -> dict[str, JsonValue]:
    found = at("components", "schemas")
    assert isinstance(found, dict)
    return found


def test_each_operations_id_is_its_handlers_name() -> None:
    assert [at("paths", path, method, "operationId") for path, method in [("/orders", "post"), ("/window", "get"), ("/exposures", "get")]] == ["place_order", "window", "exposures"]


def test_the_paths_carry_no_api_prefix() -> None:
    assert sorted(at("paths")) == ["/exposures", "/orders", "/window"]  # ty: ignore[invalid-argument-type] -- an object's keys


def test_every_error_is_problem_details_and_a_422_names_its_fields() -> None:
    responses = at("paths", "/orders", "post", "responses")
    assert isinstance(responses, dict)
    content = {status: answer["content"] for status, answer in responses.items() if isinstance(answer, dict) and isinstance(answer.get("content"), dict)}
    assert content["410"] == {"application/problem+json": {"schema": {"$ref": "#/components/schemas/Problem"}}}
    assert content["422"] == {"application/problem+json": {"schema": {"$ref": "#/components/schemas/ValidationProblem"}}}
    assert at("paths", "/orders", "post", "responses", "422", "description") == "invalid-request: The request is not valid; rejected: The order is rejected"
    assert at("components", "schemas", "ValidationProblem", "required") == ["type", "title", "status", "errors"]
    assert at("components", "schemas", "PointerError", "required") == ["detail", "pointer"]
    assert at("components", "schemas", "ParameterError", "required") == ["detail", "parameter"]


def test_every_model_is_strict_and_a_mapping_keeps_its_values() -> None:
    models = {name: schema for name, schema in schemas().items() if isinstance(schema, dict) and "properties" in schema}
    assert {name for name, schema in models.items() if schema.get("additionalProperties") is not False} == set()
    assert {"Order", "Leg-Input", "Leg-Output", "Placed", "Row", "Problem", "ValidationProblem"} <= set(models)
    assert at("components", "schemas", "Order", "properties", "marks", "additionalProperties") == openapi.DECIMAL


def _decimals(schema: JsonValue) -> Iterator[JsonValue]:
    """Each schema in `schema` that pydantic would give a decimal by, a number's or a string's."""
    if isinstance(schema, list):
        for item in schema:
            yield from _decimals(item)
    elif isinstance(schema, dict):
        if schema.get("type") == "number":
            yield schema
        yield from (found for value in schema.values() for found in _decimals(value))


def test_a_decimal_a_request_gives_is_a_string_of_its_digits_and_one_a_response_gives_a_string() -> None:
    order = at("components", "schemas", "Order", "properties")
    assert isinstance(order, dict)
    assert order["price"] == {"title": "Price"} | openapi.DECIMAL
    assert order["limit"] == {"anyOf": [openapi.DECIMAL, {"type": "null"}], "title": "Limit"}
    assert at("components", "schemas", "Leg-Input", "properties", "rate") == {"title": "Rate"} | openapi.DECIMAL
    assert at("components", "schemas", "Leg-Output", "properties", "rate") == {"type": "string", "title": "Rate"}
    assert at("components", "schemas", "Placed", "properties", "price") == {"type": "string", "title": "Price"}
    parameters = at("paths", "/window", "get", "parameters")
    assert isinstance(parameters, list)
    by_name = {parameter["name"]: parameter["schema"] for parameter in parameters if isinstance(parameter, dict)}
    assert by_name["face"] == {"title": "Face"} | openapi.DECIMAL
    assert by_name["rate"] == {"title": "Rate"} | openapi.DECIMAL
    assert list(_decimals(DOCUMENT)) == []


def test_the_decimal_pattern_is_the_grammar_basis_reads() -> None:
    pattern = re.compile(str(openapi.DECIMAL["pattern"]))
    accepted = ["0", "100", "-2.50", "0.045", "12345678901234567890.123456789"]
    refused = ["1e3", "+1", " 1", "01", "1.", ".5", "NaN", "1_000"]
    assert [bool(pattern.search(text)) for text in accepted + refused] == [True] * len(accepted) + [False] * len(refused)
    reads = strict.text(Decimal)
    assert reads is not None
    assert [reads.accepts(text) for text in accepted + refused] == [bool(pattern.search(text)) for text in accepted + refused]


def test_a_dataset_lists_arrow_beside_its_json() -> None:
    assert at("paths", "/exposures", "get", "responses", "200", "content") == {
        "application/json": {"schema": {"type": "array", "items": {"$ref": "#/components/schemas/Row"}, "title": "Response Exposures"}},
        "application/vnd.apache.arrow.stream": {"schema": {"type": "string", "format": "binary"}},
    }


def test_the_document_the_app_serves_is_the_one_its_repo_commits() -> None:
    served = api.build(SERVICE, settings=api.Settings.given()).asgi.openapi()
    assert served == DOCUMENT
    committed = openapi.text(SERVICE)
    assert committed.splitlines()[:2] == ["{", f'  "x-basis": "{MARKER}",']
    assert committed.endswith("}\n")
    assert json.loads(committed) == {"x-basis": MARKER, **DOCUMENT}
    assert openapi.text(SERVICE) == committed  # made the same way every time
