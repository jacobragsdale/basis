"""A fake of each resource `basis.core.resources` declares, for tests, which a part opens in place of what the settings
configure. A fake stands in only for what the resource reaches: the other service's network, the system's clock, the
ids' randomness. Everything around it is the part's own, as in production: each call's deadline and span, the checks
of every reply, and the kind of `basis.core.errors.ServiceError` a failure is.

A fake service fails on demand, so a test reaches every error path without a mock: `pricing.fails("/prices/X",
Unavailable)` answers each request for that path as a service that fails so does, every time, or `times=1` once and
then as its app does. Its answer is what a real service's would be, a status or a body that is not JSON, so the part's
own client makes the kind of it, as it makes a real service's, and retries it as it would."""

import threading
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Any
from uuid import UUID

import httpx2

from basis.core.errors import KINDS, BadReply, Conflict, Denied, Failed, NotFound, Rejected, ServiceError, Timeout, Unavailable

type _Message = MutableMapping[str, Any]
type ASGIApp = Callable[[_Message, Callable[[], Awaitable[_Message]], Callable[[_Message], Awaitable[None]]], Awaitable[None]]
"""An ASGI app, such as FastAPI's or Starlette's, or another service's own basis app."""

ANSWERS: dict[type[ServiceError], int] = {
    Timeout: HTTPStatus.GATEWAY_TIMEOUT,
    Unavailable: HTTPStatus.SERVICE_UNAVAILABLE,
    NotFound: HTTPStatus.NOT_FOUND,
    Conflict: HTTPStatus.CONFLICT,
    Rejected: HTTPStatus.UNPROCESSABLE_CONTENT,
    Denied: HTTPStatus.FORBIDDEN,
    BadReply: HTTPStatus.OK,
    Failed: HTTPStatus.INTERNAL_SERVER_ERROR,
}
"""The status a fake service that fails with each kind answers; `BadReply`'s 2xx has a body that is not JSON."""
NOT_JSON = b"not JSON"


@dataclass
class _Failing:
    kind: type[ServiceError]
    left: int | None
    """How many more requests it fails, or None for every one."""


class Http:
    """Another service, as an ASGI app the test gives, such as a FastAPI app with the routes the code calls. Calls reach
    it in the process, through the part's own client, unless the test has made a path fail (`fails`)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._failing: dict[str, _Failing] = {}
        self._lock = threading.Lock()

    def fails(self, path: str, kind: type[ServiceError], *, times: int | None = None) -> None:
        """Answer each request for `path`, the service's own after its base URL and without its query, as a service
        that fails with `kind`: every one, or the next `times`, after which the app answers again."""
        if not (isinstance(kind, type) and issubclass(kind, ServiceError) and kind in KINDS):
            msg = f"a fake service fails with one of the kinds of ServiceError, {', '.join(found.__name__ for found in KINDS)}, not {kind!r}"
            raise TypeError(msg)
        if not isinstance(path, str) or not path.startswith("/"):
            msg = f"a fake service fails a path of its own, after its base URL, such as /prices/X, not {path!r}"
            raise ValueError(msg)
        if times is not None and (isinstance(times, bool) or not isinstance(times, int) or times < 1):
            msg = f"a fake service fails a path a whole number of times, 1 or more, or every time with times=None, not {times!r}"
            raise ValueError(msg)
        with self._lock:
            self._failing[path] = _Failing(kind, times)

    def client(self, name: str) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.serve), base_url=f"http://{name}")

    async def serve(self, scope: _Message, receive: Callable[[], Awaitable[_Message]], send: Callable[[_Message], Awaitable[None]]) -> None:
        """The service as the fake serves it, the test's failures and then its app, which `client` reaches in the
        process, and a test may also serve on a socket, to check the part's client makes the same of both."""
        if scope["type"] != "http" or (kind := self._failure(str(scope["path"]))) is None:
            await self.app(scope, receive, send)
            return
        while (await receive()).get("more_body", False):
            pass
        status = ANSWERS[kind]
        body = NOT_JSON if kind is BadReply else b'{"failed": "%s"}' % kind.__name__.encode()
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    def _failure(self, path: str) -> type[ServiceError] | None:
        with self._lock:
            if (failing := self._failing.get(path)) is None:
                return None
            if failing.left is not None:
                failing.left -= 1
                if failing.left == 0:
                    del self._failing[path]
            return failing.kind


class Clock:
    """The time is `at`, until the test advances it."""

    def __init__(self, *, at: datetime) -> None:
        if at.tzinfo is None:
            msg = f"the fake clock's time {at} has no time zone: give it one, as datetime(..., tzinfo=UTC)"
            raise ValueError(msg)
        self._at = at.astimezone(UTC)

    def now(self) -> datetime:
        return self._at

    def advance(self, by: timedelta) -> None:
        self._at += by


class Ids:
    """UUIDv7s that count up from 1, in their random bits, at the epoch: `00000000-0000-7000-8000-000000000001`, then
    `...0002`, so a test can name each id the code makes."""

    def __init__(self) -> None:
        self._made = 0
        self._lock = threading.Lock()

    def new(self) -> UUID:
        with self._lock:
            self._made += 1
            made = self._made
        return UUID(int=(7 << 76) | (0b10 << 62) | made)


type Fake = Http | Clock | Ids
