"""A route's deadline, which bounds the request it serves: every call the request makes through a resource or a compute
function gets what is left of it, or the resource's own deadline where that is shorter, so a resource's deadline only
ever shortens the route's. The deadline starts when the route begins to serve the request, and ends the commit too.

`limit` says how long one call may take, and `Limit.exceeded` is the 504 problem for one that takes longer."""

import time
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from basis.api.resources import DeadlineExceededError


@dataclass(frozen=True)
class Deadline:
    route: str
    """The route's name, its handler's."""
    seconds: float
    at: float
    """When it passes, on `time.monotonic`'s clock."""


_CURRENT: ContextVar[Deadline | None] = ContextVar("basis.api.deadlines.current", default=None)


@contextmanager
def route(name: str, seconds: float) -> Generator[Deadline]:
    """The deadline of the route `name`, from now, for every call made in the block: the request it serves."""
    deadline = Deadline(name, seconds, time.monotonic() + seconds)
    token = _CURRENT.set(deadline)
    try:
        yield deadline
    finally:
        _CURRENT.reset(token)


def serving() -> Deadline | None:
    """The deadline of the route serving the request this runs in, or None outside one."""
    return _CURRENT.get()


@dataclass(frozen=True)
class Limit:
    """How long one call may take, and whose deadline sets it."""

    resource: str
    seconds: float
    """What is left, which may be 0: a call with nothing left is never made."""
    own_s: float | None
    """The resource's own deadline; None for a compute function, which has only its route's."""
    route: Deadline | None
    """The route's deadline, where it is the shorter, or None where the resource's own is."""

    def exceeded(self) -> DeadlineExceededError:
        if self.route is not None:
            detail = f"{self.resource} did not answer within what was left of route {self.route.route}'s deadline of {self.route.seconds:g}s"
            return DeadlineExceededError(self.resource, self.route.seconds, detail=detail)
        return DeadlineExceededError(self.resource, self.own_s if self.own_s is not None else 0)


def limit(resource: str, own_s: float | None) -> Limit:
    """How long a call through `resource`, whose own deadline is `own_s`, may take now: the shorter of `own_s` and
    what is left of the current route's deadline."""
    current = _CURRENT.get()
    if current is None:
        if own_s is None:
            msg = f"{resource} runs within a route's deadline, and none is running: call it from a handler, while it serves a request"
            raise RuntimeError(msg)
        return Limit(resource, own_s, own_s, None)
    left = max(current.at - time.monotonic(), 0.0)
    if own_s is not None and own_s <= left:
        return Limit(resource, own_s, own_s, None)
    return Limit(resource, left, own_s, current)
