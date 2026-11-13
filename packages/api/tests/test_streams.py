"""A route that streams live updates, as server-sent events (`streams.py`): declared with `api.stream`, its handler an
ordinary GET's, called again every `every_s` within its deadline, each call in a transaction of its own, an event sent
whenever its answer changes, a heartbeat while it does not, and the stream ended, with the reason, when the token
expires, when `serve.stream_s` has passed, or when the app drains; a reader too slow to take an event within
`serve.heartbeat_s` loses its stream. A `Response`, which basis cannot check, is refused where a route declares one,
and where a handler returns one."""

import asyncio
import json
from collections.abc import AsyncGenerator, Mapping
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Annotated

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, JsonValue
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.types import Message

from basis import api
from basis.api import contract, fakes, openapi, streams, tokens
from basis.api.identity import Identity


class Price(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    security: str
    clean: float


class Unlisted(api.Error):
    status = HTTPStatus.NOT_FOUND
    title = "No such security"


WAREHOUSE = api.database("warehouse", deadline_s=1, max_concurrent=2)
PRICE = "SELECT clean FROM prices WHERE security = %(security)s"
IDENTITY = {"issuer": "https://sign-in.example", "audience": "quotes", "algorithms": ["ES256"], "leeway_s": 0, "keys": {"kind": "development"}}


class Feed:
    """The prices the fake warehouse answers, one per statement, the last again once they run out."""

    def __init__(self, *prices: float | None) -> None:
        self.prices: list[float | None] = list(prices)
        self.asked = 0

    def __call__(self, binds: Mapping[str, api.Bind]) -> list[Mapping[str, object]]:
        found = self.prices[min(self.asked, len(self.prices) - 1)]
        self.asked += 1
        if found is None:
            return []
        return [{"clean": found, "security": binds["security"]}]


class Clean(BaseModel):
    clean: float


async def price(security: str, warehouse: Annotated[api.Reader, api.given(WAREHOUSE)]) -> Price:
    found = await warehouse.fetch_one(Clean, PRICE, security=security)
    if found is None:
        msg = f"no price for {security}"
        raise Unlisted(msg)
    return Price(security=security, clean=found.clean)


def _service(every_s: float = 0.02, *, user: type[api.User] | None = None) -> api.Service:
    return api.Service(
        name="quotes", settings=api.Settings, resources=[WAREHOUSE], user=user, routes=[api.stream("/prices/{security}", price, deadline_s=1, every_s=every_s, response=Price, errors=[Unlisted])]
    )


def _settings(stream_s: float = 0.3, heartbeat_s: float = 15, **values: object) -> api.Settings:
    return api.Settings.given(serve={"stream_s": stream_s, "heartbeat_s": heartbeat_s}, **values)


def _events(body: str) -> list[tuple[str | None, JsonValue | None]]:
    """Each message of an event stream, by its name and its data's JSON: a heartbeat as ("heartbeat", None), and the
    reconnection delay as ("retry", its milliseconds)."""
    found: list[tuple[str | None, JsonValue | None]] = []
    for message in body.split("\n\n"):
        if not message.strip():
            continue
        if message.strip().startswith(":"):
            found.append(("heartbeat", None))
            continue
        fields = dict(line.split(": ", 1) for line in message.split("\n") if line)
        if "retry" in fields:
            found.append(("retry", int(fields["retry"])))
        if "data" in fields:
            found.append((fields.get("event"), json.loads(fields["data"])))
    return found


@pytest.fixture
def warehouse() -> tuple[fakes.Database, Feed]:
    feed = Feed(101.25, 101.25, 101.5)
    return fakes.Database(answers={PRICE: fakes.Respond(feed)}), feed


def test_a_stream_sends_the_first_answer_then_each_change_and_ends_at_its_limit(warehouse: tuple[fakes.Database, Feed]) -> None:
    database, feed = warehouse
    with TestClient(api.build(_service(), settings=_settings(), fakes={WAREHOUSE: database}).asgi) as client:
        response = client.get("/prices/US0001")
    assert response.status_code == HTTPStatus.OK
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert "content-encoding" not in response.headers
    assert "content-length" not in response.headers  # a server would refuse every event past a length of 0
    found = [entry for entry in _events(response.text) if entry[0] != "heartbeat"]
    assert found == [("retry", streams.RETRY_MS), (None, {"security": "US0001", "clean": 101.25}), (None, {"security": "US0001", "clean": 101.5}), ("end", {"reason": "limit"})]
    assert feed.asked > 3  # polled until the limit, the unchanged answers sent no event
    assert database.committed == []  # a GET's transactions, one per call, roll back


def test_a_stream_whose_first_answer_fails_answers_its_problem_with_its_status(warehouse: tuple[fakes.Database, Feed]) -> None:
    database, feed = warehouse
    feed.prices = [None]
    with TestClient(api.build(_service(), settings=_settings(), fakes={WAREHOUSE: database}).asgi) as client:
        response = client.get("/prices/US0001")
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "unlisted"


def test_a_stream_whose_later_answer_fails_sends_its_problem_as_an_event_and_ends(warehouse: tuple[fakes.Database, Feed]) -> None:
    database, feed = warehouse
    feed.prices = [101.25, None]
    with TestClient(api.build(_service(), settings=_settings(stream_s=5), fakes={WAREHOUSE: database}).asgi) as client:
        response = client.get("/prices/US0001")
    found = _events(response.text)
    assert found[1] == (None, {"security": "US0001", "clean": 101.25})
    name, problem = found[-1]
    assert name == "problem"
    assert isinstance(problem, dict)
    assert (problem["type"], problem["status"]) == ("unlisted", HTTPStatus.NOT_FOUND)


def test_a_failure_of_what_feeds_a_stream_is_its_kind_of_problem(warehouse: tuple[fakes.Database, Feed]) -> None:
    database, feed = warehouse
    feed.prices = [101.25]
    with TestClient(api.build(_service(every_s=0.05), settings=_settings(stream_s=5), fakes={WAREHOUSE: database}).asgi) as client, client.stream("GET", "/prices/US0001") as response:
        lines = response.iter_lines()
        assert next(lines) == f"retry: {streams.RETRY_MS}"
        database.fails(api.Unavailable)
        rest = "\n".join(lines)
    name, problem = _events(rest)[-1]
    assert name == "problem"
    assert isinstance(problem, dict)
    assert (problem["type"], problem["status"]) == ("service-unavailable", HTTPStatus.SERVICE_UNAVAILABLE)


def test_a_stream_with_nothing_new_sends_a_heartbeat(warehouse: tuple[fakes.Database, Feed]) -> None:
    database, feed = warehouse
    feed.prices = [101.25]
    with TestClient(api.build(_service(), settings=_settings(stream_s=0.5, heartbeat_s=0.1), fakes={WAREHOUSE: database}).asgi) as client:
        response = client.get("/prices/US0001")
    found = _events(response.text)
    assert found.count(("heartbeat", None)) >= 2
    assert found[-1] == ("end", {"reason": "limit"})
    assert [entry for entry in found if entry[0] is None] == [(None, {"security": "US0001", "clean": 101.25})]


def test_a_stream_ends_when_its_token_expires(warehouse: tuple[fakes.Database, Feed]) -> None:
    """The token's `exp` has passed, within the identity's leeway, so it verifies however long the runner takes to
    send the request, and the stream, which ends at `exp` and gives no leeway, ends at once, after its first answer."""
    database, _ = warehouse
    identity = IDENTITY | {"leeway_s": 300}
    now = datetime.now(UTC)
    token = tokens.sign(Identity.model_validate(identity), "trader-1", issued=now - timedelta(seconds=2), expires=now - timedelta(seconds=1))
    app = api.build(_service(every_s=0.1, user=api.User), settings=_settings(stream_s=30, identity=identity), fakes={WAREHOUSE: database})
    with TestClient(app.asgi) as client:
        response = client.get("/prices/US0001", headers={"authorization": f"Bearer {token}"})
    found = _events(response.text)
    assert found[-1] == ("end", {"reason": "expired"})
    assert (None, {"security": "US0001", "clean": 101.25}) in found


def test_a_stream_ends_when_the_app_drains(warehouse: tuple[fakes.Database, Feed]) -> None:
    database, _ = warehouse
    app = api.build(_service(every_s=0.05), settings=_settings(stream_s=30), fakes={WAREHOUSE: database})
    with TestClient(app.asgi) as client, client.stream("GET", "/prices/US0001") as response:
        lines = response.iter_lines()
        assert next(lines) == f"retry: {streams.RETRY_MS}"
        app.lifecycle.drain()
        rest = "\n".join(lines)
    assert _events(rest)[-1] == ("end", {"reason": "shutdown"})


def test_a_reader_too_slow_to_take_a_message_loses_its_stream() -> None:
    sent: list[Message] = []

    async def chunks() -> AsyncGenerator[bytes]:
        yield b"data: 1\n\n"
        yield b"data: 2\n\n"

    async def receive() -> Message:
        await asyncio.Event().wait()  # the reader never leaves
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message.get("body") == b"data: 2\n\n":
            await asyncio.Event().wait()  # its buffer is full, and stays full
        sent.append(message)

    async def run() -> None:
        await asyncio.wait_for(streams.Events(chunks(), send_s=0.05)({"type": "http"}, receive, send), timeout=2)

    asyncio.run(run())
    assert [message.get("body") for message in sent if message["type"] == "http.response.body"] == [b"data: 1\n\n"]


def test_a_stream_is_declared_by_what_it_sends() -> None:
    route = api.stream("/prices/{security}", price, deadline_s=1, every_s=2, response=Price, errors=[Unlisted])
    assert (route.method, route.every_s) == ("GET", 2)
    with pytest.raises(api.DeclarationError, match="every_s"):
        api.stream("/prices/{security}", price, deadline_s=1, every_s=0, response=Price, errors=[Unlisted])

    async def prices(security: str) -> list[Price]:
        return [Price(security=security, clean=1.0)]

    with pytest.raises(api.DeclarationError, match="a pydantic model"):
        api.stream("/prices/{security}", prices, deadline_s=1, every_s=2, response=list[Price], errors=[])

    def blocking(security: str) -> Price:
        return Price(security=security, clean=1.0)

    with pytest.raises(api.DeclarationError, match="async def blocking"):
        api.stream("/prices/{security}", blocking, deadline_s=1, every_s=2, response=Price, errors=[])


@pytest.mark.parametrize("kind", [Response, StreamingResponse, JSONResponse])
def test_a_route_that_declares_a_response_is_refused(kind: type[Response]) -> None:
    def answer() -> Response:
        return Response()

    answer.__annotations__["return"] = kind
    with pytest.raises(api.DeclarationError, match=r"api\.stream"):
        api.get("/answer", answer, deadline_s=1, response=kind, errors=[])


def test_a_handler_that_returns_a_response_is_a_defect_whose_writes_roll_back() -> None:
    insert = "INSERT INTO prices (security) VALUES (%(security)s)"

    async def record(security: str, warehouse: Annotated[api.Writer, api.given(WAREHOUSE)]) -> Price:
        await warehouse.execute(insert, security=security)
        return JSONResponse({"security": security})  # ty: ignore[invalid-return-type] -- the defect under test

    service = api.Service(name="quotes", settings=api.Settings, resources=[WAREHOUSE], routes=[api.post("/prices/{security}", record, deadline_s=1, response=Price, errors=[], anonymous=True)])
    database = fakes.Database(answers={insert: 1})
    with TestClient(api.build(service, settings=api.Settings.given(), fakes={WAREHOUSE: database}).asgi, raise_server_exceptions=False) as client:
        response = client.post("/prices/US0001")
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert database.committed == []
    assert [statement.sql for statement in database.rolled_back] == [insert]


def test_the_document_gives_a_stream_its_events_schema() -> None:
    document = openapi.document(_service())
    operation = openapi.mapping(openapi.mapping(document["paths"])["/prices/{security}"])["get"]
    content = openapi.mapping(openapi.mapping(openapi.mapping(openapi.mapping(operation)["responses"])["200"])["content"])
    assert list(content) == ["text/event-stream"]
    assert content["text/event-stream"] == {"schema": {"type": "string"}, "x-basis-event": {"schema": {"$ref": "#/components/schemas/Price"}}}
    assert "Price" in openapi.mapping(openapi.mapping(document["components"])["schemas"])


def test_a_route_that_starts_or_stops_streaming_breaks_a_ui() -> None:
    streamed = openapi.document(_service())

    async def once(security: str, warehouse: Annotated[api.Reader, api.given(WAREHOUSE)]) -> Price:
        return await price(security, warehouse)

    once.__name__ = "price"
    answered = openapi.document(api.Service(name="quotes", settings=api.Settings, resources=[WAREHOUSE], routes=[api.get("/prices/{security}", once, deadline_s=1, response=Price, errors=[Unlisted])]))
    assert any("no longer streams" in change for change in contract.compare(streamed, answered).breaking)
    assert any("streams" in change for change in contract.compare(answered, streamed).breaking)
    assert contract.compare(streamed, streamed).breaking == []
