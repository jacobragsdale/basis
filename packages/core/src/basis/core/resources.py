"""The resources every part declares alike, besides its databases: another HTTP service, the clock and new ids. Each is
declared by name, with everything it needs stated, and none of it guessed:

| Declared with | Gives | Configured by |
|---|---|---|
| `http(name, deadline_s=)` | a `Client` | `BASIS_HTTP__NAME__URL`, and `__CA_BUNDLE` and `__PROXY` where it needs them |
| `clock(name)` | a `Clock` | nothing |
| `ids(name)` | `Ids` | nothing |

Every call through one has a span of its own, and every call that can wait a deadline: its resource's own, or what is
left of the deadline around it where that is shorter (`basis.core.deadlines`). `basis.core.opened` opens each, and
`basis.core.fakes` holds a fake of each, which replaces only what the resource reaches. The clock and the ids never
wait, so they have no deadline, and their spans record each value, so that a recorded run replays them. A call that
fails raises one of the kinds of `basis.core.errors.ServiceError`, whether its resource is real or a fake: `Timeout`
when it does not finish in time, and the kind of another service's failure otherwise.

This module opens nothing, so a part's declarations import it; each part says how its code declares and is given them."""

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, JsonValue

from basis.core.errors import BasisError

NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")
"""Lower-case letters, digits and underscores, so that a resource's settings are variables every shell can set."""

type HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
type QueryValue = str | int | float | Decimal | bool | date | datetime | UUID
type Query = Mapping[str, QueryValue | Sequence[QueryValue]]
type Body = BaseModel | JsonValue


class ResourceError(BasisError, TypeError):
    """A resource declared without what it needs; the message says what to declare."""


@dataclass(frozen=True)
class HttpResource:
    name: str
    """The name its settings are under, as `BASIS_HTTP__PRICING__URL` for `pricing`."""
    deadline_s: float
    """Seconds each call may take, from the first byte sent to the last byte of the reply."""


@dataclass(frozen=True)
class ClockResource:
    name: str


@dataclass(frozen=True)
class IdsResource:
    name: str


def http(name: str, *, deadline_s: float) -> HttpResource:
    """Another HTTP service, whose base URL is the setting `BASIS_HTTP__NAME__URL`, and which answers in JSON."""
    return HttpResource(named(name, "http"), seconds(name, deadline_s))


def clock(name: str) -> ClockResource:
    """The time, from the system's clock, or a fake's in a test."""
    return ClockResource(named(name, "clock"))


def ids(name: str) -> IdsResource:
    """New ids, each a UUIDv7, which sorts by when it was made."""
    return IdsResource(named(name, "ids"))


def named(name: str, kind: str) -> str:
    """`name`, which must be a resource's: `NAME`."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        msg = f"{kind} {name!r}: a resource's name is lower-case letters, digits and underscores, starting with a letter, so its settings are variables any shell sets"
        raise ResourceError(msg)
    return name


def seconds(name: str, deadline_s: float) -> float:
    """`deadline_s`, which must be the seconds above 0 that each call through `name` may take."""
    if isinstance(deadline_s, bool) or not isinstance(deadline_s, int | float) or math.isnan(deadline_s) or deadline_s <= 0:
        msg = f"{name}'s deadline is {deadline_s}s: give it deadline_s, the seconds each call may take, above 0"
        raise ResourceError(msg)
    return deadline_s


class Client(Protocol):
    """Another HTTP service. Each call's reply is validated as `reply`, from JSON. A call that fails raises the kind of
    `basis.core.errors.ServiceError` its failure is: `Timeout` past its deadline or for a 504, `Unavailable` for a
    service that cannot be reached or answers 502, 503 or 429, which a GET makes again with backoff while its deadline
    allows, `BadReply` for a body that is not a `reply`, and the kind of any other status that is not a 2xx. `path` is
    the service's own, after its base URL, and anything in it that came from outside must be validated first: a path
    parameter's pattern, say. Each call carries its span on to the service, as `traceparent`."""

    async def call[T](self, method: HttpMethod, path: str, reply: type[T], /, *, query: Query | None = None, body: Body = None) -> T: ...

    async def get[T](self, path: str, reply: type[T], /, *, query: Query | None = None) -> T: ...

    async def post[T](self, path: str, reply: type[T], /, *, query: Query | None = None, body: Body = None) -> T: ...


class Clock(Protocol):
    def now(self) -> datetime:
        """The time now, in UTC."""
        ...


class Ids(Protocol):
    def new(self) -> UUID:
        """A new UUIDv7."""
        ...
