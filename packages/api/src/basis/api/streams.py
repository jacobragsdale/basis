"""Live updates to a UI, as server-sent events: a route that `api.stream` declares, which a UI opens with one GET and
reads for as long as it shows what the stream sends.

    api.stream("/bonds/{security}/quote", handlers.quote, deadline_s=1, every_s=2, response=Quote, errors=[UNKNOWN_BOND])

Its handler is an ordinary GET's: it reads the request, asks for what it reaches with `api.given`, and returns the
model, here the bond's latest quote. basis calls it when the stream opens and again every `every_s` seconds, and
sends what it returns whenever it changes:

- **Each call is a request of its own** to what it reaches: within the route's deadline, `deadline_s`, in a
  transaction of its own in each database it reads, which rolls back when the call ends, so a stream holds no
  connection between its calls, and every resource's span, fake and failures are what any route's are. What feeds a
  stream is therefore a repository function, polled; a subscription to another service would need basis-core's client
  to stream too, which it does not.
- **The first call answers as any route's does**: a failure, its problem with its status, and a success, the
  stream, `200 text/event-stream`, whose first event is that answer. A call after it that fails sends its problem,
  the one its route would answer, as the stream's last event, `problem`, and ends it.
- **An event is the model's JSON**, `data: {...}`, of the default type, sent only when it differs from the last one
  sent, so a UI gets each change once, and a reader that connects again gets the current value first; no event has an
  id, and no `Last-Event-ID` is read.
- **A heartbeat**, a comment line, is sent after `serve.heartbeat_s` seconds without a message, so that no proxy
  closes a connection it thinks idle.
- **A stream ends**, its last event `end`, `{"reason": ...}`, when the token it was opened with expires (`expired`),
  so the UI connects again with the token it has renewed; when it has lasted `serve.stream_s` (`limit`); or when the
  app drains (`shutdown`), so the UI reaches another replica. The stream's first message, `retry: 1000`, asks a
  reader to wait a second before it connects again. Identity is the request's, verified once, when the stream opens.
- **A slow reader** holds nothing of basis's: nothing queues, since a stream calls its handler only once its last
  message has gone, and a reader that has not taken a message within `serve.heartbeat_s` loses its stream.

The response is never compressed, since a compressor holds what it sends until it has enough, and says
`Cache-Control: no-cache` and `X-Accel-Buffering: no`, so neither a cache nor nginx's ingress holds it either. A
browser's `EventSource` sends no `Authorization` header, so a UI reads a stream with `fetch`, as a client of
server-sent events that sends headers does.

In the OpenAPI document a stream's success is `text/event-stream`, a string, and `x-basis-event` beside its schema
gives the schema of each event's data, which the breaking-change check compares as any route's response. A stream is
tested as its handler is, on fakes, since each call is one; `serve.stream_s` set short, in a test's settings, ends it."""

import asyncio
import logging
import math
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass
from enum import StrEnum
from typing import override
from uuid import UUID

from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Message, Receive, Scope, Send

from basis.api import deadlines, errors
from basis.api.dependencies import Runtime, running
from basis.api.identity import EXPIRES
from basis.api.requests import REQUEST_ID
from basis.api.resources import DatabaseResource
from basis.api.service import Route
from basis.core.errors import ServiceError

MEDIA_TYPE = "text/event-stream"
RETRY_MS = 1000
"""How long a reader waits before it connects again, once a stream ends, as the stream's first message asks."""
HEARTBEAT = b": heartbeat\n\n"
_WAKE_S = 0.25
"""The longest a stream sleeps without looking whether the app drains."""

log = logging.getLogger(__name__)


class Ended(StrEnum):
    """Why basis ended a stream, its `end` event's `reason`: the UI connects again in each case."""

    EXPIRED = "expired"
    """The token the stream was opened with expired: connect with the renewed one."""
    LIMIT = "limit"
    """The stream lasted `serve.stream_s`."""
    SHUTDOWN = "shutdown"
    """The app is shutting down: connect to another replica."""


def message(data: bytes, event: str | None = None) -> bytes:
    """One server-sent event: its type, where it is not the default, and its data, one line of compact JSON."""
    return (f"event: {event}\n".encode() if event is not None else b"") + b"data: " + data + b"\n\n"


class Events(Response):
    """A response of server-sent events, each chunk of `chunks` sent as it comes, within `send_s` or the stream ends;
    it ends too when the reader leaves, and closes `chunks` either way."""

    media_type = MEDIA_TYPE

    def __init__(self, chunks: AsyncGenerator[bytes], *, send_s: float) -> None:  # no body, so no Content-Length, as a StreamingResponse
        self.status_code = 200
        self.background = None
        self.init_headers({"cache-control": "no-cache", "x-accel-buffering": "no"})
        self._chunks = chunks
        self._send_s = send_s

    @override
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async with aclosing(self._chunks) as chunks:
            sending = asyncio.ensure_future(self._send(chunks, send))
            leaving = asyncio.ensure_future(_left(receive))
            try:
                await asyncio.wait((sending, leaving), return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (sending, leaving):
                    task.cancel()
                await asyncio.gather(sending, leaving, return_exceptions=True)
            if sending.done() and not sending.cancelled() and (failed := sending.exception()) is not None:
                raise failed

    async def _send(self, chunks: AsyncIterator[bytes], send: Send) -> None:
        await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
        async for chunk in chunks:
            try:
                async with asyncio.timeout(self._send_s):
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
            except TimeoutError:
                log.info("the reader took no message for %ss, so the stream ends", self._send_s, extra={"send_s": self._send_s})
                return
        await send({"type": "http.response.body", "body": b"", "more_body": False})


async def _left(receive: Receive) -> None:
    """Return once the reader leaves."""
    while True:
        received: Message = await receive()
        if received["type"] == "http.disconnect":
            return


type Answered = Callable[[Exception], Exception]
"""What a route answers for an exception its handler raised: its declared error, a domain exception's meaning, a kind
of service error, or a defect, as `routes.answered` decides."""


@dataclass(frozen=True)
class Stream:
    """One open stream: the route, what FastAPI gave its handler for this request, and the app that serves it."""

    route: Route
    values: Mapping[str, object]
    runtime: Runtime
    answered: Answered
    request_id: UUID | None
    expires: float | None
    """When the request's token expires, in seconds since 1970, where it has one."""

    @classmethod
    def opened(cls, route: Route, request: Request, values: Mapping[str, object], answered: Answered) -> Stream:
        request_id = request.scope.get(REQUEST_ID)
        expires = request.scope.get(EXPIRES)
        return cls(route, values, running(request), answered, request_id if isinstance(request_id, UUID) else None, expires if isinstance(expires, float | int) else None)

    async def call(self) -> bytes:
        """The handler's answer now, as an event's JSON: within the route's deadline, each database it reads a
        transaction of its own, rolled back once it returns."""
        route = self.route
        model = route.response
        if not (isinstance(model, type) and issubclass(model, BaseModel)):  # pragma: no cover -- `api.stream` refuses any other
            msg = f"{route.name} streams {model!r}, not a model"
            raise TypeError(msg)
        with deadlines.route(route.name, route.deadline_s):
            sessions = {ask.parameter: self.runtime.resources.session(ask.given, writes=False) for ask in route.asks if isinstance(ask.given, DatabaseResource)}
            try:
                found = await route.handler(**{**self.values, **sessions})
            except asyncio.CancelledError:  # the reader left, or the app stops: nothing may be awaited
                for session in sessions.values():
                    session.abandon()
                raise
            except BaseException:
                for session in sessions.values():
                    await session.end(commit=False)
                raise
            for session in sessions.values():
                await session.end(commit=False)
        if isinstance(found, Response):
            msg = f"{route.name} returned a {type(found).__qualname__}, which basis cannot check: return the {model.__qualname__} its route declares"
            raise TypeError(msg)
        return model.model_validate(found, from_attributes=True).model_dump_json(by_alias=True).encode()

    def events(self, first: bytes) -> AsyncGenerator[bytes]:
        """The stream's messages, from its first answer, until it ends."""
        serving = self.runtime.settings.serve
        started = time.monotonic()
        ends, reason = started + serving.stream_s, Ended.LIMIT
        if self.expires is not None and (expiring := started + (self.expires - time.time())) < ends:
            ends, reason = expiring, Ended.EXPIRED
        return self._events(first, ends, reason, serving.heartbeat_s)

    async def _events(self, first: bytes, ends: float, reason: Ended, heartbeat_s: float) -> AsyncGenerator[bytes]:
        every = self.route.every_s or math.inf
        last = first
        yield f"retry: {RETRY_MS}\n\n".encode() + message(first)
        sent = due = time.monotonic()
        due += every
        while True:
            now = time.monotonic()
            if self.runtime.draining():
                yield _end(Ended.SHUTDOWN)
                return
            if now >= ends:
                yield _end(reason)
                return
            if now >= due:
                due = max(due + every, now)
                try:
                    found = await self.call()
                except Exception as error:  # noqa: BLE001 -- answered as the route answers it, as the stream's last event
                    yield message(self._problem(error), "problem")
                    return
                if found != last:
                    last, sent = found, time.monotonic()
                    yield message(found)
                continue
            if now - sent >= heartbeat_s:
                sent = now
                yield HEARTBEAT
                continue
            await asyncio.sleep(max(0, min(due, sent + heartbeat_s, ends, now + _WAKE_S) - now))

    def _problem(self, error: Exception) -> bytes:
        """The problem the route answers for `error`, as the JSON of a stream's last event; a defect is logged, with
        its traceback, as the request boundary logs one."""
        answer = self.answered(error)
        if not isinstance(answer, errors.Error | ServiceError):
            log.error("%s failed while streaming", self.route.name, exc_info=answer, extra={"route": self.route.name})
        else:
            log.info("%s ends its stream with %s", self.route.name, type(answer).__qualname__, extra={"route": self.route.name})
        found, _ = errors.problem(answer, self.request_id)
        return found.model_dump_json(exclude_none=True).encode()


def _end(reason: Ended) -> bytes:
    return message(f'{{"reason":"{reason}"}}'.encode(), "end")
