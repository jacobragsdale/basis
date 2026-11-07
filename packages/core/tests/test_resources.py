"""The resources every part shares, as basis-core opens and fakes them: each call's deadline and span, another
service's replies and failures, and the clock's and the ids' values."""

import asyncio
import json
from collections.abc import Awaitable, Callable, MutableMapping
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any
from uuid import UUID

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Tracer
from pydantic import BaseModel

from basis.core import deadlines, fakes, resources
from basis.core.opened import HttpClient, NewIds, SystemClock, client
from basis.core.resources import CallFailedError, DeadlineExceededError, ResourceError
from basis.core.settings import HttpService

type Message = MutableMapping[str, Any]
PRICING = resources.http("pricing", deadline_s=0.5)
AT = datetime(2026, 11, 6, 14, 30, tzinfo=UTC)


class Price(BaseModel):
    security: str
    price: str


def pricing(*, status: int = HTTPStatus.OK, body: object = None, wait_s: float = 0, seen: list[Message] | None = None) -> fakes.Http:
    """Another service, as the ASGI app its fake takes, which answers every request alike and keeps each one's scope."""

    async def app(scope: Message, receive: Callable[[], Awaitable[Message]], send: Callable[[Message], Awaitable[None]]) -> None:
        await receive()
        if seen is not None:
            seen.append(scope)
        await asyncio.sleep(wait_s)
        payload = json.dumps(body if body is not None else {"security": "S1", "price": "101.25"}).encode()
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": payload})

    return fakes.Http(app)


def spanned() -> tuple[Tracer, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def get(fake: fakes.Http, tracer: Tracer, *, around_s: float | None = None) -> Price:
    async def main() -> Price:
        async with fake.client(PRICING.name) as opened:
            calling = HttpClient(PRICING, opened, tracer)
            if around_s is None:
                return await calling.get("/prices/S1", Price, query={"on": AT.date(), "live": True})
            with deadlines.within("route", "quote", around_s):
                return await calling.get("/prices/S1", Price)

    return asyncio.run(main())


@pytest.mark.parametrize(
    ("declare", "message"),
    [
        (lambda: resources.http("pricing-service", deadline_s=1), "http 'pricing-service': a resource's name is lower-case letters, digits and underscores"),
        (lambda: resources.http("pricing", deadline_s=0), "pricing's deadline is 0s: give it deadline_s, the seconds each call may take, above 0"),
        (lambda: resources.http("pricing", deadline_s=float("nan")), "give it deadline_s"),
        (lambda: resources.http("pricing", deadline_s=True), "give it deadline_s"),
        (lambda: resources.clock("Clock"), "clock 'Clock': a resource's name"),
        (lambda: resources.ids("1ds"), "ids '1ds': a resource's name"),
    ],
    ids=["hyphen", "no-deadline", "nan-deadline", "bool-deadline", "upper-case", "leading-digit"],
)
def test_a_resource_is_declared_with_a_name_and_a_deadline(declare: Callable[[], object], message: str) -> None:
    with pytest.raises(ResourceError, match=message):
        declare()


def test_a_call_validates_its_reply_and_has_a_span_that_the_other_service_continues() -> None:
    tracer, exporter = spanned()
    seen: list[Message] = []
    assert get(pricing(seen=seen), tracer) == Price(security="S1", price="101.25")
    [span] = exporter.get_finished_spans()
    assert (span.name, dict(span.attributes or {})) == (
        "pricing GET",
        {"http.request.method": "GET", "url.path": "/prices/S1", "basis.resource": "pricing", "basis.deadline_s": 0.5, "http.response.status_code": 200},
    )
    [scope] = seen
    headers = dict(scope["headers"])
    assert scope["query_string"] == b"on=2026-11-06&live=true"
    assert headers[b"traceparent"].decode().split("-")[1:3] == [f"{span.context.trace_id:032x}", f"{span.context.span_id:016x}"]


@pytest.mark.parametrize(
    ("fake", "detail", "reply"),
    [
        (pricing(status=HTTPStatus.SERVICE_UNAVAILABLE), "pricing answered 503 to GET /prices/S1", 503),
        (pricing(body={"security": "S1"}), "pricing answered GET /prices/S1 with a body that is not a Price", 200),
    ],
    ids=["not-a-2xx", "not-the-reply"],
)
def test_another_services_failure_is_a_call_failed_error(fake: fakes.Http, detail: str, reply: int) -> None:
    tracer, exporter = spanned()
    with pytest.raises(CallFailedError, match=detail) as raised:
        get(fake, tracer)
    assert (raised.value.resource, raised.value.reply) == ("pricing", reply)
    [span] = exporter.get_finished_spans()
    assert not span.status.is_ok


def test_a_service_that_cannot_be_reached_gave_no_reply() -> None:
    async def main() -> None:
        async with client("pricing", HttpService.model_validate({"url": "http://127.0.0.1:9/"})) as opened:  # the discard port, which nothing serves
            await HttpClient(PRICING, opened, spanned()[0]).get("/prices/S1", Price)

    with pytest.raises(CallFailedError, match=r"pricing could not be reached for GET /prices/S1: ConnectError") as raised:
        asyncio.run(main())
    assert raised.value.reply is None


@pytest.mark.parametrize(
    ("around_s", "detail", "deadline_s"),
    [(None, "pricing did not answer within its deadline of 0.5s", 0.5), (0.1, "pricing did not answer within what was left of route quote's deadline of 0.1s", 0.1)],
    ids=["its-own", "the-one-around-it"],
)
def test_a_call_past_the_shorter_deadline_is_a_deadline_exceeded_error(around_s: float | None, detail: str, deadline_s: float) -> None:
    with pytest.raises(DeadlineExceededError, match=detail) as raised:
        get(pricing(wait_s=5), spanned()[0], around_s=around_s)
    assert (raised.value.resource, raised.value.deadline_s) == ("pricing", deadline_s)


def test_a_call_with_no_time_left_is_never_made() -> None:
    seen: list[Message] = []
    with pytest.raises(DeadlineExceededError, match="within what was left of route quote's deadline of 0s"):
        get(pricing(seen=seen), spanned()[0], around_s=0)
    assert seen == []


def test_a_client_reads_nothing_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("SSL_CERT_FILE", "/nowhere.pem")
    opened = client("pricing", HttpService.model_validate({"url": "https://pricing.example/api"}))
    assert (opened.trust_env, str(opened.base_url)) == (False, "https://pricing.example/api/")


def test_the_clocks_and_the_ids_spans_record_each_value() -> None:
    tracer, exporter = spanned()
    clock = SystemClock(resources.clock("clock"), fakes.Clock(at=AT).now, tracer)
    ids = NewIds(resources.ids("ids"), fakes.Ids().new, tracer)
    assert (clock.now(), ids.new(), ids.many(2)) == (AT, UUID("00000000-0000-7000-8000-000000000001"), [UUID(f"00000000-0000-7000-8000-00000000000{n}") for n in (2, 3)])
    assert [(span.name, dict(span.attributes or {})) for span in exporter.get_finished_spans()] == [
        ("clock now", {"basis.resource": "clock", "basis.value": "2026-11-06T14:30:00+00:00"}),
        ("ids new", {"basis.resource": "ids", "basis.value": "00000000-0000-7000-8000-000000000001"}),
        ("ids new", {"basis.resource": "ids", "basis.count": 2}),
    ]


def test_a_fake_clock_has_a_zone_and_advances() -> None:
    with pytest.raises(ValueError, match="has no time zone"):
        fakes.Clock(at=datetime(2026, 11, 6))
    clock = fakes.Clock(at=AT)
    clock.advance(AT - datetime(2026, 11, 6, tzinfo=UTC))
    assert clock.now() == datetime(2026, 11, 7, 5, 0, tzinfo=UTC)


def test_work_with_only_the_deadline_around_it_needs_one() -> None:
    with pytest.raises(RuntimeError, match="compute solve has no deadline of its own, and runs only within one around it"):
        deadlines.limit("compute solve", None)
    with deadlines.within("route", "quote", 2):
        found = deadlines.limit("compute solve", None)
    assert found.enclosing is not None
    assert (found.own_s, found.enclosing.whose, 0 < found.seconds <= 2) == (None, "route quote", True)
