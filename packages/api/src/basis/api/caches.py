"""A cache resource as the app opens it: values kept in the process's memory, each until its time to live passes, and
the most recently read up to the cache's `max_entries`, each call with a span of its own; one value loaded once,
however many requests ask for it at once."""

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from opentelemetry.trace import Tracer

from basis.api import deadlines
from basis.api.resources import CacheResource

log = logging.getLogger(__name__)


class _Abandoned(Exception):  # noqa: N818 -- not an error a caller sees: a waiter loads the value itself
    """The request loading a value stopped before it finished."""


class MemoryCache[V]:
    """Values kept in the process, by key, each until its time to live passes, and the most recently read up to
    `max_entries`. The event loop is the only thread that touches it."""

    def __init__(self, resource: CacheResource[V], monotonic: Callable[[], float], tracer: Tracer) -> None:
        self._resource = resource
        self._monotonic = monotonic
        self._tracer = tracer
        self._entries: OrderedDict[str, tuple[float, V]] = OrderedDict()
        self._loading: dict[str, asyncio.Future[V]] = {}

    async def get(self, key: str, load: Callable[[], Awaitable[V]], /) -> V:
        resource = self._resource
        with self._tracer.start_as_current_span(f"{resource.name} get", attributes={"basis.resource": resource.name, "basis.deadline_s": resource.deadline_s}) as span:
            limit = deadlines.limit(resource.name, resource.deadline_s)
            if limit.seconds <= 0:
                raise deadlines.exceeded(limit)
            deadline = asyncio.timeout(limit.seconds)
            try:
                async with deadline:
                    value, hit = await self._get(key, load)
            except TimeoutError as error:
                if deadline.expired():
                    raise deadlines.exceeded(limit) from error
                raise
            span.set_attribute("basis.cache.hit", hit)
            return value

    async def _get(self, key: str, load: Callable[[], Awaitable[V]]) -> tuple[V, bool]:
        while True:
            if (entry := self._entries.get(key)) is not None:
                expires, value = entry
                if self._monotonic() < expires:
                    self._entries.move_to_end(key)
                    return value, True
                del self._entries[key]
            if (loading := self._loading.get(key)) is not None:
                try:
                    return await asyncio.shield(loading), False
                except _Abandoned:
                    continue  # the request that was loading it stopped: load it here
            loaded = asyncio.Future[V]()  # on the running loop
            self._loading[key] = loaded
            try:
                value = await load()
            except BaseException as error:
                del self._loading[key]
                loaded.set_exception(error if isinstance(error, Exception) else _Abandoned())
                loaded.exception()  # retrieved, whether or not another request waits on it
                raise
            del self._loading[key]
            self._entries[key] = (self._monotonic() + self._resource.ttl_s, value)
            if len(self._entries) > self._resource.max_entries:
                self._entries.popitem(last=False)
            loaded.set_result(value)
            return value, False
