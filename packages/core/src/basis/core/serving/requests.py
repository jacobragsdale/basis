"""The health routes and the request boundary, for any ASGI app on Starlette, an API's and a Dash app's alike.

The request boundary is an app's outermost middleware of its own. It gives each request an id, a UUIDv7, and adds it
to every log line logged while the request is served, the handler's included, as basis-core's logs add the ids of the
trace and of the span each line is logged in. It returns the id as `X-Request-Id` and the current span as
`traceparent`, logs one line when the request ends, and turns an exception nothing expected into a 500, logged once,
with its traceback. A caller that leaves before it has sent its body is logged as that, at INFO, and answered nothing. A
health check logs only when it fails, at INFO, since one fails on purpose during a shutdown: the cluster asks every few
seconds."""

import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from http import HTTPStatus
from uuid import UUID, uuid7

from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from basis.core import logs
from basis.core.serving import HEALTH, Health, Lifecycle, Stage

log = logging.getLogger(__name__)

REQUEST_ID = "basis.request_id"
"""The scope key that holds the request's id, for whatever answers the request's error."""

_FIRST_ERROR = HTTPStatus.BAD_REQUEST
_FIRST_SERVER_ERROR = HTTPStatus.INTERNAL_SERVER_ERROR
_PROPAGATOR = TraceContextTextMapPropagator()


type NotReady = Callable[[Request, Stage], Awaitable[Response]]
"""How a server answers readiness while it is not ready: a 503, in its own kind of body."""


async def _not_ready(_: Request, stage: Stage) -> Response:
    return Response(f'{{"status":"{stage}"}}', status_code=HTTPStatus.SERVICE_UNAVAILABLE, media_type="application/json")


def health(lifecycle: Lifecycle, commit: str | None, *, prefix: str = "", not_ready: NotReady = _not_ready) -> list[BaseRoute]:
    """The routes `PREFIX/health/live` and `PREFIX/health/ready`, for an ASGI app's router: live while the process
    answers, and ready only while `lifecycle` is, each with `commit`; before and after, readiness answers `not_ready`'s
    response, by default a 503 that names the stage. `prefix` is the path an app is served under, such as `/apps/pos`,
    with no slash at its end, where a proxy in front of it keeps it."""
    live_body = Health(status="live", commit=commit).model_dump_json()
    ready_body = Health(status="ready", commit=commit).model_dump_json()

    async def live(_: Request) -> Response:
        return Response(live_body, media_type="application/json")

    async def ready(request: Request) -> Response:
        if (stage := lifecycle.stage) is not Stage.READY:
            return await not_ready(request, stage)
        return Response(ready_body, media_type="application/json")

    base = f"{prefix.rstrip('/')}{HEALTH}"
    return [Route(f"{base}live", live, methods=["GET"], include_in_schema=False), Route(f"{base}ready", ready, methods=["GET"], include_in_schema=False)]


def is_health(path: str, prefix: str = "") -> bool:
    """Whether `path` is a health check's, under `prefix`: which makes no span, and logs only when it fails."""
    return path.startswith(f"{prefix.rstrip('/')}{HEALTH}")


type Answer = Callable[[Exception, UUID], ASGIApp]
"""What a server answers for an exception nothing expected, given the request's id: a 500, in its own kind of body."""
type Fields = Callable[[Scope], Mapping[str, object]]
"""What a server adds to the one line the boundary logs for each request, from the request's scope."""


def _answer(_: Exception, __: UUID) -> ASGIApp:
    return Response('{"status":500}', status_code=HTTPStatus.INTERNAL_SERVER_ERROR, media_type="application/json")


def _no_fields(_: Scope) -> Mapping[str, object]:
    return {}


class Requests:
    """The request boundary, as the module's docstring says, for Starlette's `Middleware(Requests, ...)`: health
    checks under `prefix`, the 500 `answer` gives for an exception nothing expected, and each request's log line with
    what `fields` adds."""

    def __init__(self, app: ASGIApp, *, prefix: str = "", answer: Answer = _answer, fields: Fields = _no_fields) -> None:
        self.app = app
        self._prefix = prefix
        self._answer = answer
        self._fields = fields

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid7()
        scope[REQUEST_ID] = request_id
        span = trace.get_current_span()
        headers = [(b"x-request-id", str(request_id).encode())]
        if span.get_span_context().is_valid:
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

        with logs.fields(request_id=str(request_id)):
            try:
                await self.app(scope, receive, respond)
            except ClientDisconnect:  # the caller's doing, not a defect, and no one is left to answer
                log.info("%s %s: the caller left before sending its body", scope["method"], scope["path"], extra=self._summary(scope, status, started))
                return
            except Exception as error:
                log.exception("%s %s failed", scope["method"], scope["path"], extra=self._summary(scope, HTTPStatus.INTERNAL_SERVER_ERROR if status is None else status, started))
                span.record_exception(error)  # the server span's middleware, outside, sees only the 500
                if status is None:  # otherwise the response has begun, and the caller sees it cut short
                    await self._answer(error, request_id)(scope, receive, respond)
                return
            if (level := self._level(scope["path"], status)) is not None:
                log.log(level, "%s %s: %s", scope["method"], scope["path"], status, extra=self._summary(scope, status, started))

    def _level(self, path: str, status: int | None) -> int | None:
        """How a request that ended without an unexpected error is logged: a server error as a warning, a health
        check only when it fails, and everything else as information."""
        failed = status is None or status >= _FIRST_ERROR
        if is_health(path, self._prefix):
            return logging.INFO if failed else None
        return logging.WARNING if status is None or status >= _FIRST_SERVER_ERROR else logging.INFO

    def _summary(self, scope: Scope, status: int | None, started: float) -> dict[str, object]:
        route = scope.get("route")
        summary = {"method": scope["method"], "path": scope["path"], "route": route.path if isinstance(route, Route) else None, "status": status, "ms": logs.ms_since(started)}
        return summary | dict(self._fields(scope))
