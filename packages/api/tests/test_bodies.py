"""A request's body as a route reads it: only finite numbers, each key once, no more bytes than the settings allow,
large ones parsed off the event loop, and a caller that leaves mid-upload logged as that, not as a defect."""

import asyncio
import threading
from collections.abc import Callable, Iterator
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, field_validator
from starlette.types import Message

from basis import api


class Leg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rate: float


class Order(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    security: str
    price: float
    legs: tuple[Leg, ...] = ()


_THREADS: list[int] = []


class Noted(BaseModel):
    """A body whose validation notes the thread it runs on."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str

    @field_validator("text")
    @classmethod
    def noted(cls, text: str) -> str:
        _THREADS.append(threading.get_ident())
        return text


def place(order: Order) -> Order:
    return order


async def note(noted: Noted) -> int:
    """Answers the event loop's thread, on which an async handler runs."""
    del noted
    return threading.get_ident()


SERVICE = api.Service(
    name="orders", settings=api.Settings, resources=[], routes=[api.post("/orders", place, deadline_s=1, response=Order, errors=[]), api.post("/notes", note, deadline_s=1, response=int, errors=[])]
)


def _client(**serve: object) -> TestClient:
    return TestClient(api.build(SERVICE, settings=api.Settings.given(serve=serve), fakes={}).asgi)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with _client() as started:
        yield started


@pytest.mark.parametrize(
    ("raw", "pointer", "detail"),
    [
        (b'{"security": "X", "price": NaN}', "#/price", "Input should be a finite number"),
        (b'{"security": "X", "price": Infinity}', "#/price", "Input should be a finite number"),
        (b'{"security": "X", "price": -Infinity}', "#/price", "Input should be a finite number"),
        (b'{"security": "X", "price": 1e999}', "#/price", "Input should be a finite number"),
        (b'{"security": "X", "price": 1, "legs": [{"rate": -1e400}]}', "#/legs/0/rate", "Input should be a finite number"),
        (b'{"security": "X", "price": 1, "security": "Y"}', "#/security", "The key is given twice"),
        (b'{"security": "X", "price": 1, "legs": [{"rate": 1, "rate": 2}]}', "#/legs/0/rate", "The key is given twice"),
    ],
    ids=["nan", "infinity", "minus-infinity", "too-large", "nested-too-large", "repeated-key", "nested-repeated-key"],
)
def test_a_body_takes_only_finite_numbers_and_each_key_once(client: TestClient, raw: bytes, pointer: str, detail: str) -> None:
    response = client.post("/orders", content=raw, headers={"content-type": "application/json"})
    assert response.status_code == HTTPStatus.UNPROCESSABLE_CONTENT
    assert [(error["pointer"], error["detail"].partition(":")[0]) for error in response.json()["errors"]] == [(pointer, detail)]


def test_a_body_of_finite_numbers_is_read_as_before(client: TestClient) -> None:
    sent = {"security": "X", "price": 1e300, "legs": [{"rate": -0.0}, {"rate": 5e-324}]}
    assert client.post("/orders", json=sent).json() == sent


@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
def test_a_body_over_the_settings_limit_answers_413(chunked: bool) -> None:  # noqa: FBT001 -- a parameter of the case
    raw = b'{"security": "' + b"X" * 200 + b'", "price": 1}'
    content: bytes | Iterator[bytes] = iter([raw[:50], raw[50:]]) if chunked else raw
    with _client(max_body_bytes=100) as client:
        response = client.post("/orders", content=content, headers={"content-type": "application/json"})
        within = client.post("/orders", json={"security": "X", "price": 1})
    assert (response.status_code, response.json()["type"]) == (HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body-too-large")
    assert response.json()["detail"] == "The body is over 100 bytes, the most this API takes: send a smaller one"
    assert within.status_code == HTTPStatus.OK


def test_a_route_that_takes_a_body_lists_its_413() -> None:
    document = api.build(SERVICE, settings=api.Settings.given(), fakes={}).asgi.openapi()
    assert document["paths"]["/orders"]["post"]["responses"]["413"]["description"] == "body-too-large: The request body is too large"


def test_a_large_body_is_parsed_off_the_event_loop() -> None:
    _THREADS.clear()
    with _client() as client:
        small = client.post("/notes", json={"text": "x"}).json()
        large = client.post("/notes", json={"text": "x" * 100_000}).json()
    assert small == large  # one event loop
    assert _THREADS[0] == small
    assert _THREADS[1] != large


def test_a_caller_that_leaves_mid_upload_is_logged_as_that(logged: Callable[[], list[dict[str, object]]]) -> None:
    app = api.build(SERVICE, settings=api.Settings.given(), fakes={}).asgi
    sent: list[Message] = []

    async def main() -> None:
        messages = iter([{"type": "http.request", "body": b'{"security": ', "more_body": True}, {"type": "http.disconnect"}])

        async def receive() -> Message:
            return next(messages)

        async def send(message: Message) -> None:
            sent.append(message)

        async with app.router.lifespan_context(app) as state:
            scope = {
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/orders", "raw_path": b"/orders",
                "query_string": b"", "root_path": "", "headers": [(b"content-type", b"application/json")], "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80),
                "state": dict(state or {}),
            }  # fmt: skip
            await app(scope, receive, send)

    asyncio.run(main())
    records = [record for record in logged() if record.get("path") == "/orders"]
    assert [(record["level"], record["message"]) for record in records] == [("info", "POST /orders: the caller left before sending its body")]
    assert not any("traceback" in record for record in logged())
    assert sent == []
