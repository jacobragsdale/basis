"""A service's OpenAPI document, as its app serves it and its repo commits it: operation ids from handler names,
strict models, every number a JSON number, every error as problem details, and a dataset's Arrow beside its JSON."""

import json
from datetime import date
from http import HTTPStatus
from typing import Annotated

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from basis import api
from basis.api import openapi
from basis.core.sync import MARKER


class Leg(BaseModel):
    """Lax, as pydantic's default is, and in a request and a response alike."""

    rate: Annotated[float, Field(ge=0)]
    note: str | None = None


class Order(BaseModel):
    security: str
    price: float
    limit: Annotated[float, Field(gt=0)] | None = None
    quantity: int = 1
    legs: tuple[Leg, ...] = ()
    marks: dict[str, float] = Field(default_factory=dict)


class Placed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_id: int
    price: float
    legs: tuple[Leg, ...]


class Window(BaseModel):
    model_config = ConfigDict(extra="forbid")

    face: Annotated[float, Field(gt=0)]
    start: date


class Row(BaseModel):
    book: str
    exposure: float


class Rejected(api.Error):
    status = HTTPStatus.UNPROCESSABLE_CONTENT
    title = "The order is rejected"


class Gone(api.Error):
    status = HTTPStatus.GONE
    title = "The order is gone"


def place_order(order: Order) -> Placed:
    return Placed(order_id=1, price=order.price, legs=order.legs)


def window(given: Annotated[Window, Query()], rate: float) -> Placed:
    del given, rate
    return Placed(order_id=1, price=1.0, legs=())


def exposures() -> list[Row]:
    return []


SERVICE = api.Service(
    name="orders",
    settings=api.Settings,
    routes=[
        api.post("/orders", place_order, deadline_s=1, response=Placed, errors=[Rejected, Gone], status=HTTPStatus.CREATED, anonymous=True),
        api.get("/window", window, deadline_s=1, response=Placed, errors=[]),
        api.get("/exposures", exposures, deadline_s=1, response=list[Row], errors=[], dataset=True),
    ],
)
DOCUMENT = openapi.document(SERVICE)


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
    assert at("components", "schemas", "Order", "properties", "marks", "additionalProperties") == {"type": "number"}


def test_every_number_is_a_json_number_and_a_decimal_is_refused() -> None:
    order = at("components", "schemas", "Order", "properties")
    assert isinstance(order, dict)
    assert order["price"] == {"type": "number", "title": "Price"}
    assert order["limit"] == {"anyOf": [{"type": "number", "exclusiveMinimum": 0}, {"type": "null"}], "title": "Limit"}
    assert order["quantity"] == {"type": "integer", "title": "Quantity", "default": 1}
    assert at("components", "schemas", "Placed", "properties", "price") == {"type": ["number", "null"], "title": "Price"}  # NaN and ±inf answer null
    assert "x-basis-decimal" not in json.dumps(DOCUMENT)


def test_a_responses_float_may_be_null_and_its_every_field_is_sent() -> None:
    """NaN and ±inf answer null, and the app sends a field with a default as it sends any other: a request's model of
    the same name keeps what it takes, as its own schema."""
    placed = at("components", "schemas", "Placed", "properties")
    assert isinstance(placed, dict)
    assert placed["price"] == {"type": ["number", "null"], "title": "Price"}
    assert at("components", "schemas", "Leg-Output", "properties", "rate") == {"type": ["number", "null"], "minimum": 0, "title": "Rate"}
    assert at("components", "schemas", "Leg-Output", "required") == ["rate", "note"]
    assert at("components", "schemas", "Leg-Input", "properties", "rate") == {"type": "number", "minimum": 0, "title": "Rate"}
    assert at("components", "schemas", "Leg-Input", "required") == ["rate"]
    assert placed["legs"] == {"type": "array", "items": {"$ref": "#/components/schemas/Leg-Output"}, "title": "Legs"}
    assert at("components", "schemas", "Order", "properties", "legs", "items") == {"$ref": "#/components/schemas/Leg-Input"}
    assert "Leg" not in schemas()
    assert at("components", "schemas", "Row", "properties", "exposure") == {"type": ["number", "null"], "title": "Exposure"}


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
