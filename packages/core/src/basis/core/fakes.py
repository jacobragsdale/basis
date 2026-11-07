"""A fake of each resource `basis.core.resources` declares, for tests, which a part opens in place of what the settings
configure. A fake stands in only for what the resource reaches: the other service's network, the system's clock, the
ids' randomness. Everything around it is the part's own, as in production: each call's deadline and span, and the
checks of every reply."""

import threading
from collections.abc import Awaitable, Callable, MutableMapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx2

type _Message = MutableMapping[str, Any]
type ASGIApp = Callable[[_Message, Callable[[], Awaitable[_Message]], Callable[[_Message], Awaitable[None]]], Awaitable[None]]
"""An ASGI app, such as FastAPI's or Starlette's, or another service's own basis app."""


class Http:
    """Another service, as an ASGI app the test gives, such as a FastAPI app with the routes the code calls. Calls reach
    it in the process, through the part's own client."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    def client(self, name: str) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app), base_url=f"http://{name}")


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
