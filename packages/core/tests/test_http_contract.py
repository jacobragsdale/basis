"""The contract of another service's client, run against the service as its fake reaches it, in the process, and as a
real server reaches it, uvicorn on 127.0.0.1 serving the same app: each kind of failure, the retries, the deadline and a
reply are the same through both, so a test on the fake sees what production would."""

import asyncio
import json
import socket
import threading
from collections.abc import Awaitable, Callable, Generator, MutableMapping
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx2
import pytest
import uvicorn  # basis-api's server, which the workspace holds: a real HTTP server on a socket
from opentelemetry.sdk.trace import TracerProvider
from pydantic import BaseModel

from basis.core import fakes, resources
from basis.core.errors import KINDS, BadReply, ServiceError, Timeout, Unavailable
from basis.core.opened import HttpClient, client
from basis.core.settings import HttpService

type Message = MutableMapping[str, Any]
type Transport = Literal["fake", "socket"]
PRICING = resources.http("pricing", deadline_s=0.5)
TRANSPORTS: tuple[Transport, ...] = ("fake", "socket")


class Price(BaseModel):
    security: str
    price: str


@dataclass
class Service:
    """The service's app, which counts the requests it answers, and the fake around it, whose failures the test sets."""

    seen: list[str] = field(default_factory=list[str])
    fake: fakes.Http = field(init=False)

    def __post_init__(self) -> None:
        self.fake = fakes.Http(self.app)

    async def app(self, scope: Message, receive: Callable[[], Awaitable[Message]], send: Callable[[Message], Awaitable[None]]) -> None:
        if scope["type"] != "http":
            return
        await receive()
        self.seen.append(f"{scope['method']} {scope['path']}")
        if scope["path"] == "/slow":
            await asyncio.sleep(1)  # past the 0.5s deadline
        body = json.dumps({"security": "S1", "price": "101.25"}).encode()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})


@dataclass
class Served:
    """A server on a socket, serving whichever service the test gives it."""

    port: int
    service: Service | None = None

    async def app(self, scope: Message, receive: Callable[[], Awaitable[Message]], send: Callable[[Message], Awaitable[None]]) -> None:
        if self.service is None:
            msg = "no service is being served"
            raise RuntimeError(msg)
        await self.service.fake.serve(scope, receive, send)


@pytest.fixture(scope="module")
def served() -> Generator[Served]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    found = Served(port)
    server = uvicorn.Server(uvicorn.Config(found.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off", access_log=False, interface="asgi3"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        threading.Event().wait(0.01)
    assert server.started
    yield found
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def service(served: Served) -> Service:
    served.service = Service()
    return served.service


def reaching(transport: Transport, service: Service, served: Served) -> httpx2.AsyncClient:
    if transport == "fake":
        return service.fake.client(PRICING.name)
    return client(PRICING.name, HttpService.model_validate({"url": f"http://127.0.0.1:{served.port}/"}))


def outcome(transport: Transport, service: Service, served: Served, call: Callable[[HttpClient], Awaitable[object]]) -> tuple[object, ...]:
    """What the call made of the service: its reply, or its failure's kind, resource, status and detail; and the
    requests the service's app answered."""

    async def main() -> object:
        async with reaching(transport, service, served) as opened:
            return await call(HttpClient(PRICING, opened, TracerProvider().get_tracer("test")))

    try:
        found: object = asyncio.run(main())
    except ServiceError as error:
        found = (type(error), error.resource, error.status, error.detail)
    return found, tuple(service.seen)


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize("kind", KINDS, ids=[kind.__name__ for kind in KINDS])
def test_each_kind_a_fake_fails_with_is_the_kind_through_either(kind: type[ServiceError], transport: Transport, service: Service, served: Served) -> None:
    service.fake.fails("/prices/S1", kind)
    found = outcome(transport, service, served, lambda calling: calling.post("/prices/S1", Price))
    status = fakes.ANSWERS[kind]
    detail = "pricing answered POST /prices/S1 with a body that is not a Price" if kind is BadReply else f"pricing answered {status} to POST /prices/S1"
    assert found == ((kind, "pricing", status, detail), ())


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_get_unavailable_once_is_made_again_and_answers_through_either(transport: Transport, service: Service, served: Served) -> None:
    service.fake.fails("/prices/S1", Unavailable, times=1)
    found = outcome(transport, service, served, lambda calling: calling.get("/prices/S1", Price))
    assert found == (Price(security="S1", price="101.25"), ("GET /prices/S1",))


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_post_unavailable_once_is_not_made_again_through_either(transport: Transport, service: Service, served: Served) -> None:
    service.fake.fails("/prices/S1", Unavailable, times=1)
    found = outcome(transport, service, served, lambda calling: calling.post("/prices/S1", Price))
    assert found == ((Unavailable, "pricing", 503, "pricing answered 503 to POST /prices/S1"), ())


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_call_past_its_deadline_is_a_timeout_through_either(transport: Transport, service: Service, served: Served) -> None:
    found = outcome(transport, service, served, lambda calling: calling.get("/slow", Price))
    assert found == ((Timeout, "pricing", None, "pricing did not answer within its deadline of 0.5s"), ("GET /slow",))


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_reply_through_either_is_the_same(transport: Transport, service: Service, served: Served) -> None:
    found = outcome(transport, service, served, lambda calling: calling.get("/prices/S1", Price))
    assert found == (Price(security="S1", price="101.25"), ("GET /prices/S1",))


def test_a_server_nothing_listens_for_is_unavailable(service: Service) -> None:
    """What a fake that fails with `Unavailable` stands for, besides a 502, a 503 or a 429: a refused connection, which
    a GET makes again too, until no backoff fits before the deadline."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # closed again, so nothing listens on it

    async def main() -> None:
        async with client(PRICING.name, HttpService.model_validate({"url": f"http://127.0.0.1:{port}/"})) as opened:
            await HttpClient(PRICING, opened, TracerProvider().get_tracer("test")).get("/prices/S1", Price)

    with pytest.raises(Unavailable, match=r"^pricing could not be reached for GET /prices/S1: ConnectError$") as raised:
        asyncio.run(main())
    assert (raised.value.status, service.seen) == (None, [])
