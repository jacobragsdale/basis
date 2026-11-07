"""Loading a job's inputs, in the process that runs it, before the transform sees any of them. Every query on one
database runs in one transaction that only reads, so the job sees that database at one moment, as the database's
isolation allows; databases load at once, each on its own connection. Each frame is conformed to its row as it
arrives, and an empty one fails the run unless its dataset allows it.

A run on files (`data=DIR`) reads each dataset that has a file in `DIR`, `NAME.csv` or `NAME.parquet`, instead of its
source, which is how a laptop and the contract tests run on a fixture; a dataset with no file there is read from its
source as usual. No run falls back from a source to a file."""

import io
import logging
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import polars as pl

from etl_framework.databases import Database
from etl_framework.inputs import Dataset, File, Query
from etl_framework.interval import Interval
from etl_framework.logs import ms_since
from etl_framework.rows import conform
from etl_framework.snapshots import InputRecord
from etl_framework.storage import Root

log = logging.getLogger(__name__)


class EmptyInputError(ValueError):
    pass


def load(datasets: Mapping[str, Dataset], interval: Interval | None, *, open_database: Callable[[str], Database], data: Path | None) -> tuple[dict[str, pl.DataFrame], dict[str, InputRecord]]:
    """Every dataset's frame and where it came from, by name."""
    files = {name: found for name in datasets if data is not None and (found := _data_file(data, name)) is not None}
    frames: dict[str, pl.DataFrame] = {}
    records: dict[str, InputRecord] = {}
    for name, path in files.items():
        frames[name], records[name] = _read_file(datasets[name], str(path), source=f"data: {path}")
    by_database: dict[str, dict[str, Query]] = {}
    for name, dataset in datasets.items():
        if name in files:
            continue
        match dataset:
            case Query():
                by_database.setdefault(dataset.database, {})[name] = dataset
            case File():
                frames[name], records[name] = _read_file(dataset, dataset.location(interval), source=f"file: {dataset.path}")
            case _:
                msg = f"{name} is a {type(dataset).__name__}, which nothing reads"
                raise TypeError(msg)
    with ThreadPoolExecutor(max_workers=max(len(by_database), 1)) as pool:
        futures = {database: pool.submit(_read_database, open_database(database), queries, interval) for database, queries in by_database.items()}
    for future in futures.values():
        loaded = future.result()
        frames |= {name: frame for name, (frame, _) in loaded.items()}
        records |= {name: record for name, (_, record) in loaded.items()}
    if empty := sorted(name for name, frame in frames.items() if not frame.height and datasets[name].empty == "fail"):
        msg = f"{', '.join(empty)} returned no rows, which their datasets do not allow (empty='fail'): has the data landed?"
        raise EmptyInputError(msg)
    return {name: frames[name] for name in datasets}, {name: records[name] for name in datasets}


def _read_database(database: Database, queries: Mapping[str, Query], interval: Interval | None) -> dict[str, tuple[pl.DataFrame, InputRecord]]:
    loaded: dict[str, tuple[pl.DataFrame, InputRecord]] = {}
    with database.reading() as reader:
        for name, query in queries.items():
            start = time.perf_counter()
            what = f"dataset {name} ({query.sql})"
            try:
                frame = conform(reader.fetch(query.text(), interval, what=what), query.row, what=what, naive="in_zone")
            except Exception as error:
                error.add_note(f"while loading {what} from database {database.name}")
                raise
            record = InputRecord(source=query.describe(), rows=frame.height, read_ms=ms_since(start))
            log.info("loaded", extra={"dataset": name, "source": record.source, "rows": record.rows, "ms": record.read_ms})
            loaded[name] = (frame, record)
    return loaded


def _read_file(dataset: Dataset, location: str, *, source: str) -> tuple[pl.DataFrame, InputRecord]:
    start = time.perf_counter()
    what = f"dataset {dataset.name} ({location})"
    try:
        root = Root.at(location)
        raw = pl.read_csv(io.BytesIO(root.read_bytes()), infer_schema=False) if location.endswith(".csv") else root.read_frame()
        frame = conform(raw, dataset.row, what=what, naive="in_zone")
    except Exception as error:
        error.add_note(f"while loading {what}")
        raise
    record = InputRecord(source=source, rows=frame.height, read_ms=ms_since(start))
    log.info("loaded", extra={"dataset": dataset.name, "source": source, "rows": record.rows, "ms": record.read_ms})
    return frame, record


def _data_file(data: Path, name: str) -> Path | None:
    found = [path for path in (data / f"{name}.csv", data / f"{name}.parquet") if path.exists()]
    if len(found) > 1:
        msg = f"{data} has both {name}.csv and {name}.parquet: keep one"
        raise ValueError(msg)
    return found[0] if found else None
