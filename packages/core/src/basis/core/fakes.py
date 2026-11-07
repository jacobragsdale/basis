"""A fake of each resource `basis.core.resources` declares, for tests, which a part opens in place of what the settings
configure. A fake stands in only for what the resource reaches: the other service's network, the system's clock, the
ids' randomness. Everything around it is the part's own, as in production: each call's deadline and span, the checks
of every reply, and the kind of `basis.core.errors.ServiceError` a failure is.

A fake service fails on demand, so a test reaches every error path without a mock: `pricing.fails("/prices/X",
Unavailable)` answers each request for that path as a service that fails so does, every time, or `times=1` once and
then as its app does. Its answer is what a real service's would be, a status or a body that is not JSON, so the part's
own client makes the kind of it, as it makes a real service's, and retries it as it would: a fake service's client
waits as its `waits` do, which take no time, so a GET made again until its deadline makes every attempt at once.
`Failures` is how a fake keeps what it fails with, which every part's fakes share."""

import threading
import time
from collections.abc import Awaitable, Callable, Hashable, MutableMapping
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
    """How many more calls it fails, or None for every one."""


class Failures[K: Hashable]:
    """What a fake fails with, by key, such as a service's path or a database's statement: a kind of `ServiceError`,
    every time, or the next `times`, which the fake takes as each call is made."""

    def __init__(self, what: str, kinds: tuple[type[ServiceError], ...]) -> None:
        self._what = what
        """The fake, as its refusals name it: `a fake service`."""
        self._kinds = kinds
        """The kinds it may fail with: those of what it fakes."""
        self._failing: dict[K, _Failing] = {}
        self._lock = threading.Lock()

    def set(self, key: K, kind: type[ServiceError], times: int | None) -> None:
        """Fail each call `key` names with `kind`: every one, or the next `times`."""
        if not (isinstance(kind, type) and kind in self._kinds):
            names = ", ".join(found.__name__ for found in self._kinds)
            msg = f"{self._what} fails with one of {names}, the kinds of ServiceError it can raise, not {getattr(kind, '__name__', kind)!r}"
            raise TypeError(msg)
        if times is not None and (isinstance(times, bool) or not isinstance(times, int) or times < 1):
            msg = f"{self._what} fails a whole number of times, 1 or more, or every time with times=None, not {times!r}"
            raise ValueError(msg)
        with self._lock:
            self._failing[key] = _Failing(kind, times)

    def take(self, *keys: K, only: frozenset[type[ServiceError]] | None = None) -> tuple[K, type[ServiceError]] | None:
        """The first of `keys` that fails, and its kind, counting it as failed once; or None. With `only`, a failure of
        another kind is left as it is, and not taken."""
        with self._lock:
            for key in keys:
                if (failing := self._failing.get(key)) is None or (only is not None and failing.kind not in only):
                    continue
                if failing.left is not None:
                    failing.left -= 1
                    if failing.left == 0:
                        del self._failing[key]
                return key, failing.kind
        return None


class Waits:
    """Waits that take no time: each is kept, in `waited`, and the clock the client reads moves on by it, so a call
    made again while its deadline allows is made as often as it would be, at once. Each waits all of its backoff."""

    def __init__(self) -> None:
        self.waited: list[float] = []
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return time.monotonic() + sum(self.waited)

    async def sleep(self, seconds: float) -> None:
        with self._lock:
            self.waited.append(seconds)

    def jitter(self) -> float:
        return 1.0


class Http:
    """Another service, as an ASGI app the test gives, such as a FastAPI app with the routes the code calls. Calls reach
    it in the process, through the part's own client, unless the test has made a path fail (`fails`), and the client
    waits between attempts as `waits` does."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.waits = Waits()
        self._failing = Failures[str]("a fake service", KINDS)

    def fails(self, path: str, kind: type[ServiceError], *, times: int | None = None) -> None:
        """Answer each request for `path`, the service's own after its base URL and without its query, as a service
        that fails with `kind`: every one, or the next `times`, after which the app answers again."""
        if not isinstance(path, str) or not path.startswith("/"):
            msg = f"a fake service fails a path of its own, after its base URL, such as /prices/X, not {path!r}"
            raise ValueError(msg)
        self._failing.set(path, kind, times)

    def client(self, name: str) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.serve), base_url=f"http://{name}")

    async def serve(self, scope: _Message, receive: Callable[[], Awaitable[_Message]], send: Callable[[_Message], Awaitable[None]]) -> None:
        """The service as the fake serves it, the test's failures and then its app, which `client` reaches in the
        process, and a test may also serve on a socket, to check the part's client makes the same of both."""
        if scope["type"] != "http" or (taken := self._failing.take(str(scope["path"]))) is None:
            await self.app(scope, receive, send)
            return
        while (await receive()).get("more_body", False):
            pass
        kind = taken[1]
        status = ANSWERS[kind]
        body = NOT_JSON if kind is BadReply else b'{"failed": "%s"}' % kind.__name__.encode()
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


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
