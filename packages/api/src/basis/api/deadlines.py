"""A route's deadline, which bounds the request it serves: every call the request makes through a resource or a compute
function gets what is left of it, or the resource's own deadline where that is shorter, so a resource's deadline only
ever shortens the route's. The deadline starts when the route begins to serve the request, and ends the commit too.

It is basis-core's deadline around the calls a block makes (`basis.core.deadlines.within`), of kind `route`, so that
basis-core's own resources, another service's client among them, keep to it too. `limit` says how long one call may
take, and `exceeded` is the 504 problem for one that takes longer."""

from collections.abc import Generator
from contextlib import contextmanager

from basis.api.resources import DeadlineExceededError
from basis.core import deadlines as core
from basis.core.deadlines import Deadline, Limit

__all__ = ["Deadline", "Limit", "exceeded", "limit", "route", "serving"]
_ROUTE = "route"


@contextmanager
def route(name: str, seconds: float) -> Generator[Deadline]:
    """The deadline of the route `name`, from now, for every call made in the block: the request it serves."""
    with core.within(_ROUTE, name, seconds) as deadline:
        yield deadline


def serving() -> Deadline | None:
    """The deadline of the route serving the request this runs in, or None outside one."""
    found = core.around()
    return found if found is not None and found.kind == _ROUTE else None


def limit(resource: str, own_s: float | None) -> Limit:
    """How long a call through `resource`, whose own deadline is `own_s`, may take now: the shorter of `own_s` and
    what is left of the current route's deadline."""
    if own_s is None and serving() is None:
        msg = f"{resource} runs within a route's deadline, and none is running: call it from a handler, while it serves a request"
        raise RuntimeError(msg)
    return core.limit(resource, own_s)


def exceeded(found: Limit) -> DeadlineExceededError:
    """The 504 problem of a call that took longer than `found` allows."""
    return DeadlineExceededError.of(found.exceeded())
