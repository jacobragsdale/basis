"""Another service's path, as a handler names it: a template whose parameters basis encodes, each as one segment, so a
value a request supplied never reaches another endpoint of the service, its query or a fragment."""

import json
from http import HTTPStatus
from typing import Annotated

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from basis import api
from basis.api import fakes, outboxes
from basis.api.resources import filled

OUTBOX = api.outbox("prices_outbox", max_age_s=3600, backoff_s=60, max_backoff_s=600)
WAREHOUSE = api.database("warehouse", deadline_s=1, max_concurrent=4, outbox=OUTBOX)
PRICING = api.http("pricing", deadline_s=1)
INSERT = "INSERT INTO prices (security) VALUES (%(security)s)"


class Price(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    security: str
    price: float


async def by_template(security: str, pricing: Annotated[api.Client, api.given(PRICING)]) -> Price:
    return await pricing.get("/prices/{security}", Price, segments={"security": security})


async def by_hand(security: str, pricing: Annotated[api.Client, api.given(PRICING)]) -> Price:
    return await pricing.get(f"/prices/{security}", Price)


async def book(price: Price, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)], pricing: Annotated[api.Client, api.given(PRICING)]) -> Price:
    await warehouse.execute(INSERT, security=price.security)
    await pricing.send("PUT", "/prices/{security}", price, key="k", segments={"security": price.security})
    return price


SERVICE = api.Service(
    name="quotes",
    settings=api.Settings,
    resources=[WAREHOUSE, PRICING],
    routes=[
        api.get("/template", by_template, deadline_s=5, response=Price, errors=[]),
        api.get("/hand", by_hand, deadline_s=5, response=Price, errors=[]),
        api.post("/book", book, deadline_s=5, response=Price, errors=[], anonymous=True),
    ],
)


class Pricing:
    """The pricing service, as a fake, which notes the raw path and the query of each call it is sent."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []
        self.app = FastAPI()

        @self.app.api_route("/{anything:path}", methods=["GET", "PUT"])
        async def anything(request: Request) -> Response:
            self.seen.append((request.scope["raw_path"].decode(), request.scope["query_string"].decode()))
            return Response(json.dumps({"security": "X", "price": 1.0}), media_type="application/json")


def _client(pricing: Pricing, warehouse: fakes.Database | None = None) -> TestClient:
    given: fakes.Fakes = {WAREHOUSE: warehouse or fakes.Database(answers={INSERT: 1}), PRICING: fakes.Http(pricing.app)}
    return TestClient(api.build(SERVICE, settings=api.Settings.given(), fakes=given).asgi)


@pytest.mark.parametrize(
    ("security", "path"),
    [("US912810TM64", "/prices/US912810TM64"), ("../admin/wipe", "/prices/..%2Fadmin%2Fwipe"), ("x?force=true#", "/prices/x%3Fforce%3Dtrue%23"), ("a b%2F", "/prices/a%20b%252F")],
    ids=["plain", "another-endpoint", "a-query-and-a-fragment", "a-space-and-an-escape"],
)
def test_a_templates_parameter_is_one_segment_whatever_it_holds(security: str, path: str) -> None:
    pricing = Pricing()
    with _client(pricing) as client:
        response = client.get("/template", params={"security": security})
    assert (response.status_code, pricing.seen) == (HTTPStatus.OK, [(path, "")])


@pytest.mark.parametrize("security", ["../admin/wipe", "x?force=true#", "."], ids=["another-endpoint", "a-query-and-a-fragment", "a-dot-segment"])
def test_a_path_that_would_reach_another_endpoint_is_never_called(security: str) -> None:
    pricing = Pricing()
    with _client(pricing) as client:
        response = client.get("/hand", params={"security": security})
    assert (response.status_code, pricing.seen) == (HTTPStatus.INTERNAL_SERVER_ERROR, [])


def test_a_sent_calls_parameters_are_encoded_in_its_outbox_row() -> None:
    pricing, warehouse = Pricing(), fakes.Database(answers={INSERT: 1})
    with _client(pricing, warehouse) as client:
        response = client.post("/book", json={"security": "../admin", "price": 1.0})
    [row] = warehouse.outbox
    assert (response.status_code, row.path, pricing.seen) == (HTTPStatus.OK, "/prices/..%2Fadmin", [("/prices/..%2Fadmin", "")])


@pytest.mark.parametrize(
    ("template", "segments", "message"),
    [
        ("/prices/{security}", {}, r"names \{security\}, which segments does not give: give segments=\{'security': ...\}"),
        ("/prices", {"security": "X"}, "gives segments the template does not name, security: name each as {security} in the path"),
        ("/prices/{security}", {"security": ""}, "an empty segment"),
        ("/prices/{security}", {"security": ".."}, "a dot segment"),
        ("/prices/{security}", {"security": True}, "give each segment as a str or an int"),
        ("/prices?all=1", None, "holds a query or a fragment: give the query as query="),
        ("/prices#top", None, "holds a query or a fragment"),
        ("/prices/../admin", None, "a dot segment"),
        ("/prices/./x", None, "a dot segment"),
        ("prices", None, "give a path that starts with /"),
    ],
    ids=[
        "a-missing-segment", "an-unused-segment", "an-empty-segment", "a-dot-dot-segment", "a-bool", "a-query", "a-fragment", "a-dot-dot-path", "a-dot-path",
        "no-slash",
    ],
)  # fmt: skip
def test_a_template_basis_cannot_fill_safely_is_refused_with_the_fix(template: str, segments: dict[str, object] | None, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        filled(template, segments)  # ty: ignore[invalid-argument-type] -- each case gives one thing wrong on purpose


def test_the_outbox_holds_no_path_that_reaches_another_endpoint() -> None:
    with pytest.raises(ValueError, match="a dot segment"):
        outboxes.checked("/bookings/../admin", "k")
