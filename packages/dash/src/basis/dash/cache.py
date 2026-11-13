"""The process's cache of loaded datasets: each value an Arrow table, which nothing can change, kept by its dataset's
name and its params, and handed to each caller as a frame of its own (`basis.core.frames.from_arrow`), so a caller that
changes its frame changes nobody else's.

- **Fresh** for its dataset's `ttl_s` from when it was loaded: a load then answers from it, and reads nothing.
- **Kept** for `stale_s` more, so that a reload that fails can answer it, marked stale (15.5).
- **Bounded** by the setting `cache_mb`, the Arrow tables' bytes: past it, the least recently used are evicted first.
  A value larger than the whole ceiling is answered once and never kept.
- **Versioned**: each value loaded has a number no other has, which a view's key carries for each of its inputs, so a
  view is computed again when an input is loaded again, and never otherwise (15.3).

Callers that ask for one key at the same moment share one load (`Cache.flight`), and nothing here blocks: the cache is
the event loop's alone, and only the load itself leaves it."""

import asyncio
import itertools
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass
from datetime import datetime

import pyarrow as pa

log = logging.getLogger(__name__)

type Key = tuple[str, tuple[tuple[str, Hashable], ...]]
"""A value's key: its dataset's name, and its params by name, in order; a view's params carry its inputs' versions."""

MEGABYTE = 1024 * 1024


def key(name: str, params: Mapping[str, Hashable]) -> Key:
    return name, tuple(sorted(params.items()))


@dataclass(frozen=True)
class Entry:
    table: pa.Table
    loaded_at: datetime
    """When it was read, in UTC, as the page shows it."""
    version: int
    fresh_until: float
    """The cache's clock past which it is read again."""
    kept_until: float
    """The cache's clock past which it is dropped, even where a reload fails."""


class Cache:
    """The values of one app, in its process, at most `max_mb` megabytes of them."""

    def __init__(self, max_mb: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_bytes = max_mb * MEGABYTE
        self.clock = clock
        """Seconds, which only ever grow: a test sets its own."""
        self._entries: OrderedDict[Key, Entry] = OrderedDict()
        self._bytes: int = 0
        self._flights: dict[Key, asyncio.Task[Entry]] = {}
        self._latest: dict[Key, Key] = {}
        """A view's last key, by the key without its inputs' versions."""
        self._versions = itertools.count(1)

    @property
    def bytes(self) -> int:
        """The bytes its values hold."""
        return self._bytes

    def __len__(self) -> int:
        return len(self._entries)

    def fresh(self, found: Key) -> Entry | None:
        """The value at `found` while it is fresh, now the most recently used."""
        entry = self.kept(found)
        return entry if entry is not None and self.clock() < entry.fresh_until else None

    def kept(self, found: Key) -> Entry | None:
        """The value at `found`, fresh or past its TTL, while it is kept."""
        entry = self._entries.get(found)
        if entry is None:
            return None
        if self.clock() >= entry.kept_until:
            self._drop(found)
            return None
        self._entries.move_to_end(found)
        return entry

    def put(self, found: Key, table: pa.Table, *, loaded_at: datetime, ttl_s: float, stale_s: float, replaces: Key | None = None) -> Entry:
        """Keep `table` at `found`, loaded at `loaded_at`, fresh for `ttl_s` and kept `stale_s` past that, evicting the
        least recently used values past the ceiling; the entry, kept or not. `replaces` is what the key shares with
        the values it outdates, a view's key without its inputs' versions, whose last value is dropped."""
        now = self.clock()
        entry = Entry(table, loaded_at, next(self._versions), now + ttl_s, now + ttl_s + stale_s)
        if replaces is not None:
            if (outdated := self._latest.get(replaces)) is not None:
                self._drop(outdated)
            self._latest[replaces] = found
        self._drop(found)
        if table.nbytes > self.max_bytes:
            log.warning(
                "%s is %.1f MB, more than the cache holds, so it is never kept",
                found[0],
                table.nbytes / MEGABYTE,
                extra={"dataset": found[0], "bytes": table.nbytes, "cache_mb": self.max_bytes // MEGABYTE},
            )
            return entry
        self._entries[found] = entry
        self._bytes += table.nbytes
        while self._bytes > self.max_bytes:
            evicted = next(iter(self._entries))
            log.info("evicted %s from the cache", evicted[0], extra={"dataset": evicted[0], "bytes": self._entries[evicted].table.nbytes})
            self._drop(evicted)
        return entry

    def forget(self, found: Key) -> None:
        """Drop the value at `found`, so the next load reads it again."""
        self._drop(found)

    def flight(self, found: Key, start: Callable[[], asyncio.Task[Entry]]) -> tuple[asyncio.Task[Entry], bool]:
        """The load of `found` under way, and whether this caller joined it rather than started it: callers that ask at
        once share one. It ends itself once done."""
        if (running := self._flights.get(found)) is not None:
            return running, True
        task = start()
        self._flights[found] = task
        task.add_done_callback(lambda _: self._flights.pop(found, None))
        return task, False

    def _drop(self, found: Key) -> None:
        if (entry := self._entries.pop(found, None)) is not None:
            self._bytes -= entry.table.nbytes
