"""A number's display format (step 2.13): declared on a field as `Annotated[float, api.format.money("USD")]`, written
into the document as `x-basis-format` and a dataset's Arrow field metadata as `basis.format`, the object agreed with
basis-ui, and checked when it is declared."""

import json
from typing import Annotated

import pandas as pd
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from basis import api
from basis.api import openapi
from basis.core.display import DisplayError

ARROW = "application/vnd.apache.arrow.stream"


class Quote(BaseModel):
    security: str
    clean: Annotated[float, api.format.price32("quarter")]
    notional: Annotated[float, api.format.money("USD")]
    spread: Annotated[float, api.format.bps(1)]
    weight: Annotated[float, api.format.pct()]
    count: Annotated[float, api.format.number(0)]
    plain: float


def quotes() -> api.Table[Quote]:
    return pd.DataFrame({"security": ["UST10Y"], "clean": [98.75], "notional": [1e6], "spread": [0.0125], "weight": [0.5], "count": [3.0], "plain": [1.5]})


SERVICE = api.Service(name="quotes", settings=api.Settings, routes=[api.get("/quotes", quotes, deadline_s=1, response=api.Table[Quote], errors=[])])
FORMATS = {
    "clean": {"kind": "price32", "step": "quarter"},
    "notional": {"kind": "money", "currency": "USD"},
    "spread": {"kind": "bps", "decimals": 1},
    "weight": {"kind": "pct"},
    "count": {"kind": "number", "decimals": 0},
}


def test_each_numbers_format_is_in_the_document_beside_its_type() -> None:
    properties = openapi.mapping(openapi.mapping(openapi.mapping(openapi.mapping(openapi.document(SERVICE).get("components")).get("schemas")).get("Quote")).get("properties"))
    found = {name: openapi.mapping(schema).get("x-basis-format") for name, schema in properties.items()}
    assert found == {"security": None, "plain": None, **FORMATS}
    assert openapi.mapping(properties["notional"]).get("type") == "number"


def test_each_numbers_format_is_in_its_arrow_fields_metadata() -> None:
    with TestClient(api.build(SERVICE, settings=api.Settings.given()).asgi) as client:
        table = pa.ipc.open_stream(client.get("/quotes", headers={"accept": ARROW}).content).read_all()
    found = {field.name: json.loads(field.metadata[b"basis.format"]) if field.metadata else None for field in table.schema}
    assert found == {"security": None, "plain": None, **FORMATS}


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: api.format.money("XXQ"), "'XXQ' is no ISO 4217 currency with minor units"),
        (lambda: api.format.money("usd"), "'usd' is no ISO 4217 currency with minor units"),
        (lambda: api.format.pct(16), "a display format shows 0 to 15 decimals, not 16"),
        (lambda: api.format.price32("sixteenth"), "a price32 format's step is one of"),  # ty: ignore[invalid-argument-type] -- an unknown step, on purpose
    ],
    ids=["an-unknown-currency", "lower-case", "too-many-decimals", "an-unknown-step"],
)
def test_a_format_basis_cannot_show_is_refused_where_it_is_declared(make: object, message: str) -> None:
    with pytest.raises(DisplayError, match=message):
        make()  # ty: ignore[call-non-callable] -- each case's maker


class Labelled(BaseModel):
    label: Annotated[str, api.format.number(2)]


def labelled() -> list[Labelled]:
    return []


def test_a_datasets_format_is_its_numbers_alone() -> None:
    with pytest.raises(api.DeclarationError, match="a dataset's display formats are its numbers', its float columns'"):
        api.get("/labelled", labelled, deadline_s=1, response=list[Labelled], errors=[], dataset=True)
