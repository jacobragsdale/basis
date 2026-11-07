"""Each resource `basis.core.resources` declares, as a run or an app opens it: another service through an httpx2 client,
the system's clock and new ids, or the fakes a test gives (`basis.core.fakes`). Every call through one gets the same,
real or fake: a span of its own on the run's or the app's tracer and, for a call that can wait, a deadline, its
resource's own or what is left of the one around it (`basis.core.deadlines`), past which it raises `Timeout`.

A call to another service that fails raises the kind of `basis.core.errors.ServiceError` its failure is: a transport's
failure to connect, or a connection reset or closed, is `Unavailable`, and any other is `Failed`; a reply that is not a
2xx is the kind its status is (`errors.answered`); and a body that is not the declared reply is `BadReply`. A GET that
is `Unavailable` is made again, after a backoff that starts at `RETRY_FIRST_S` and doubles to `RETRY_MAX_S` at most,
each wait jittered to between half and all of it, or after the reply's `Retry-After` seconds where that is longer, so
long as the wait ends before the deadline; once none does, or the deadline cuts a resend short, the last `Unavailable`
is raised, not a `Timeout`. The call's span counts the resends (`http.request.resend_count`). No other method, and no
other kind, is made again. How a client waits is a resource of its own (`Waits`): the system's clock and sleep, or a
fake's, which waits no time and moves its clock on by each wait, so a test makes every attempt a deadline allows at
once.

How a part reaches another service is its settings' alone: its URL, CA bundle and proxy, never `SSL_CERT_FILE`,
`HTTP_PROXY` or any other variable of the environment's."""

import asyncio
import logging
import random
import ssl
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

import httpx2
from opentelemetry.trace import Span, SpanKind, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import BaseModel, TypeAdapter, ValidationError
from pydantic_core import to_json

from basis.core import deadlines
from basis.core.errors import BadReply, Failed, ServiceError, Timeout, Unavailable, answered
from basis.core.resources import Body, ClockResource, HttpMethod, HttpResource, IdsResource, Query, QueryValue
from basis.core.settings import HttpService

log = logging.getLogger(__name__)
_PROPAGATOR = TraceContextTextMapPropagator()
RETRY_FIRST_S = 0.05
"""The wait before an `Unavailable` GET is made again the first time, which doubles each time after."""
RETRY_MAX_S = 1.0
"""The longest wait between two attempts at an `Unavailable` GET, but for a longer `Retry-After`."""
_UNREACHABLE = (httpx2.NetworkError, httpx2.RemoteProtocolError, httpx2.ProxyError)
"""A transport's failures to reach the service: to connect, through the proxy too, or a connection reset or closed
before the reply."""


class Waits(Protocol):
    """How a client waits between attempts at a call: the clock its deadline is read on, the wait, and each wait's
    jitter, the share of its backoff it waits."""

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...

    def jitter(self) -> float: ...


class SystemWaits:
    """The system's monotonic clock and sleep, and a jitter from half to all of each backoff, which spreads the
    replicas' attempts."""

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    def jitter(self) -> float:
        return random.uniform(0.5, 1)  # noqa: S311 -- jitter, which spreads the replicas' retries, not a secret


SYSTEM_WAITS = SystemWaits()


def client(name: str, service: HttpService) -> httpx2.AsyncClient:
    """The client of the service the settings name `name`: its certificate checked against the declared CA bundle or
    the operating system's trusted CAs, through the declared proxy or none, and nothing read from the environment."""
    verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=service.ca_bundle) if service.ca_bundle is not None else True
    proxy = str(service.proxy) if service.proxy is not None else None
    log.debug("calls %s at %s", name, service.url, extra={"resource": name, "ca_bundle": str(service.ca_bundle) if service.ca_bundle else None, "proxy": proxy})
    return httpx2.AsyncClient(base_url=str(service.url), verify=verify, proxy=proxy, trust_env=False)


class HttpClient:
    """Another service, as `Client` gives it: every call in a span of its own, within its deadline, waiting between
    attempts as `waits` does."""

    def __init__(self, resource: HttpResource, client: httpx2.AsyncClient, tracer: Tracer, *, waits: Waits = SYSTEM_WAITS) -> None:
        self._resource = resource
        self._client = client
        self._tracer = tracer
        self._waits = waits

    @property
    def name(self) -> str:
        return self._resource.name

    @property
    def deadline_s(self) -> float:
        return self._resource.deadline_s

    async def get[T](self, path: str, reply: type[T], /, *, query: Query | None = None) -> T:
        return await self.call("GET", path, reply, query=query)

    async def post[T](self, path: str, reply: type[T], /, *, query: Query | None = None, body: Body = None) -> T:
        return await self.call("POST", path, reply, query=query, body=body)

    async def call[T](self, method: HttpMethod, path: str, reply: type[T], /, *, query: Query | None = None, body: Body = None) -> T:
        resource = self._resource
        with self._span(method, path, {}) as span:
            response = await self._exchange(span, method, path, query=query, content=content(body), headers={})
            try:
                return adapter(reply).validate_json(response.content)
            except ValidationError as error:
                name = getattr(reply, "__qualname__", str(reply))
                errors = error.errors(include_url=False, include_input=False)
                log.warning("%s answered %s %s with a body that is not a %s", resource.name, method, path, name, extra={"resource": resource.name, "errors": errors})
                msg = f"{resource.name} answered {method} {path} with a body that is not a {name}"
                raise BadReply(msg, resource=resource.name, status=response.status_code) from error

    async def request(  # noqa: PLR0913 -- the request's parts, by keyword
        self, method: HttpMethod, path: str, *, query: Query | None = None, content: bytes | None = None, headers: Mapping[str, str] | None = None, attributes: Mapping[str, str] | None = None
    ) -> httpx2.Response:
        """One call, in a span of its own with `attributes` besides its own, and within its deadline, whose reply must
        be a 2xx: its response, whose body the caller reads itself. `content` is JSON, where there is any."""
        with self._span(method, path, attributes or {}) as span:
            return await self._exchange(span, method, path, query=query, content=content, headers=headers or {})

    @contextmanager
    def _span(self, method: HttpMethod, path: str, attributes: Mapping[str, str]) -> Generator[Span]:
        resource = self._resource
        given = {"http.request.method": method, "url.path": path, "basis.resource": resource.name, "basis.deadline_s": resource.deadline_s} | dict(attributes)
        with self._tracer.start_as_current_span(f"{resource.name} {method}", kind=SpanKind.CLIENT, attributes=given) as span:
            yield span

    async def _exchange(  # noqa: PLR0913 -- the request's parts, by keyword
        self, span: Span, method: HttpMethod, path: str, *, query: Query | None, content: bytes | None, headers: Mapping[str, str]
    ) -> httpx2.Response:
        """The service's 2xx reply to the request, within the deadline, made again while it is an `Unavailable` GET and
        a backoff fits before the deadline."""
        resource = self._resource
        sent = {name.lower(): value for name, value in headers.items()}
        _PROPAGATOR.inject(sent)  # this call's span, as the other service's parent
        if content is not None:
            sent["content-type"] = "application/json"
        limit = deadlines.limit(resource.name, resource.deadline_s)
        if limit.seconds <= 0:
            raise limit.exceeded()
        waits = self._waits
        ends_at = waits.monotonic() + limit.seconds
        resent = 0
        failure: ServiceError | None = None
        while True:
            response: httpx2.Response | None = None
            try:
                response = await self._attempt(method, path, query=query, content=content, headers=sent, limit=limit, ends_at=ends_at)
            except (Unavailable, Failed) as error:  # the transport's
                failure = error
            except Timeout:
                if failure is None:
                    raise
                raise failure  # noqa: B904 -- a resend the deadline cut short: the service is the unavailable one it was, with its own cause, and the timeout its context
            else:
                span.set_attribute("http.response.status_code", response.status_code)
                if response.is_success:
                    return response
                status = response.status_code
                log.warning("%s answered %s to %s %s", resource.name, status, method, path, extra={"resource": resource.name, "reply": status, "body": response.text[:2000]})
                failure = answered(status)(f"{resource.name} answered {status} to {method} {path}", resource=resource.name, status=status)
            if (wait_s := _retry_in(method, failure, response, resent=resent, ends_at=ends_at, waits=waits)) is None:
                raise failure
            resent += 1
            span.set_attribute("http.request.resend_count", resent)
            span.add_event("basis.retry", {"basis.error": str(failure), "basis.wait_s": wait_s})
            log.info("%s is unavailable for %s %s: making it again in %.3gs", resource.name, method, path, wait_s, extra={"resource": resource.name, "resent": resent, "wait_s": wait_s})
            await waits.sleep(wait_s)

    async def _attempt(  # noqa: PLR0913 -- the request's parts, by keyword
        self, method: HttpMethod, path: str, *, query: Query | None, content: bytes | None, headers: Mapping[str, str], limit: deadlines.Limit, ends_at: float
    ) -> httpx2.Response:
        """One attempt at the request, within what is left of the deadline: the service's reply, whatever its status.
        It raises `Timeout` once the deadline passes, and the transport's failure as `Unavailable` or `Failed`."""
        resource = self._resource
        if (left_s := ends_at - self._waits.monotonic()) <= 0:
            raise limit.exceeded()
        deadline = asyncio.timeout(left_s)
        try:
            async with deadline:
                return await self._client.request(method, path, params=_query(query), content=content, headers=headers, timeout=left_s)
        except TimeoutError as error:
            if deadline.expired():
                raise limit.exceeded() from error
            raise
        except httpx2.TimeoutException as error:  # one of the client's own timeouts, each the deadline
            raise limit.exceeded() from error
        except _UNREACHABLE as error:
            msg = f"{resource.name} could not be reached for {method} {path}: {type(error).__name__}"
            raise Unavailable(msg, resource=resource.name) from error
        except httpx2.RequestError as error:  # one such as a redirect loop, or a body that does not decode
            msg = f"{resource.name} failed {method} {path}: {type(error).__name__}"
            raise Failed(msg, resource=resource.name) from error


def _retry_in(method: HttpMethod, failure: ServiceError, response: httpx2.Response | None, *, resent: int, ends_at: float, waits: Waits) -> float | None:  # noqa: PLR0913 -- what the attempt was, and its clock
    """The seconds to wait before making the request again after `failure`, its `resent`-th resend: only for an
    `Unavailable` GET, and only where the wait ends before the deadline, on `waits`' clock; otherwise None."""
    if method != "GET" or not isinstance(failure, Unavailable):
        return None
    wait_s = min(RETRY_MAX_S, RETRY_FIRST_S * 2.0 ** min(resent, 32)) * waits.jitter()
    if response is not None and (after := _retry_after(response)) is not None:
        wait_s = max(wait_s, after)
    return wait_s if waits.monotonic() + wait_s < ends_at else None


def _retry_after(response: httpx2.Response) -> float | None:
    """The seconds a reply's `Retry-After` asks the caller to wait, where it gives them as a number; its other form, a
    date, is read as no wait of its own."""
    found = response.headers.get("retry-after", "").strip()
    return float(found) if found.isdigit() else None


def content(body: Body) -> bytes | None:
    """A call's body as JSON, or None for a call without one."""
    if body is None:
        return None
    return body.model_dump_json().encode() if isinstance(body, BaseModel) else to_json(body)


def _query(query: Query | None) -> dict[str, str | list[str]] | None:
    if query is None:
        return None
    encoded: dict[str, str | list[str]] = {}
    for name, value in query.items():
        encoded[name] = _text(value) if isinstance(value, str | int | float | Decimal | date | UUID) else [_text(item) for item in value]
    return encoded


def _text(value: QueryValue) -> str:
    match value:
        case bool():
            return "true" if value else "false"
        case date():  # a datetime too
            return value.isoformat()
        case _:
            return str(value)


class SystemClock:
    """The time, from `now`: the system's clock, a fake's, or a replay's, each value in its span."""

    def __init__(self, resource: ClockResource, now: Callable[[], datetime], tracer: Tracer) -> None:
        self._resource = resource
        self._now = now
        self._tracer = tracer

    def now(self) -> datetime:
        with self._tracer.start_as_current_span(f"{self._resource.name} now", attributes={"basis.resource": self._resource.name}) as span:
            value = self._now()
            span.set_attribute("basis.value", value.isoformat())
            return value


class NewIds:
    """New ids, from `new`: UUIDv7s, a fake's, or a replay's, each value in its span."""

    def __init__(self, resource: IdsResource, new: Callable[[], UUID], tracer: Tracer) -> None:
        self._resource = resource
        self._new = new
        self._tracer = tracer

    def new(self) -> UUID:
        with self._tracer.start_as_current_span(f"{self._resource.name} new", attributes={"basis.resource": self._resource.name}) as span:
            value = self._new()
            span.set_attribute("basis.value", str(value))
            return value

    def many(self, count: int) -> list[UUID]:
        """`count` new ids, in one span, which records how many rather than each: a column's ids, say."""
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            msg = f"{self._resource.name} makes a count of ids, a whole number of 0 or more, not {count!r}"
            raise ValueError(msg)
        with self._tracer.start_as_current_span(f"{self._resource.name} new", attributes={"basis.resource": self._resource.name, "basis.count": count}):
            return [self._new() for _ in range(count)]


def utc_now() -> datetime:
    return datetime.now(UTC)


_ADAPTERS: dict[object, object] = {}
"""Each type's pydantic adapter, made the first time a row or a reply is validated as it: building one costs a few
microseconds, which a call would otherwise pay every time. A program validates a fixed set of types."""


def adapter[T](kind: type[T]) -> TypeAdapter[T]:
    if (made := _ADAPTERS.get(kind)) is None:
        made = TypeAdapter(kind)
        _ADAPTERS[kind] = made
    return made  # ty: ignore[invalid-return-type] -- each type's entry is the adapter made for it, as above
