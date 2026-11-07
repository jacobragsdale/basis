"""The resources every part shares, as basis-core opens and fakes them: each call's deadline and span, another
service's replies and the kind of each failure, a GET made again while it is unavailable, and the clock's and the ids'
values. test_http_contract.py checks the client makes the same of a fake service as of one on a socket."""

import asyncio
import json
import time
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

from basis.core import deadlines, errors, fakes, resources
from basis.core.errors import KINDS, BadReply, Conflict, Denied, Failed, NotFound, Rejected, ServiceError, Timeout, Unavailable
from basis.core.opened import SYSTEM_WAITS, HttpClient, NewIds, SystemClock, client
from basis.core.resources import ResourceError
from basis.core.settings import HttpService

type Message = MutableMapping[str, Any]
PRICING = resources.http("pricing", deadline_s=5)
"""Another service, whose 5s a call that answers never needs: a call that must miss a deadline has one of its own."""
AT = datetime(2026, 11, 6, 14, 30, tzinfo=UTC)


class Price(BaseModel):
    security: str
    price: str


def pricing(*, status: int = HTTPStatus.OK, body: object = None, seen: list[Message] | None = None, headers: tuple[tuple[bytes, bytes], ...] = ()) -> fakes.Http:
    """Another service, as the ASGI app its fake takes, which answers every request alike and keeps each one's scope."""

    async def app(scope: Message, receive: Callable[[], Awaitable[Message]], send: Callable[[Message], Awaitable[None]]) -> None:
        await receive()
        if seen is not None:
            seen.append(scope)
        payload = json.dumps(body if body is not None else {"security": "S1", "price": "101.25"}).encode()
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), *headers]})
        await send({"type": "http.response.body", "body": payload})

    return fakes.Http(app)


def spanned() -> tuple[Tracer, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def get(fake: fakes.Http, tracer: Tracer, *, around_s: float | None = None, resource: resources.HttpResource = PRICING) -> Price:
    async def main() -> Price:
        async with fake.client(resource.name) as opened:
            calling = HttpClient(resource, opened, tracer, waits=fake.waits)
            if around_s is None:
                return await calling.get("/prices/S1", Price, query={"on": AT.date(), "live": True})
            with deadlines.within("route", "quote", around_s):
                return await calling.get("/prices/S1", Price)

    return asyncio.run(main())


def post(fake: fakes.Http, tracer: Tracer) -> Price:
    async def main() -> Price:
        async with fake.client(PRICING.name) as opened:
            return await HttpClient(PRICING, opened, tracer, waits=fake.waits).post("/prices/S1", Price, body={"security": "S1"})

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
        {"http.request.method": "GET", "url.path": "/prices/S1", "basis.resource": "pricing", "basis.deadline_s": 5, "http.response.status_code": 200},
    )
    [scope] = seen
    headers = dict(scope["headers"])
    assert scope["query_string"] == b"on=2026-11-06&live=true"
    assert headers[b"traceparent"].decode().split("-")[1:3] == [f"{span.context.trace_id:032x}", f"{span.context.span_id:016x}"]


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (504, Timeout), (502, Unavailable), (503, Unavailable), (429, Unavailable), (404, NotFound), (409, Conflict), (400, Rejected), (422, Rejected),
        (401, Denied), (403, Denied), (500, Failed), (501, Failed), (405, Failed), (408, Failed), (301, Failed),
    ],
)  # fmt: skip
def test_a_reply_that_is_not_a_2xx_is_the_kind_its_status_is(status: int, kind: type[ServiceError]) -> None:
    tracer, exporter = spanned()
    seen: list[Message] = []
    with pytest.raises(kind) as raised:
        post(pricing(status=status, seen=seen), tracer)
    assert type(raised.value) is kind
    assert (raised.value.resource, raised.value.status, raised.value.detail, str(raised.value)) == ("pricing", status, f"pricing answered {status} to POST /prices/S1", raised.value.detail)
    [span] = exporter.get_finished_spans()
    assert (not span.status.is_ok, len(seen)) == (True, 1)


def test_a_reply_that_is_not_the_declared_one_is_a_bad_reply() -> None:
    tracer, exporter = spanned()
    with pytest.raises(BadReply, match="pricing answered GET /prices/S1 with a body that is not a Price") as raised:
        get(pricing(body={"security": "S1"}), tracer)
    assert (raised.value.resource, raised.value.status) == ("pricing", 200)
    [span] = exporter.get_finished_spans()
    assert not span.status.is_ok


def test_a_service_that_cannot_be_reached_is_unavailable_and_gave_no_reply() -> None:
    async def main() -> None:
        async with client("pricing", HttpService.model_validate({"url": "http://127.0.0.1:9/"})) as opened:  # the discard port, which nothing serves
            await HttpClient(PRICING, opened, spanned()[0]).post("/prices/S1", Price)

    with pytest.raises(Unavailable, match=r"^pricing could not be reached for POST /prices/S1: ConnectError$") as raised:
        asyncio.run(main())
    assert raised.value.status is None


@pytest.mark.parametrize(
    ("around_s", "detail"),
    [(None, "pricing did not answer within its deadline of 0.5s"), (0.1, "pricing did not answer within what was left of route quote's deadline of 0.1s")],
    ids=["its-own", "the-one-around-it"],
)
def test_a_call_past_the_shorter_deadline_is_a_timeout(around_s: float | None, detail: str) -> None:
    with pytest.raises(Timeout, match=detail) as raised:
        get(never(), spanned()[0], around_s=around_s, resource=resources.http("pricing", deadline_s=0.5))
    assert (raised.value.resource, raised.value.status) == ("pricing", None)


def test_a_resend_the_deadline_cuts_short_is_still_unavailable() -> None:
    """The first attempt answers 503; the resend never answers, and the deadline stops it: the service is still the
    unavailable one it was, not a Timeout."""
    attempts: list[int] = []

    async def app(scope: Message, receive: Callable[[], Awaitable[Message]], send: Callable[[Message], Awaitable[None]]) -> None:
        del scope
        await receive()
        attempts.append(len(attempts))
        if len(attempts) > 1:
            await asyncio.Event().wait()  # never answers
        await send({"type": "http.response.start", "status": 503, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    fake = fakes.Http(app)
    with pytest.raises(Unavailable, match="pricing answered 503 to GET /prices/S1") as raised:
        get(fake, spanned()[0], resource=resources.http("pricing", deadline_s=1))
    assert (len(attempts), fake.waits.waited, raised.value.status, type(raised.value.__context__)) == (2, [0.05], 503, Timeout)


def never() -> fakes.Http:
    """Another service that never answers, whose call the deadline stops."""

    async def app(scope: Message, receive: Callable[[], Awaitable[Message]], send: Callable[[Message], Awaitable[None]]) -> None:
        del scope, send
        await receive()
        await asyncio.Event().wait()

    return fakes.Http(app)


def test_a_call_with_no_time_left_is_never_made() -> None:
    seen: list[Message] = []
    with pytest.raises(Timeout, match="within what was left of route quote's deadline of 0s"):
        get(pricing(seen=seen), spanned()[0], around_s=0)
    assert seen == []


def test_an_unavailable_get_is_made_again_after_a_backoff_and_its_span_counts_the_resends() -> None:
    tracer, exporter = spanned()
    seen: list[Message] = []
    fake = pricing(seen=seen)
    fake.fails("/prices/S1", Unavailable, times=2)
    assert get(fake, tracer) == Price(security="S1", price="101.25")
    [span] = exporter.get_finished_spans()
    attributes = dict(span.attributes or {})
    assert (len(seen), attributes["http.request.resend_count"], attributes["http.response.status_code"], [event.name for event in span.events]) == (1, 2, 200, ["basis.retry", "basis.retry"])


def test_an_unavailable_get_is_made_again_until_no_backoff_fits_before_its_deadline() -> None:
    """A fake's waits take no time, its clock moves by them alone, and each waits all of its backoff: 50ms, doubling to
    1s, which 5s fits eight of, however long each attempt takes."""
    seen: list[Message] = []
    fake = pricing(status=503, seen=seen)
    started = time.monotonic()
    with pytest.raises(Unavailable, match="pricing answered 503 to GET /prices/S1"):
        get(fake, spanned()[0], resource=resources.http("pricing", deadline_s=5))
    assert (len(seen), fake.waits.waited) == (9, [0.05, 0.1, 0.2, 0.4, 0.8, 1, 1, 1])
    assert time.monotonic() - started < 4.55  # the waits' own seconds, were any really waited


def test_the_systems_waits_jitter_each_backoff_between_half_and_all_of_it() -> None:
    jitters = [SYSTEM_WAITS.jitter() for _ in range(200)]
    assert (min(jitters) >= 0.5, max(jitters) <= 1, len(set(jitters)) > 1) == (True, True, True)


@pytest.mark.parametrize("kind", [kind for kind in KINDS if kind is not Unavailable])
def test_no_other_kind_is_made_again(kind: type[ServiceError]) -> None:
    fake = pricing()
    fake.fails("/prices/S1", kind, times=1)
    with pytest.raises(kind):
        get(fake, spanned()[0])


def test_no_other_method_is_made_again() -> None:
    seen: list[Message] = []
    with pytest.raises(Unavailable):
        post(pricing(status=503, seen=seen), spanned()[0])
    assert len(seen) == 1


def test_a_retry_after_longer_than_what_is_left_of_the_deadline_is_not_waited_for() -> None:
    seen: list[Message] = []
    with pytest.raises(Unavailable):
        get(pricing(status=429, seen=seen, headers=((b"retry-after", b"10"),)), spanned()[0])
    assert len(seen) == 1


def test_a_retry_after_is_waited_for_where_it_is_longer_than_the_backoff() -> None:
    seen: list[Message] = []
    fake = pricing(status=429, seen=seen, headers=((b"retry-after", b"2"),))
    with pytest.raises(Unavailable):
        get(fake, spanned()[0], resource=resources.http("pricing", deadline_s=5))
    assert (len(seen), fake.waits.waited) == (3, [2, 2])


def test_a_service_error_is_one_of_its_kinds_with_its_detail_and_reason() -> None:
    with pytest.raises(TypeError, match="ServiceError is the base of the kinds of a resource's failure, and is never raised itself"):
        ServiceError("pricing failed", resource="pricing")
    found = Failed("database books failed the statement", resource="books", reason="ORA-00900: invalid SQL statement")
    assert (found.detail, found.status, str(found)) == ("database books failed the statement", None, "database books failed the statement: ORA-00900: invalid SQL statement")
    assert [errors.answered(status) for status in (504, 503, 404, 409, 422, 403, 418, 500)] == [Timeout, Unavailable, NotFound, Conflict, Rejected, Denied, Failed, Failed]


@pytest.mark.parametrize(
    ("fails", "message"),
    [
        (
            lambda fake: fake.fails("/prices/S1", ServiceError),
            "a fake service fails with one of Timeout, Unavailable, NotFound, Conflict, Rejected, Denied, BadReply, Failed, the kinds of ServiceError it can raise, not 'ServiceError'",
        ),
        (lambda fake: fake.fails("/prices/S1", ValueError), "not 'ValueError'"),
        (lambda fake: fake.fails("prices/S1", Unavailable), "fails a path of its own, after its base URL"),
        (lambda fake: fake.fails("/prices/S1", Unavailable, times=0), "a fake service fails a whole number of times, 1 or more"),
    ],
    ids=["the-base", "not-a-kind", "no-slash", "no-times"],
)
def test_a_fake_service_fails_a_path_with_a_kind(fails: Callable[[fakes.Http], None], message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        fails(pricing())


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
