"""`await bd.load(DATASET, **params)`, the one way a page gets data, and what the running app opened for it.

    @callback(Output("positions", "rowData"), Input("as-of", "value"))
    async def show(as_of: str) -> bd.Rows:
        positions = await bd.load(POSITIONS, as_of=date.fromisoformat(as_of))
        return bd.rows(positions)

A load:

- **leaves the event loop**: a callback that is not `async` runs on the loop's own thread on Dash's FastAPI backend, so
  one slow query would hold every page. The query runs in `anyio.to_thread.run_sync`, under its database's own
  `anyio.CapacityLimiter(max_concurrent)`, never a bare executor, so Dash's `ctx` and `set_props` hold inside it
  (spike 14.1, question 7). `bd.load` called from a function that is not `async`, or in a thread with no loop, raises
  `LoadError`, whose fix is to make the callback `async` and await it;
- **is bounded** by its database's `deadline_s`, from when it is asked for, its wait for a free thread included, which
  the driver's own timeout bounds too; past it, it raises basis-core's `Timeout`;
- **answers from the cache** while its value is fresh (`basis.dash.cache`), and callers that ask for one key at once
  share one query; a caller that goes away leaves the query to finish for the rest;
- **gives exactly its declared params**, each of its declared type, or raises `LoadError` naming what to give;
- **reads**: through its database's read login, in a transaction that only reads, and never a writer;
- **conforms** what it read to its row, by basis-core's `conform`, and keeps it as Arrow, handing each caller a fresh
  frame;
- **is one span and one log record**, `loaded DATASET`, with its dataset, params, rows, milliseconds and how the cache
  answered (`hit`, `miss`, or `shared`, a query another caller started);
- **reports into the page's status** (`basis.dash.ids.STATUS`), inside a callback: `data[DATASET]` becomes
  `{"loaded_at": ISO 8601 UTC, "rows": N, "stale": false, "params": {NAME: VALUE}}`, a date as ISO text, merged into
  what the store holds through a `Patch`, so the store keeps every other dataset's."""

import asyncio
import inspect
from collections.abc import Awaitable, Hashable, Mapping
from datetime import date, datetime
from typing import Protocol

import pandas as pd

import dash
from basis.core.errors import BasisError
from basis.dash import internals
from basis.dash.declarations import Dataset, File, Query
from basis.dash.ids import STATUS


class LoadError(BasisError, RuntimeError):
    """A load asked for in a way basis refuses; the message ends with the fix."""


class Running(Protocol):
    """The app the process serves, as `bd.load` reaches it (`basis.dash.runtime.Runtime`)."""

    @property
    def app(self) -> str: ...

    @property
    def datasets(self) -> Mapping[int, Dataset]: ...

    def load(self, dataset: Dataset, params: Mapping[str, Hashable]) -> Awaitable[pd.DataFrame]: ...

    def deadline(self, resource: str) -> float | None:
        """The deadline of the database `resource` names, for a failure's sentence."""
        ...

    def reload(self, dataset: Dataset, params: Mapping[str, Hashable]) -> bool: ...

    def loaded(self, dataset: Dataset, params: Mapping[str, Hashable]) -> datetime | None: ...


running: Running | None = None
"""The runtime of the app the process serves, from its start to its stop."""


def load(dataset: Dataset, /, **params: object) -> Awaitable[pd.DataFrame]:
    """`dataset`'s frame for `params`, from the cache while it is fresh, read off the event loop otherwise: await it in
    an `async` callback."""
    _awaited_on_the_loop(dataset)
    if running is None:
        msg = f"{dataset.name} is loaded while no app is running: load it in a callback of a started app, or in a test inside bd.testing.client(app)"
        raise LoadError(msg)
    return running.load(dataset, checked(running, dataset, params))


def reload(dataset: Dataset, /, **params: object) -> bool:
    """Drop `dataset`'s value for `params`, so the next load reads it, unless it was read less than its `reload_s`
    ago; whether it was dropped."""
    if not isinstance(dataset, Query | File):
        msg = f"{dataset.name} is a {dataset.kind}, which is computed from its inputs: reload each of them"
        raise LoadError(msg)
    if dataset.reload_s is None:
        msg = f"{dataset.name} declares no reload_s, so a person cannot ask for it again: declare reload_s, the fewest seconds between two reloads, as reload_s=60"
        raise LoadError(msg)
    app = _app(dataset)
    return app.reload(dataset, checked(app, dataset, params))


def loaded(dataset: Dataset, /, **params: object) -> datetime | None:
    """When the value of `dataset` a load would answer for `params` was read, in UTC, or None where it has not been."""
    app = _app(dataset)
    return app.loaded(dataset, checked(app, dataset, params))


def _app(dataset: Dataset) -> Running:
    if running is None:
        msg = f"{dataset.name} is asked for while no app is running: ask in a callback of a started app, or in a test inside bd.testing.client(app)"
        raise LoadError(msg)
    return running


def checked(app: Running, dataset: Dataset, given: Mapping[str, object]) -> dict[str, Hashable]:
    """`given`, checked against `dataset`'s declared params: each named, of its type, and no other."""
    if id(dataset) not in app.datasets:
        msg = f"{dataset.name} is not a dataset of app {app.app}: list every resource it reads in the app's resources"
        raise LoadError(msg)
    declared = dataset.params
    wanted = ", ".join(f"{name}: {kind.__name__}" for name, kind in declared.items()) or "none"
    if missing := sorted(set(declared) - set(given)):
        msg = f"{dataset.name} is loaded without {', '.join(missing)}: give each of its params ({wanted}), as bd.load({dataset.name.upper()}, {missing[0]}=...)"
        raise LoadError(msg)
    if extra := sorted(set(given) - set(declared)):
        msg = f"{dataset.name} takes no param {', '.join(extra)}: give its params alone ({wanted})"
        raise LoadError(msg)
    return {name: _typed(dataset.name, name, declared[name], value) for name, value in given.items()}


def _awaited_on_the_loop(dataset: Dataset) -> None:
    """Refuse a load that is not awaited on the event loop: from a function that is not `async`, which runs on the
    loop's thread and would hold it, or from a thread with no loop."""
    fix = f"make the callback async and await the load, as `frame = await bd.load({dataset.name.upper()}, ...)`"
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        msg = f"{dataset.name} is loaded in a thread with no event loop: {fix}"
        raise LoadError(msg) from None
    frame = inspect.currentframe()
    caller = frame.f_back.f_back if frame is not None and frame.f_back is not None else None
    if caller is not None and not caller.f_code.co_flags & (inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR):
        msg = f"{dataset.name} is loaded in {caller.f_code.co_name}, which is not async, so it would hold the event loop and every page: {fix}"
        raise LoadError(msg)


def _typed(dataset: str, name: str, kind: type, value: object) -> Hashable:
    if kind is float and type(value) is int:
        return float(value)
    if type(value) is not kind:
        example = _EXAMPLES.get(kind, kind.__name__)
        msg = f"{dataset}'s param {name} is a {type(value).__name__}, {value!r}: give a {kind.__name__}, as {example}"
        raise LoadError(msg)
    return value if isinstance(value, Hashable) else str(value)


_EXAMPLES: dict[type, str] = {date: "date.fromisoformat(text)", float: "float(text)", int: "int(text)", bool: "True", str: "str(value)"}


def shown(params: Mapping[str, Hashable]) -> dict[str, str | float | int | bool]:
    """Params as JSON holds them: a date as ISO text."""
    return {name: value.isoformat() if isinstance(value, date) else value if isinstance(value, str | float | int | bool) else str(value) for name, value in params.items()}


def report(name: str, loaded_at: datetime, params: Mapping[str, object], *, rows: int, stale: bool = False) -> None:
    """Tell the page's status store what it should know of this load, inside a callback."""
    if not internals.in_callback():
        return
    patch = internals.updated(STATUS, "data")
    if not isinstance(patch, dash.Patch):
        patch = dash.Patch()
    patch[name] = {"loaded_at": loaded_at.isoformat().replace("+00:00", "Z"), "rows": rows, "stale": stale, "params": dict(params)}
    dash.set_props(STATUS, {"data": patch})
