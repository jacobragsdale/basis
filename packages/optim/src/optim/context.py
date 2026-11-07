"""The process's connections, opened once per command or call: the CLI opens one, and so does a run from Python.
Every source, the cache and the orders table are wired here and nowhere else. A database is a source once basis-core's
settings configure it and optim's give its limits; a source the environment does not configure is not opened, and a
dataset that reads from it fails when it loads, naming the settings it needs."""

import threading
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass

from basis.core.databases import open_database
from basis.core.storage import Root
from optim import orders
from optim.data.cache import FileCache
from optim.data.loader import Loader
from optim.orders import Orders
from optim.settings import Settings
from optim.sources import Source, SourceName, rest
from optim.sources.database import DatabaseSource
from optim.sources.file import FileSource


@dataclass(frozen=True)
class Context:
    loader: Loader
    orders: Orders | None
    """The orders table, where the environment configures one and the caller writes orders."""


@contextmanager
def open_context(settings: Settings, *, cache: bool = True, orders_table: bool = True) -> Generator[Context]:
    """Everything closes on exit. `cache=False`, or no cache root, reads every dataset live; `orders_table=False`
    leaves the orders table closed, whatever the environment configures, as a run from Python does. Opening a database
    connects to nothing: each read opens its own connection, in a slot of the database's own."""
    with ExitStack() as stack:
        sources: dict[SourceName, Source] = {"file": FileSource()}
        limits: dict[SourceName, threading.Semaphore] = {}
        for name, limit in settings.optim.databases.items():
            sources[name] = DatabaseSource(open_database(settings, name), settings.database(name).timeout_s)
            limits[name] = threading.Semaphore(limit.max_concurrent)
        if (api := settings.optim.rest) is not None:
            sources["rest"] = rest.RestSource(stack.enter_context(rest.client(api)), api.max_rows)
            limits["rest"] = threading.Semaphore(api.max_concurrent)
        table = orders.connect(settings, settings.optim.orders) if settings.optim.orders is not None and orders_table else None
        yield Context(Loader(sources, open_cache(settings) if cache else None, limits), table)


def open_cache(settings: Settings) -> FileCache | None:
    """The dataset cache, or None without `BASIS_OPTIM__CACHE_ROOT`."""
    return FileCache(Root.at(settings.optim.cache_root)) if settings.optim.cache_root is not None else None
