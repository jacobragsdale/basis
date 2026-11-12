"""Loading a job's inputs, in the process that runs it, before the transform sees any of them. Every query on one
database runs in one transaction that only reads, so the job sees that database at one moment, as the database's
isolation allows; databases load at once, each on its own connection, and so do the GETs of the job's fetches, each
within its deadline. Each frame is conformed to its row as it arrives, and an empty one fails the run unless its
dataset allows it. Every statement and every GET has a span in the run's trace, each thread running in a copy of the
run's context.

A run on files (`data=DIR`) reads each dataset that has a file in `DIR`, `NAME.csv` or `NAME.parquet`, instead of its
source, which is how a laptop and the contract tests run on a fixture; a dataset with no file there is read from its
source as usual. No run falls back from a source to a file."""

import asyncio
import contextvars
import io
import json
import logging
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Never

import httpx2
import pandas as pd
from opentelemetry.trace import Tracer

from basis.core import folders, mongo
from basis.core.databases import Database
from basis.core.errors import BadReply
from basis.core.interval import Interval
from basis.core.logs import ms_since
from basis.core.opened import HttpClient, Waits
from basis.core.snapshots import InputRecord
from basis.core.storage import Root
from basis.job.inputs import Dataset, Excel, Fetch, File, Mongo, Query
from basis.job.rows import Row, conform

log = logging.getLogger(__name__)


class EmptyInputError(ValueError):
    pass


type Reach = Callable[[str], tuple[httpx2.AsyncClient, Waits]]
"""The client of the service a fetch names, as the settings or a fake reach it, and how it waits between attempts: the
system's waits, or the fake's, which take no time."""


def load(
    datasets: Mapping[str, Dataset],
    interval: Interval | None,
    *,
    open_database: Callable[[str], Database],
    reach: Reach,
    tracer: Tracer,
    data: Path | None,
    open_folder: folders.Opener | None = None,
    open_mongo: MongoOpener | None = None,
) -> tuple[dict[str, pd.DataFrame], dict[str, InputRecord]]:
    """Every dataset's frame and where it came from, by name."""
    files = {name: found for name in datasets if data is not None and (found := _data_file(data, name)) is not None}
    frames: dict[str, pd.DataFrame] = {}
    records: dict[str, InputRecord] = {}
    for name, path in files.items():
        frames[name], records[name] = _read_file(datasets[name], str(path), source=f"data: {path}")
    by_database: dict[str, dict[str, Query]] = {}
    fetches: dict[str, Fetch] = {}
    for name, dataset in datasets.items():
        if name in files:
            continue
        match dataset:
            case Query():
                by_database.setdefault(dataset.database, {})[name] = dataset
            case Fetch():
                fetches[name] = dataset
            case File() | Excel() | Mongo():
                frames[name], records[name] = _read_one(name, dataset, interval, open_folder, open_mongo)
            case _:
                msg = f"{name} is a {type(dataset).__name__}, which nothing reads"
                raise TypeError(msg)
    with ThreadPoolExecutor(max_workers=max(len(by_database) + bool(fetches), 1)) as pool:
        futures = [pool.submit(_copied(_read_database, open_database(database), queries, interval)) for database, queries in by_database.items()]
        if fetches:
            futures.append(pool.submit(_copied(lambda: asyncio.run(_fetch_all(fetches, interval, reach, tracer)))))
    for future in futures:
        loaded = future.result()
        frames |= {name: frame for name, (frame, _) in loaded.items()}
        records |= {name: record for name, (_, record) in loaded.items()}
    if empty := sorted(name for name, frame in frames.items() if not len(frame) and datasets[name].empty == "fail"):
        msg = f"{', '.join(empty)} returned no rows, which their datasets do not allow (empty='fail'): has the data landed?"
        raise EmptyInputError(msg)
    return {name: frames[name] for name in datasets}, {name: records[name] for name in datasets}


def _copied[**A, T](work: Callable[A, T], *args: A.args, **kwargs: A.kwargs) -> Callable[[], T]:
    """`work`, to run on another thread in a copy of this context, so that its spans are the run's and its records
    carry the run's fields."""
    context = contextvars.copy_context()
    return lambda: context.run(work, *args, **kwargs)


def _read_database(database: Database, queries: Mapping[str, Query], interval: Interval | None) -> dict[str, tuple[pd.DataFrame, InputRecord]]:
    loaded: dict[str, tuple[pd.DataFrame, InputRecord]] = {}
    with database.reading() as reader:
        for name, query in queries.items():
            start = time.perf_counter()
            what = f"dataset {name} ({query.sql})"
            try:
                bound = interval.in_zone(query.zone) if interval is not None and query.zone is not None else interval
                frame = conform(reader.fetch(query.text(), bound, what=what), query.row, what=what, naive="in_zone")
            except Exception as error:
                error.add_note(f"while loading {what} from database {database.name}")
                raise
            record = InputRecord(source=query.describe(), rows=len(frame), read_ms=ms_since(start))
            log.info("loaded", extra={"dataset": name, "source": record.source, "rows": record.rows, "ms": record.read_ms})
            loaded[name] = (frame, record)
    return loaded


async def _fetch_all(fetches: Mapping[str, Fetch], interval: Interval | None, reach: Reach, tracer: Tracer) -> dict[str, tuple[pd.DataFrame, InputRecord]]:
    """Each fetch's GET, at once, on one client for each service."""
    clients: dict[str, tuple[httpx2.AsyncClient, Waits]] = {}
    try:
        for fetch in fetches.values():
            if fetch.http not in clients:
                clients[fetch.http] = reach(fetch.http)
        async with asyncio.TaskGroup() as group:
            tasks = {
                name: group.create_task(_fetch(name, fetch, interval, HttpClient(fetch.resource, clients[fetch.http][0], tracer, waits=clients[fetch.http][1]))) for name, fetch in fetches.items()
            }
    except ExceptionGroup as failed:
        raise failed.exceptions[0] from None  # the first fetch that failed, as a database's load names its one dataset; the others are cancelled
    finally:
        for client, _ in clients.values():
            await client.aclose()
    return {name: task.result() for name, task in tasks.items()}


async def _fetch(name: str, fetch: Fetch, interval: Interval | None, client: HttpClient) -> tuple[pd.DataFrame, InputRecord]:
    """The fetch's rows, or the kind of `ServiceError` its GET failed with, as basis-core's client makes it, or
    `BadReply` for a reply that is not rows of the fetch's row, which `json_rows` says why of."""
    start = time.perf_counter()
    what = f"dataset {name} ({fetch.describe()})"
    try:
        location = fetch.location(interval)
        response = await client.request("GET", location)
        try:
            frame = json_rows(response.content, fetch.row, what=what)
        except (ValueError, TypeError) as error:
            msg = f"{fetch.http} answered GET {location} with a body that is not rows of {fetch.row.__name__}"
            raise BadReply(msg, resource=fetch.http, status=response.status_code, reason=str(error)) from error
    except Exception as error:
        error.add_note(f"while loading {what}")
        raise
    record = InputRecord(source=fetch.describe(), rows=len(frame), read_ms=ms_since(start))
    log.info("loaded", extra={"dataset": name, "source": record.source, "rows": record.rows, "ms": record.read_ms})
    return frame, record


def json_rows(content: bytes, row: type[Row], *, what: str) -> pd.DataFrame:
    """A JSON list of objects as `row`'s frame: each value as its text, as a CSV's arrives, so that a number keeps the
    digits it was sent with, and `conform` converts each value as it converts a CSV's. A field the row has no column
    for, a value that is an object or a list, and `NaN` or `Infinity`, which Python reads and JSON has not, are
    refused; a field an object leaves out is null. This and `conform` are the one place a fetch's rows become a frame."""

    def refuse(constant: str) -> Never:
        msg = f"{what} has {constant}, which is no JSON number: send a value its row reads, or null"
        raise ValueError(msg)

    parsed: object = json.loads(content, parse_float=str, parse_int=str, parse_constant=refuse)
    entries: list[dict[str, object]] = []
    for entry in parsed if isinstance(parsed, list) else [parsed]:
        if not isinstance(parsed, list) or not isinstance(entry, dict):
            msg = f"{what} is not a JSON list of objects, one per row"
            raise TypeError(msg)
        entries.append({str(key): value for key, value in entry.items()})
    names = row.names()
    if unknown := sorted({key for entry in entries for key in entry} - set(names)):
        msg = f"{what} has fields its row has no column for: {', '.join(unknown)}"
        raise ValueError(msg)
    columns = {name: pd.Series([_text(entry.get(name), what=what, name=name) for entry in entries], dtype=_TEXT) for name in names}
    return conform(pd.DataFrame(columns), row, what=what, naive="in_zone")


def _text(value: object, *, what: str, name: str) -> str | None:
    match value:
        case None | str():
            return value
        case bool():
            return "true" if value else "false"
        case _:
            msg = f"{what} has an object or a list in {name}, and a row's values are flat"
            raise TypeError(msg)


def _read_file(dataset: Dataset, location: str, *, source: str) -> tuple[pd.DataFrame, InputRecord]:
    start = time.perf_counter()
    what = f"dataset {dataset.name} ({location})"
    try:
        root = Root.at(location)
        raw = _read_csv(root.read_bytes()) if location.endswith(".csv") else root.read_frame()
        frame = conform(raw, dataset.row, what=what, naive="in_zone")
    except Exception as error:
        error.add_note(f"while loading {what}")
        raise
    record = InputRecord(source=source, rows=len(frame), read_ms=ms_since(start))
    log.info("loaded", extra={"dataset": dataset.name, "source": source, "rows": record.rows, "ms": record.read_ms})
    return frame, record


def _read_sheet(dataset: Excel, folder: folders.Folder, location: str) -> tuple[pd.DataFrame, InputRecord]:
    start = time.perf_counter()
    what = f"dataset {dataset.name} ({dataset.folder}: {location} [{dataset.sheet}])"
    try:
        frame = conform(folder.read_sheet(location, dataset.sheet), dataset.row, what=what, naive="in_zone")
    except Exception as error:
        error.add_note(f"while loading {what}")
        raise
    record = InputRecord(source=f"{dataset.folder}: {location} [{dataset.sheet}]", rows=len(frame), read_ms=ms_since(start))
    log.info("loaded", extra={"dataset": dataset.name, "source": record.source, "rows": record.rows, "ms": record.read_ms})
    return frame, record


def _read_one(name: str, dataset: File | Excel | Mongo, interval: Interval | None, open_folder: folders.Opener | None, open_mongo: MongoOpener | None) -> tuple[pd.DataFrame, InputRecord]:
    """A dataset read on its own, on this thread: a file, a workbook's sheet, or a Mongo collection's documents."""
    match dataset:
        case File():
            return _read_file(dataset, dataset.location(interval), source=f"file: {dataset.path}")
        case Excel():
            if open_folder is None:
                msg = f"{name} reads a workbook in the folder {dataset.folder}, and the run was given no folders"
                raise TypeError(msg)
            return _read_sheet(dataset, open_folder(dataset.folder), dataset.location(interval))
        case Mongo():
            if open_mongo is None:
                msg = f"{name} reads the Mongo database {dataset.mongo}, and the run was given none"
                raise TypeError(msg)
            return _read_mongo(dataset, *open_mongo(dataset.mongo), interval)


type MongoOpener = Callable[[str], tuple[mongo.Store, int]]
"""Each Mongo database a run names, with the most documents a read of it may return."""


def _read_mongo(dataset: Mongo, store: mongo.Store, max_documents: int, interval: Interval | None) -> tuple[pd.DataFrame, InputRecord]:
    start = time.perf_counter()
    what = f"dataset {dataset.name} ({dataset.mongo}: {dataset.collection})"
    try:
        documents = mongo.read(store, dataset.collection, dataset.query(interval), max_documents=max_documents, name=dataset.mongo)
        frame = conform(mongo.frame_of(documents, dataset.row.columns), dataset.row, what=what, naive="in_zone")
    except Exception as error:
        error.add_note(f"while loading {what}")
        raise
    record = InputRecord(source=f"{dataset.mongo}: {dataset.collection}", rows=len(frame), read_ms=ms_since(start))
    log.info("loaded", extra={"dataset": dataset.name, "source": record.source, "rows": record.rows, "ms": record.read_ms})
    return frame, record


def _read_csv(content: bytes) -> pd.DataFrame:
    """A CSV's columns as their text, as a fetch's arrive, for `conform` to type by the dataset's row: nothing is
    inferred, and only an empty field is null, never text such as `NA` or `null`. Every line after the header is a
    row, a blank one too, which in a CSV of one column is a null; the header is read as written, so a name it repeats
    is one `conform` refuses, where pandas would rename it `NAME.1`."""
    read = pd.read_csv(io.BytesIO(content), header=None, dtype=_TEXT, keep_default_na=False, na_values=[""], skip_blank_lines=False)
    frame = read.iloc[1:].reset_index(drop=True)
    frame.columns = pd.Index([str(name) for name in read.iloc[0]] if len(read) else [], dtype=object)
    return frame


_TEXT = "string[pyarrow]"
"""Text, before `conform` types it: pandas' Arrow-backed strings, whose null is pandas' NA."""


def unfiled(datasets: Mapping[str, Dataset], data: Path) -> list[str]:
    """The datasets that have no file in `data`, so that a run on it reads them from their sources."""
    return [name for name in datasets if _data_file(data, name) is None]


def _data_file(data: Path, name: str) -> Path | None:
    found = [path for path in (data / f"{name}.csv", data / f"{name}.parquet") if path.exists()]
    if len(found) > 1:
        msg = f"{data} has both {name}.csv and {name}.parquet: keep one"
        raise ValueError(msg)
    return found[0] if found else None
