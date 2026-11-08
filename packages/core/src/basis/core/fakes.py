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

import json
import smtplib
import threading
from collections.abc import Awaitable, Callable, Hashable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from http import HTTPStatus
from typing import Any, Literal
from uuid import UUID

import httpx2

from basis.core.errors import KINDS, BadReply, Conflict, Denied, Failed, NotFound, Rejected, ServiceError, Timeout, Unavailable
from basis.core.mongo import Upserted

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
    """Waits that take no time: each is kept, in `waited`, and the clock the client reads moves by them alone, so a call
    made again while its deadline allows is made as often as the backoff says, at once, however long each attempt
    takes on the machine. Each waits all of its backoff. An attempt itself still has what is left of the deadline,
    in real time."""

    def __init__(self) -> None:
        self.waited: list[float] = []
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return sum(self.waited)

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


SLACK_KINDS: tuple[type[ServiceError], ...] = (Timeout, Unavailable, NotFound, Rejected, Denied, Failed)
"""The kinds a Slack channel's post raises, real or fake: never a `Conflict` or a `BadReply`, since its reply is read
for its status alone."""
EMAIL_KINDS: tuple[type[ServiceError], ...] = (Timeout, Unavailable, Rejected, Denied, Failed)
"""The kinds an email's send raises, real or fake."""


class Slack:
    """A Slack channel, as its webhook answers: each message posted keeps its text, in `posted`, unless the test has
    made the channel fail (`fails`), when the webhook answers as a failing one does, every time, or the next `times`.
    The part's own poster posts to it, through `transport`, as it posts to Slack."""

    def __init__(self) -> None:
        self.posted: list[str] = []
        self._failing = Failures[str]("a fake Slack channel", SLACK_KINDS)

    def fails(self, kind: type[ServiceError], *, times: int | None = None) -> None:
        self._failing.set("post", kind, times)

    def client(self) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self._answer))

    def _answer(self, request: httpx2.Request) -> httpx2.Response:
        if (taken := self._failing.take("post")) is not None:
            return httpx2.Response(ANSWERS[taken[1]], text="failing")
        sent: object = json.loads(request.content)
        if not isinstance(sent, dict) or not isinstance(text := sent.get("text"), str):
            return httpx2.Response(HTTPStatus.BAD_REQUEST, text="invalid_payload")
        self.posted.append(text)
        return httpx2.Response(HTTPStatus.OK, text="ok")


_SMTP_FAILURES: dict[type[ServiceError], Callable[[], Exception]] = {
    Timeout: lambda: TimeoutError("timed out"),
    Unavailable: lambda: smtplib.SMTPConnectError(421, b"4.3.2 service not available"),
    Rejected: lambda: smtplib.SMTPRecipientsRefused({"someone@example.com": (550, b"5.1.1 no such user")}),
    Denied: lambda: smtplib.SMTPAuthenticationError(535, b"5.7.8 authentication failed"),
    Failed: lambda: smtplib.SMTPDataError(554, b"5.6.0 message refused"),
}
"""What a relay that fails with each kind does, as `smtplib` raises it."""


class Email:
    """An SMTP relay: each message sent to it keeps what it was, in `sent`, unless the test has made the relay fail
    (`fails`), when it fails as a relay that does, every time, or the next `times`. The part's own mailer opens a
    session with it through `connect`, as it does with a relay."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        self.logins: list[str] = []
        self._failing = Failures[str]("a fake email relay", EMAIL_KINDS)

    def fails(self, kind: type[ServiceError], *, times: int | None = None) -> None:
        self._failing.set("send", kind, times)

    def connect(self, relay: object, timeout_s: float) -> _Session:
        del relay, timeout_s  # a fake relay is reached in the process, at once
        return _Session(self)


class _Session:
    """One session with a fake relay, as `smtplib.SMTP` gives one."""

    def __init__(self, relay: Email) -> None:
        self._relay = relay

    def login(self, user: str, password: str) -> object:
        del password
        self._relay.logins.append(user)
        return None

    def send_message(self, msg: EmailMessage) -> object:
        if (taken := self._relay._failing.take("send")) is not None:  # noqa: SLF001 -- the session is its relay's
            failure = _SMTP_FAILURES[taken[1]]()
            raise failure
        self._relay.sent.append(msg)
        return {}

    def quit(self) -> object:
        return None


MONGO_KINDS: tuple[type[ServiceError], ...] = (Timeout, Unavailable, Conflict, Rejected, Denied, Failed)
"""The kinds a Mongo database's reads and upserts raise, real or fake."""


class Mongo:
    """A Mongo database in memory: each collection a list of documents, in `collections`, which a test fills and reads.
    A query matches by equality and by `$eq`, `$ne`, `$gt`, `$gte`, `$lt`, `$lte` and `$in` on a field, as a job's
    filters do; an upsert sets fields by key, as Mongo's does. It fails on demand (`fails`), every time or the next
    `times`, with a kind of `ServiceError`: its finds, its upserts, or both."""

    def __init__(self) -> None:
        self.collections: dict[str, list[dict[str, object]]] = {}
        self._failing = Failures[str]("a fake Mongo database", MONGO_KINDS)

    def fails(self, kind: type[ServiceError], *, times: int | None = None, on: Literal["find", "upsert", "any"] = "any") -> None:
        self._failing.set(on, kind, times)

    def find(self, collection: str, query: Mapping[str, object], *, limit: int) -> list[dict[str, object]]:
        self._fail("find")
        return [dict(document) for document in self.collections.get(collection, []) if _matches(document, query)][:limit]

    def upsert(self, collection: str, key: Sequence[str], documents: Sequence[Mapping[str, object]]) -> Upserted:
        self._fail("upsert")
        held = self.collections.setdefault(collection, [])
        matched = modified = upserted = 0
        for document in documents:
            found = next((there for there in held if all(there.get(field) == document[field] for field in key)), None)
            if found is None:
                held.append(dict(document))
                upserted += 1
                continue
            matched += 1
            if any(found.get(field) != value for field, value in document.items()):
                found.update(document)
                modified += 1
        return Upserted(matched, modified, upserted)

    def _fail(self, operation: str) -> None:
        if (taken := self._failing.take(operation, "any")) is not None:
            failure = taken[1]("a fake Mongo database fails", resource="mongo")
            raise failure


_COMPARE: dict[str, Callable[[Any, Any], bool]] = {
    "$eq": lambda found, wanted: found == wanted,
    "$ne": lambda found, wanted: found != wanted,
    "$gt": lambda found, wanted: found is not None and found > wanted,
    "$gte": lambda found, wanted: found is not None and found >= wanted,
    "$lt": lambda found, wanted: found is not None and found < wanted,
    "$lte": lambda found, wanted: found is not None and found <= wanted,
    "$in": lambda found, wanted: found in wanted,
}


def _matches(document: Mapping[str, object], query: Mapping[str, object]) -> bool:
    for field, wanted in query.items():
        found = document.get(field)
        if isinstance(wanted, Mapping) and wanted and all(str(operator).startswith("$") for operator in wanted):
            if not all(_COMPARE[str(operator)](found, value) for operator, value in wanted.items()):
                return False
        elif found != wanted:
            return False
    return True


type Fake = Http | Clock | Ids | Slack | Email | Mongo
