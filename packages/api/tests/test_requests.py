import asyncio
import json
import logging
import re
from collections.abc import Callable
from http import HTTPStatus
from typing import Annotated
from uuid import UUID

import httpx2
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from basis import api

type Records = Callable[[], list[dict[str, object]]]  # the `logged` fixture's

log = logging.getLogger("basis.api.tests")

TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
CALLER_SPAN = "00f067aa0ba902b7"


class Unpriced(api.Error):
    status = HTTPStatus.NOT_FOUND
    title = "No price for the security"


def price(security: str, settings: Annotated[api.Settings, api.given(api.Settings)]) -> dict[str, str]:
    log.info("pricing %s", security, extra={"security": security, "port": settings.serve.port})
    if security == "UNKNOWN":
        msg = f"no price for {security}"
        raise Unpriced(msg)
    return {"security": security, "price": "101.25"}


class Order(BaseModel):
    security: str
    quantity: int


def order(placed: Order) -> Order:
    return placed


def conflict() -> dict[str, str]:
    raise HTTPException(HTTPStatus.CONFLICT, detail={"conflicts": ["T1", "T2"]})


def broken() -> dict[str, str]:
    log.info("about to fail")
    msg = "the database password is hunter2"
    raise RuntimeError(msg)


SERVICE = api.Service(
    name="prices",
    settings=api.Settings,
    routes=[
        api.get("/prices/{security}", price, deadline_s=1, response=dict[str, str], errors=[Unpriced]),
        api.post("/orders", order, deadline_s=1, response=Order, errors=[], anonymous=True),
        api.get("/conflict", conflict, deadline_s=1, response=dict[str, str], errors=[]),
        api.get("/broken", broken, deadline_s=1, response=dict[str, str], errors=[]),
    ],
)


def _app(**settings: object) -> api.App:
    return api.build(SERVICE, settings=api.Settings.given(**settings))


def _of(records: list[dict[str, object]], request_id: str) -> list[dict[str, object]]:
    return [record for record in records if record.get("request_id") == request_id]


def test_every_log_line_of_a_request_carries_its_id(logged: Records) -> None:
    with TestClient(_app().asgi) as client:
        first, second = client.get("/prices/US912810TM64"), client.get("/prices/US912810TM64")
    request_id = first.headers["x-request-id"]
    assert UUID(request_id).version == 7
    assert request_id != second.headers["x-request-id"]
    assert [record["message"] for record in _of(logged(), request_id)] == ["pricing US912810TM64", "GET /prices/US912810TM64: 200"]


def test_two_requests_served_at_once_log_their_own_ids(logged: Records) -> None:
    """Each handler logs, waits until both requests are being served, and logs again while the other one still is."""

    async def main() -> list[httpx2.Response]:
        inside = {name: asyncio.Event() for name in "ab"}
        logged_after = {name: asyncio.Event() for name in "ab"}

        async def overlap(name: str) -> dict[str, str]:
            other = "b" if name == "a" else "a"
            log.info("before %s", name)
            inside[name].set()
            await inside[other].wait()
            if name == "b":
                await logged_after["a"].wait()
            log.info("after %s", name)
            logged_after[name].set()
            await logged_after[other].wait()
            return {"name": name}

        route = api.get("/overlap/{name}", overlap, deadline_s=5, response=dict[str, str], errors=[])
        app = api.build(api.Service(name="overlap", settings=api.Settings, routes=[route]), settings=api.Settings.given())
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app.asgi), base_url="http://test") as client:
            return list(await asyncio.gather(client.get("/overlap/a"), client.get("/overlap/b")))

    responses = asyncio.run(main())
    records = logged()
    for response, name in zip(responses, "ab", strict=True):
        assert [record["message"] for record in _of(records, response.headers["x-request-id"])] == [f"before {name}", f"after {name}", f"GET /overlap/{name}: 200"]


def test_a_callers_traceparent_continues_its_trace(logged: Records) -> None:
    with TestClient(_app().asgi) as client:
        response = client.get("/prices/US912810TM64", headers={"traceparent": f"00-{TRACE}-{CALLER_SPAN}-01"})
    version, trace, span, _ = response.headers["traceparent"].split("-")
    assert (version, trace) == ("00", TRACE)
    assert span != CALLER_SPAN
    assert {(record["trace_id"], record["span_id"]) for record in _of(logged(), response.headers["x-request-id"])} == {(TRACE, span)}


def test_a_request_without_a_traceparent_starts_its_own_trace() -> None:
    with TestClient(_app().asgi) as client:
        first, second = client.get("/prices/US912810TM64"), client.get("/prices/US912810TM64")
    traces = {response.headers["traceparent"].split("-")[1] for response in (first, second)}
    assert len(traces) == 2
    assert "0" * 32 not in traces


def test_spans_go_only_where_the_settings_say(capsys: pytest.CaptureFixture[str]) -> None:
    with TestClient(_app().asgi) as client:
        client.get("/prices/US912810TM64")
    out, err = capsys.readouterr()
    assert (out, "SpanKind" in err) == ("", False)
    with TestClient(_app(traces={"exporter": "console"}).asgi) as client:
        client.get("/health/ready")
        response = client.get("/prices/US912810TM64", headers={"traceparent": f"00-{TRACE}-{CALLER_SPAN}-01"})
    out, err = capsys.readouterr()  # stderr, as stdout carries a command's results
    server = [span for span in _spans(err) if span["kind"] == "SpanKind.SERVER"]
    assert out == ""
    assert [(span["name"], span["parent_id"]) for span in server] == [("GET /prices/{security}", f"0x{CALLER_SPAN}")]
    context = server[0]["context"]
    assert isinstance(context, dict)
    assert {str(key): value for key, value in context.items()}["span_id"] == f"0x{response.headers['traceparent'].split('-')[2]}"


def test_a_declared_error_is_a_problem_and_an_ordinary_log_line(logged: Records) -> None:
    with TestClient(_app().asgi) as client:
        response = client.get("/prices/UNKNOWN")
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json() == {"type": "unpriced", "title": "No price for the security", "status": 404, "detail": "no price for UNKNOWN", "instance": f"urn:uuid:{response.headers['x-request-id']}"}
    assert [(record["level"], record["status"]) for record in _of(logged(), response.headers["x-request-id"]) if "status" in record] == [("info", 404)]


def test_an_unexpected_error_is_a_bare_500_logged_once_with_its_traceback(logged: Records) -> None:
    with TestClient(_app().asgi) as client:
        response = client.get("/broken")
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert response.json() == {"type": "about:blank", "title": "Internal Server Error", "status": 500, "instance": f"urn:uuid:{response.headers['x-request-id']}"}
    records = _of(logged(), response.headers["x-request-id"])
    assert [record["message"] for record in records] == ["about to fail", "GET /broken failed"]
    failure = records[1]
    assert (failure["level"], failure["status"], failure["route"]) == ("error", 500, "/broken")
    assert failure["error"] == "RuntimeError: the database password is hunter2"
    assert "raise RuntimeError(msg)" in str(failure["traceback"])
    assert sum("traceback" in record for record in logged()) == 1


def test_a_body_that_is_not_json_is_a_problem_with_the_whole_body() -> None:
    with TestClient(_app().asgi) as client:
        response = client.post("/orders", content=b'{"security": "T1",', headers={"content-type": "application/json"})
    assert (response.status_code, response.json()["type"]) == (HTTPStatus.UNPROCESSABLE_CONTENT, "invalid-request")
    [error] = response.json()["errors"]
    assert error["pointer"] == "#"
    assert error["detail"].startswith("The body is not JSON: EOF while parsing a value at line 1 column 18")


def test_an_http_exception_in_a_route_is_a_500_whose_log_says_to_raise_a_declared_error(logged: Records) -> None:
    with TestClient(_app().asgi) as client:
        response = client.get("/conflict")
    assert response.json() == {"type": "about:blank", "title": "Internal Server Error", "status": 500, "instance": f"urn:uuid:{response.headers['x-request-id']}"}
    [failure] = [record for record in _of(logged(), response.headers["x-request-id"]) if "traceback" in record]
    assert str(failure["error"]).startswith("basis.api.routes.UndeclaredError: conflict raised an HTTPException 409, which no route declares: raise an api.Error subclass")


@pytest.mark.parametrize(
    ("method", "path", "status", "headers"), [("GET", "/nowhere", HTTPStatus.NOT_FOUND, {}), ("POST", "/broken", HTTPStatus.METHOD_NOT_ALLOWED, {"allow": "GET"})], ids=["no-route", "wrong-method"]
)
def test_errors_from_routing_are_problems_too(method: str, path: str, status: int, headers: dict[str, str]) -> None:
    with TestClient(_app().asgi) as client:
        response = client.request(method, path)
    assert (response.status_code, response.headers["content-type"], response.json()["type"]) == (status, "application/problem+json", "about:blank")
    assert headers.items() <= response.headers.items()


def _spans(err: str) -> list[dict[str, object]]:
    """ConsoleSpanExporter's output on stderr: one indented JSON object per span, each starting on a line of its own
    with `{`. Anything else on stderr, such as a record a handler another test left behind could not write, is not a
    span."""
    decoder = json.JSONDecoder()
    spans: list[dict[str, object]] = []
    for found in re.finditer(r"^\{$", err, flags=re.MULTILINE):
        span, _ = decoder.raw_decode(err, found.start())
        assert isinstance(span, dict)
        spans.append({str(key): value for key, value in span.items()})
    return spans
