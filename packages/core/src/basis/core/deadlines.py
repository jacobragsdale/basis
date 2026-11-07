"""Each call's deadline: its resource's own, from when the call is made, or what is left of a deadline around every call
a block makes, where that is shorter, as an API's route bounds the calls its request makes. A resource's deadline
only ever shortens the one around it.

`within` sets the deadline around a block's calls, `limit` says how long one call may take now, and `Limit.exceeded`
is the `Timeout` of a call that takes longer."""

import time
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from basis.core.errors import Timeout


@dataclass(frozen=True)
class Deadline:
    """A deadline around every call a block makes."""

    kind: str
    """What it bounds, such as `route`."""
    name: str
    """Which one, such as the route's name."""
    seconds: float
    at: float
    """When it passes, on `time.monotonic`'s clock."""

    @property
    def whose(self) -> str:
        """Whose deadline it is, as a message names it: `route set_price`."""
        return f"{self.kind} {self.name}"


_AROUND: ContextVar[Deadline | None] = ContextVar("basis.core.deadlines.around", default=None)


@contextmanager
def within(kind: str, name: str, seconds: float) -> Generator[Deadline]:
    """A deadline of `seconds` from now around every call the block makes, in its context alone: a task the block
    starts carries it, and a worker thread that copies the context."""
    deadline = Deadline(kind, name, seconds, time.monotonic() + seconds)
    token = _AROUND.set(deadline)
    try:
        yield deadline
    finally:
        _AROUND.reset(token)


def around() -> Deadline | None:
    """The deadline around the calls made here, or None where there is none."""
    return _AROUND.get()


@dataclass(frozen=True)
class Limit:
    """How long one call may take, and whose deadline sets it."""

    resource: str
    seconds: float
    """What is left, which may be 0: a call with nothing left is never made."""
    own_s: float | None
    """The resource's own deadline, or None for work that has only the one around it."""
    enclosing: Deadline | None
    """The deadline around the call, where it is the shorter, or None where the resource's own is."""

    def exceeded(self) -> Timeout:
        if self.enclosing is not None:
            detail = f"{self.resource} did not answer within what was left of {self.enclosing.whose}'s deadline of {self.enclosing.seconds:g}s"
            return Timeout(detail, resource=self.resource)
        return Timeout(f"{self.resource} did not answer within its deadline of {self.own_s if self.own_s is not None else 0:g}s", resource=self.resource)


def limit(resource: str, own_s: float | None) -> Limit:
    """How long a call through `resource`, whose own deadline is `own_s`, may take now: the shorter of `own_s` and
    what is left of the deadline around it."""
    current = _AROUND.get()
    if current is None:
        if own_s is None:
            msg = f"{resource} has no deadline of its own, and runs only within one around it, which none is here"
            raise RuntimeError(msg)
        return Limit(resource, own_s, own_s, None)
    left = max(current.at - time.monotonic(), 0.0)
    if own_s is not None and own_s <= left:
        return Limit(resource, own_s, own_s, None)
    return Limit(resource, left, own_s, current)
