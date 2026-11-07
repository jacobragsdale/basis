"""Each request's boundary, the app's outermost middleware of its own. It gives the request an id, a UUIDv7, and adds
it, with the trace's ids, to every log line logged while the request is served, its handler's included. It returns the
id as `X-Request-Id` and the server span as `traceparent`, logs one line when the request ends, and turns an exception
nothing expected into a 500 problem, logged once, with its traceback. FastAPI's telemetry, around it, has already read
the caller's `traceparent` and started the server span, which is the current span here.

A health check makes no span, and logs only when it fails, at INFO, since one fails on purpose during a shutdown: the
cluster asks every few seconds."""

import logging
import time
from http import HTTPStatus
from uuid import uuid7

from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from basis.api import errors
from basis.core import logs

REQUEST_ID = "basis.request_id"
"""The scope key that holds the request's id, for the problem a handler's error becomes."""
HEALTH = "/health/"

log = logging.getLogger(__name__)
_PROPAGATOR = TraceContextTextMapPropagator()


class Requests:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid7()
        scope[REQUEST_ID] = request_id
        span = trace.get_current_span()
        ids: dict[str, object] = {"request_id": str(request_id)}
        headers = [(b"x-request-id", str(request_id).encode())]
        if (context := span.get_span_context()).is_valid:
            ids |= {"trace_id": trace.format_trace_id(context.trace_id), "span_id": trace.format_span_id(context.span_id)}
            carrier: dict[str, str] = {}
            _PROPAGATOR.inject(carrier)
            headers.extend((key.encode(), value.encode()) for key, value in carrier.items())
        started = time.perf_counter()
        status: int | None = None

        async def respond(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                message = {**message, "headers": [*message.get("headers", ()), *headers]}
            await send(message)

        with logs.fields(**ids):
            try:
                await self.app(scope, receive, respond)
            except Exception as error:
                log.exception("%s %s failed", scope["method"], scope["path"], extra=_summary(scope, HTTPStatus.INTERNAL_SERVER_ERROR if status is None else status, started))
                span.record_exception(error)  # FastAPI's telemetry, outside, sees only the 500
                if status is None:  # otherwise the response has begun, and the caller sees it cut short
                    await errors.response(error, request_id)(scope, receive, respond)
                return
            if (level := _level(scope["path"], status)) is not None:
                log.log(level, "%s %s: %s", scope["method"], scope["path"], status, extra=_summary(scope, status, started))


def _level(path: str, status: int | None) -> int | None:
    """How a request that ended without an unexpected error is logged: a server error as a warning, a health check
    only when it fails, and everything else as information."""
    failed = status is None or status >= errors.FIRST_ERROR
    if path.startswith(HEALTH):
        return logging.INFO if failed else None
    return logging.WARNING if status is None or status >= errors.FIRST_SERVER_ERROR else logging.INFO


def _summary(scope: Scope, status: int | None, started: float) -> dict[str, object]:
    route = scope.get("route")
    return {"method": scope["method"], "path": scope["path"], "route": route.path if isinstance(route, Route) else None, "status": status, "ms": logs.ms_since(started)}
