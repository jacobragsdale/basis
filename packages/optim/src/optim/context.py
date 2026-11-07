"""The process's connections, opened once per command or call: the CLI opens one, and so does a run from Python.
Every source, the cache and the orders table are wired here and nowhere else. A database is a source once basis-core's
settings configure it and optim's give its limits; a source the environment does not configure is not opened, and a
dataset that reads from it fails when it loads, naming the settings it needs. The orders table opens only for a
command that publishes, which fails without one."""

import threading
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass

from basis.core.databases import open_database
from basis.core.settings import SettingsError
from basis.core.storage import Root
from optim import orders
from optim.data.cache import FileCache
from optim.data.loader import Loader
from optim.orders import Orders, OrdersSettings
from optim.settings import Settings
from optim.sources import Source, SourceName, rest
from optim.sources.database import DatabaseSource
from optim.sources.file import FileSource


@dataclass(frozen=True)
class Context:
    loader: Loader
    orders: Orders | None
    """The orders table, where the caller publishes."""


@contextmanager
def open_context(settings: Settings, *, cache: bool = True, publish: bool = False) -> Generator[Context]:
    """Everything closes on exit. `cache=False`, or no cache root, reads every dataset live; `publish` opens the orders
    table, which the environment must configure, and without it the table stays closed whatever the environment
    configures, as for a run from Python. Opening a database connects to nothing: each read opens its own connection,
    in a slot of the database's own."""
    with ExitStack() as stack:
        sources: dict[SourceName, Source] = {"file": FileSource()}
        limits: dict[SourceName, threading.Semaphore] = {}
        for name, limit in settings.optim.databases.items():
            sources[name] = DatabaseSource(open_database(settings, name), settings.database(name).timeout_s)
            limits[name] = threading.Semaphore(limit.max_concurrent)
        if (api := settings.optim.rest) is not None:
            sources["rest"] = rest.RestSource(stack.enter_context(rest.client(api)), api.max_rows)
            limits["rest"] = threading.Semaphore(api.max_concurrent)
        yield Context(Loader(sources, open_cache(settings) if cache else None, limits), open_orders(settings) if publish else None)


def open_orders(settings: Settings) -> Orders:
    """The orders table the environment configures, for a command that publishes."""
    return orders.connect(settings, orders_settings(settings))


def orders_settings(settings: Settings) -> OrdersSettings:
    """The orders table the environment configures, which publishing and `basis ddl MODEL` need."""
    if (configured := settings.optim.orders) is None:
        msg = "no orders table is configured, which publishing and `basis ddl MODEL` need: set BASIS_OPTIM__ORDERS__DATABASE and BASIS_OPTIM__ORDERS__TABLE, as production does"
        raise SettingsError(msg)
    return configured


def open_cache(settings: Settings) -> FileCache | None:
    """The dataset cache, or None without `BASIS_OPTIM__CACHE_ROOT`."""
    return FileCache(Root.at(settings.optim.cache_root)) if settings.optim.cache_root is not None else None
