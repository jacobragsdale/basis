"""What a built app opens for its datasets, which `bd.load` (`basis.dash.loads`) reaches while the app runs: each
database's port, basis-core's or a fake's, read through its read login alone in a transaction that only reads, and its
`anyio.CapacityLimiter`; each folder; the SQL each query runs, read when the app is built; and the cache
(`basis.dash.cache`). Opening connects to nothing. Fakes stand in for every resource the app lists, or none, so a fake
never meets a real database. `loads.py`'s docstring says what a load does."""

import asyncio
import io
import logging
import math
import time
from collections.abc import Hashable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

import anyio
import httpx2
import orjson
import pandas as pd
import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq
from opentelemetry.trace import Tracer

from basis.core import fakes as core_fakes
from basis.core import frames, opened, traces
from basis.core.databases import Bind, Statements, open_database
from basis.core.errors import BadReply, ServiceError, Timeout, Unavailable
from basis.core.folders import Folder as OpenFolder
from basis.core.folders import folder as open_folder
from basis.core.lake import Lake as OpenLake
from basis.core.lake import lake as open_lake
from basis.core.opened import HttpClient
from basis.core.resources import HttpResource
from basis.core.rows import conform
from basis.core.settings import MssqlSettings, SettingsError, variable
from basis.dash import loads
from basis.dash.cache import Cache, Entry, Key, key
from basis.dash.declarations import RESOURCES, Database, Dataset, Fetch, File, Folder, Lake, Query, Service, View
from basis.dash.errors import DeclarationError
from basis.dash.loads import LoadError
from basis.dash.settings import Settings

log = logging.getLogger(__name__)


class DatabasePort(Protocol):
    """What a database reaches: basis-core's database, or a fake, such as basis-core's `fakes.Database`."""

    def statements(self, *, writes: bool) -> AbstractContextManager[Statements]: ...


@dataclass
class _Database:
    declared: Database
    port: DatabasePort
    limiter: anyio.CapacityLimiter | None = None
    """Made when the app starts, inside its event loop."""


class Runtime:
    """What a built app opened for its datasets: each database's port and limiter, each folder, the SQL each query
    runs, and the cache. Opening connects to nothing; the app's lifespan starts it, and `bd.load` finds the one the
    process runs."""

    def __init__(self, app_name: str, *, datasets: Sequence[Dataset], resources: Sequence[object], settings: Settings, tracer: Tracer, fakes: Mapping[Any, object] | None) -> None:
        self.app = app_name
        self.datasets = {id(dataset): dataset for dataset in datasets}
        self.tracer = tracer
        self.cache = Cache(settings.cache_mb)
        self.sql = {dataset.name: dataset.sql.read_text(encoding="utf-8") for dataset in datasets if isinstance(dataset, Query)}
        self._databases: dict[int, _Database] = {}
        self._folders: dict[int, OpenFolder] = {}
        self._lakes: dict[int, OpenLake] = {}
        self._services: dict[int, tuple[HttpClient, httpx2.AsyncClient]] = {}
        fakes = {key.name if isinstance(key, RESOURCES) else key: fake for key, fake in fakes.items()} if fakes is not None else None
        _check_fakes(app_name, resources, fakes)
        for resource in resources:
            given = fakes.get(resource.name) if fakes is not None and isinstance(resource, RESOURCES) else None
            if isinstance(resource, Database):
                self._databases[id(resource)] = _Database(resource, _port(resource, given, settings))
            elif isinstance(resource, Folder):
                self._folders[id(resource)] = _folder(resource, given, settings)
            elif isinstance(resource, Lake):
                self._lakes[id(resource)] = _lake(resource, given, settings)
            elif isinstance(resource, Service):
                self._services[id(resource)] = _service(resource, given, settings, tracer)

    def start(self) -> None:
        """Start: each database's limiter, in the running loop, and this the process's runtime."""
        for database in self._databases.values():
            database.limiter = anyio.CapacityLimiter(database.declared.max_concurrent)
        loads.running = self

    def stop(self) -> None:
        if loads.running is self:
            loads.running = None

    async def close(self) -> None:
        """Close each service's client, once the app has stopped."""
        for _, client in self._services.values():
            await client.aclose()

    async def load(self, dataset: Dataset, params: Mapping[str, Hashable]) -> pd.DataFrame:
        return frames.from_arrow((await self.fetch(dataset, params)).table)

    async def fetch(self, dataset: Dataset, params: Mapping[str, Hashable]) -> Entry:
        """`dataset`'s value for `params`, fresh from the cache or read: one span, one log record, and its status."""
        started = time.perf_counter()
        shown = loads.shown(params)
        with self.tracer.start_as_current_span(f"load {dataset.name}", attributes={"basis.dataset": dataset.name, **{f"basis.param.{name}": str(value) for name, value in shown.items()}}) as span:
            stale = False
            if isinstance(dataset, View):
                inputs = await asyncio.gather(*(self.fetch(given, {name: params[name] for name in given.params}) for given in dataset.inputs.values()))
                stale = any(entry.stale for entry in inputs)
                found = key(dataset.name, {**params, **{f"{name} version": entry.version for name, entry in zip(dataset.inputs, inputs, strict=True)}})
                read = lambda: self._view(dataset, params, inputs, found)  # noqa: E731 -- the read the flight starts
            else:
                found = key(dataset.name, params)
                read = lambda: self._read_or_stale(dataset, params, found)  # noqa: E731 -- the read the flight starts
            entry, how = self.cache.fresh(found), "hit"
            if entry is None:
                loop = asyncio.get_running_loop()
                task, joined = self.cache.flight(found, lambda: loop.create_task(read()))
                entry, how = await asyncio.shield(task), "shared" if joined else "miss"
            if stale and not entry.stale:
                entry = replace(entry, stale=True)  # a view of a stale input is stale
            rows = entry.table.num_rows
            span.set_attributes({"basis.rows": rows, "basis.cache": how, "basis.stale": entry.stale})
        ms = round((time.perf_counter() - started) * 1000, 3)
        log.info("loaded %s", dataset.name, extra={"dataset": dataset.name, "params": shown, "rows": rows, "ms": ms, "cache": how, "stale": entry.stale})
        loads.report(dataset.name, entry.loaded_at, shown, rows=rows, stale=entry.stale)
        return entry

    def reload(self, dataset: Dataset, params: Mapping[str, Hashable]) -> bool:
        found = key(dataset.name, params)
        entry = self.cache.kept(found)
        floor = dataset.reload_s if isinstance(dataset, Query | File) else None
        if entry is not None and floor is not None and self.cache.clock() - entry.read_at < floor:
            log.info("kept %s, read %.0f s ago, less than its reload_s", dataset.name, self.cache.clock() - entry.read_at, extra={"dataset": dataset.name, "reload_s": floor})
            return False
        self.cache.forget(found)
        log.info("dropped %s, so the next load reads it", dataset.name, extra={"dataset": dataset.name, "params": loads.shown(params)})
        return True

    def loaded(self, dataset: Dataset, params: Mapping[str, Hashable]) -> datetime | None:
        entry = self.cache.kept(key(dataset.name, params)) if not isinstance(dataset, View) else None
        return entry.loaded_at if entry is not None else None

    def deadline(self, resource: str) -> float | None:
        """The deadline of the database `resource` names, or None for another resource."""
        return next((database.declared.deadline_s for database in self._databases.values() if database.declared.name == resource), None)

    async def _read_or_stale(self, dataset: Dataset, params: Mapping[str, Hashable], found: Key) -> Entry:
        """`_read`, or, where it fails with a resource's failure inside the dataset's `stale_s`, the last good value,
        marked stale."""
        try:
            return await self._read(dataset, params, found)
        except ServiceError as error:
            if (kept := self.cache.kept(found)) is None:
                raise
            log.warning(
                "%s could not be read again (%s), so its last good value answers, read at %s",
                dataset.name,
                error.detail,
                kept.loaded_at.isoformat(),
                extra={"dataset": dataset.name, "kind": type(error).__name__, "resource": error.resource, "reason": error.reason},
            )
            return replace(kept, stale=True)

    async def _read(self, dataset: Dataset, params: Mapping[str, Hashable], found: Key) -> Entry:
        loaded_at = datetime.now(UTC)
        if isinstance(dataset, Query):
            table = await self._query(dataset, params)
        elif isinstance(dataset, File):
            table = await anyio.to_thread.run_sync(self._file, dataset, params)
        elif isinstance(dataset, Fetch):
            table = await self._fetch(dataset, params)
        else:
            msg = f"{dataset.name} is a {dataset.kind}, which no load reads"
            raise LoadError(msg)
        ttl_s, stale_s = (dataset.ttl_s, dataset.stale_s) if isinstance(dataset, Query | File | Fetch) else (0.0, 0.0)
        return self.cache.put(found, table, loaded_at=loaded_at, ttl_s=ttl_s, stale_s=stale_s)

    async def _view(self, dataset: View, params: Mapping[str, Hashable], inputs: Sequence[Entry], found: Key) -> Entry:
        """`dataset`'s function of its inputs' frames and its own params, in a worker thread, its frame conformed to its
        row; kept while its inputs are the versions it was computed from, as old as its oldest input."""
        given = {name: params[name] for name in dataset.arguments}

        def compute() -> pa.Table:
            made = dataset.function(**{name: frames.from_arrow(entry.table) for name, entry in zip(dataset.inputs, inputs, strict=True)}, **given)
            if not isinstance(made, pd.DataFrame):
                msg = f"view {dataset.name}'s function returned a {type(made).__qualname__}: return a pandas frame of its row's columns"
                raise TypeError(msg)
            return frames.to_arrow(conform(made, dataset.row, what=f"view {dataset.name}", naive="reject"))

        with traces.tracing(self.tracer):
            table = await anyio.to_thread.run_sync(compute)
        return self.cache.put(found, table, loaded_at=min(entry.loaded_at for entry in inputs), ttl_s=math.inf, stale_s=0.0, replaces=_unversioned(found))

    async def _query(self, dataset: Query, params: Mapping[str, Hashable]) -> pa.Table:
        database = self._databases[id(dataset.database)]
        declared = database.declared
        sql, tracer = self.sql[dataset.name], self.tracer
        binds: dict[str, Bind] = {name: value for name, value in params.items() if isinstance(value, str | float | int | bool | date)}

        def read() -> pa.Table:
            with traces.tracing(tracer), database.port.statements(writes=False) as statements:
                table = statements.query(sql, binds, timeout_s=declared.deadline_s)
            return frames.to_arrow(conform(frames.from_arrow(table), dataset.row, what=f"query {dataset.name}", naive="in_zone"))

        try:
            with anyio.fail_after(declared.deadline_s):
                return await anyio.to_thread.run_sync(read, limiter=database.limiter, abandon_on_cancel=True)
        except TimeoutError as error:
            msg = f"database {declared.name} did not answer query {dataset.name} within its deadline of {declared.deadline_s:g}s"
            raise Timeout(msg, resource=declared.name) from error

    def _file(self, dataset: File, params: Mapping[str, Hashable]) -> pa.Table:
        path = dataset.path.format(**loads.shown(params))
        with traces.tracing(self.tracer):
            if isinstance(dataset.folder, Lake):
                store = self._lakes[id(dataset.folder)]
                where = f"{store.container}/{path}"
                try:
                    frame = frames.from_arrow(pq.read_table(where, filesystem=store.fs))
                except (OSError, pa.ArrowException) as error:
                    msg = f"lake {store.name} could not give {store.where(path)}"
                    raise Unavailable(msg, resource=store.name, reason=" ".join(str(error).split())) from error
            else:
                opened = self._folders[id(dataset.folder)]
                if dataset.sheet is not None:
                    frame = opened.read_sheet(path, dataset.sheet)
                else:
                    content = opened.read_bytes(path)
                    if path.lower().endswith(".parquet"):
                        frame = frames.from_arrow(pq.read_table(io.BytesIO(content)))
                    else:  # every field as text, which the row types, so nothing is inferred
                        frame = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False, na_filter=False)
        return frames.to_arrow(conform(frame, dataset.row, what=f"file {dataset.name}", naive="in_zone"))

    async def _fetch(self, dataset: Fetch, params: Mapping[str, Hashable]) -> pa.Table:
        client, _ = self._services[id(dataset.service)]
        query = loads.shown(params)
        response = await client.request("GET", dataset.path, query=dict(query) or None, attributes={"basis.dataset": dataset.name})

        def rows() -> pa.Table:
            try:
                frame = json_rows(response.content, dataset.rows_at)
            except ValueError as error:
                msg = f"{dataset.service.name} answered GET {dataset.path} with a body that is not rows of {dataset.row.__name__}"
                raise BadReply(msg, resource=dataset.service.name, status=response.status_code, reason=str(error)) from error
            return frames.to_arrow(conform(frame, dataset.row, what=f"fetch {dataset.name}", naive="in_zone"))

        return await anyio.to_thread.run_sync(rows)


def _unversioned(found: Key) -> Key:
    """A view's key without its inputs' versions: what each of its values for its params shares."""
    name, params = found
    return name, tuple((param, value) for param, value in params if not param.endswith(" version"))


def json_rows(content: bytes, rows_at: str | None) -> pd.DataFrame:
    """A JSON reply's rows, a list of objects, at the dotted `rows_at` or the reply itself, as a frame of each value's
    text, as a CSV's arrives, so the row types it; a value that is an object or a list, or a row that is no object, is
    refused, and a field an object leaves out is null."""
    found: object = orjson.loads(content)
    for part in rows_at.split(".") if rows_at is not None else ():
        found = found.get(part) if isinstance(found, dict) else None
    if not isinstance(found, list):
        msg = f"the reply holds no list of rows{f' at {rows_at}' if rows_at is not None else ''}"
        raise ValueError(msg)  # noqa: TRY004 -- a reply's shape, which the caller makes a BadReply
    columns: dict[str, list[str | None]] = {}
    for position, entry in enumerate(found):
        if not isinstance(entry, dict):
            msg = f"row {position} is a {type(entry).__name__}, not an object"
            raise ValueError(msg)  # noqa: TRY004 -- as above
        for name, value in entry.items():
            if isinstance(value, dict | list):
                msg = f"row {position}'s {name} is a JSON {type(value).__name__}, which no column holds"
                raise ValueError(msg)  # noqa: TRY004 -- as above
            columns.setdefault(str(name), [None] * position)
        for name, values in columns.items():
            value = entry.get(name)
            values.append(None if value is None else ("true" if value is True else "false" if value is False else str(value)))
    return frames.from_arrow(pa.table({name: pa.array(values, pa.string()) for name, values in columns.items()}) if columns else pa.table({}))


def _check_fakes(app_name: str, resources: Sequence[object], fakes: Mapping[Any, object] | None) -> None:
    """Fakes for every resource or for none, so a fake never meets a real database."""
    if fakes is None:
        return
    names = {resource.name for resource in resources if isinstance(resource, RESOURCES)}
    if unfaked := sorted(names - set(fakes)):
        msg = f"app {app_name} is given fakes, and none for {', '.join(unfaked)}: give a fake of every resource, since a fake never meets a real one"
        raise DeclarationError(msg)
    if unknown := sorted(set(fakes) - names):
        msg = f"app {app_name} is given fakes of {', '.join(unknown)}, which it does not list in its resources: fake its own alone"
        raise DeclarationError(msg)


def _port(declared: Database, fake: object, settings: Settings) -> DatabasePort:
    if fake is not None:
        if not callable(getattr(fake, "statements", None)):
            msg = f"the fake of database {declared.name} is a {type(fake).__qualname__}: give one with statements(writes=...), as basis-core's fakes.Database"
            raise DeclarationError(msg)
        if (dialect := getattr(fake, "dialect", declared.dialect)) != declared.dialect:
            msg = f"the fake of database {declared.name} is {dialect}, and the database is declared {declared.dialect}: make the fake's dialect the database's"
            raise DeclarationError(msg)
        return fake  # ty: ignore[invalid-return-type] -- checked to have statements, as a port's
    found = settings.database(declared.name)
    kind = "mssql" if isinstance(found, MssqlSettings) else "oracle"
    if kind != declared.dialect:
        msg = f"database {declared.name} is declared {declared.dialect}, and its settings are {kind}'s: set its settings for the database its SQL is written for, or declare dialect={kind!r}"
        raise SettingsError(msg)
    return open_database(settings, declared.name)


def _folder(declared: Folder, fake: object, settings: Settings) -> OpenFolder:
    if fake is None:
        return open_folder(declared.name, settings)
    if isinstance(fake, OpenFolder):
        return fake
    if isinstance(fake, Path | str):
        return OpenFolder(declared.name, Path(fake))
    msg = f"the fake of folder {declared.name} is a {type(fake).__qualname__}: give the path of a folder of test files"
    raise DeclarationError(msg)


def _lake(declared: Lake, fake: object, settings: Settings) -> OpenLake:
    if fake is None:
        return open_lake(declared.name, settings)
    if isinstance(fake, OpenLake):
        return fake
    if isinstance(fake, Path | str):
        return OpenLake(declared.name, str(Path(fake).resolve()), pafs.LocalFileSystem())
    msg = f"the fake of lake {declared.name} is a {type(fake).__qualname__}: give the path of a folder that stands in for its container"
    raise DeclarationError(msg)


def _service(declared: Service, fake: object, settings: Settings, tracer: Tracer) -> tuple[HttpClient, httpx2.AsyncClient]:
    resource = HttpResource(declared.name, declared.deadline_s)
    if fake is None:
        if (found := settings.http.get(declared.name)) is None:
            msg = f"the service {declared.name} is not configured, and the app reads it: set {variable(('http', declared.name, 'url'))}, as .env.example shows"
            raise SettingsError(msg)
        client = opened.client(declared.name, found)
        return HttpClient(resource, client, tracer), client
    if isinstance(fake, core_fakes.Http):
        client = fake.client(declared.name)
        return HttpClient(resource, client, tracer, waits=fake.waits), client
    msg = f"the fake of service {declared.name} is a {type(fake).__qualname__}: give basis-core's fakes.Http, of an ASGI app that answers as the service does"
    raise DeclarationError(msg)
