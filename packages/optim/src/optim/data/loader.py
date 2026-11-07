"""The `Loader`, which fetches datasets (`optim.data.dataset`) through their sources, validates them and caches them
(`optim.data.cache`). Loading is raw acquisition: a frame holds what its source returned, validated against its row model and nothing
more. Combining frames into what a solve sees is the model's prep."""

import hashlib
import json
import logging
import sys
import threading
import time
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from functools import partial
from itertools import batched
from typing import Literal, Self

import pandas as pd

from optim.contract import LISTING
from optim.data.cache import Cache
from optim.data.dataset import Dataset
from optim.data.frames import empty_frame
from optim.logs import brief, ms_since, timed, to_json, utc_now
from optim.schema import Model
from optim.sources import Param, Source, SourceName
from optim.sources.sql import read_sql

log = logging.getLogger(__name__)
_stop: ContextVar[threading.Event | None] = ContextVar("_stop", default=None)  # set when a call in a `TaskGroup` fails


class Skipped(Exception):  # noqa: N818 -- not an error: the call was not made
    """A load that never reached its source, because another load in the same `TaskGroup` had already failed."""


class TaskGroup:
    """Runs each submitted call in a thread and waits for them all on exit, like asyncio's TaskGroup. The first failure
    stops the rest: loads still waiting for a source slot are skipped, and those already running end at their source's
    timeout. Then the failure raises. Nested groups share the stop, so a failed batch also stops the market loading
    beside it."""

    def __init__(self) -> None:
        self._stop = _stop.get() or threading.Event()
        self._errors: list[BaseException] = []
        # a thread per call, mostly waiting on a source's slot; fine to thousands of batches, a queue beyond
        self._pool = ThreadPoolExecutor(max_workers=sys.maxsize)

    def submit[**P, T](self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> Future[T]:
        def run() -> T:
            _stop.set(self._stop)  # the pool is this group's alone, so its threads never carry another group's stop
            try:
                return fn(*args, **kwargs)
            except BaseException as error:
                self._errors.append(error)
                self._stop.set()
                raise

        return self._pool.submit(run)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self._pool.shutdown()
        if self._errors:
            raise next((e for e in self._errors if not isinstance(e, Skipped)), self._errors[0])  # the cause, not the loads it skipped


class DatasetRead(Model):
    """How a run got one dataset, as its snapshot's `run.json` records it."""

    source: str
    """Where it was read from: a source's name, or for a file, `file:` and its path."""
    read: Literal["live", "hit", "miss", "refresh"]
    """`live`: read from its source, as it is not cached; `hit`: from the cache; `miss`: read from its source, then
    cached; `refresh`: read from its source by `optim warm`."""
    fetched_at: datetime
    """When its source was read: for a hit, when the cache entry was filled. Loaded in batches, the oldest batch's."""


class Reads:
    """Where each dataset of a run came from, as its loads finish. Thread-safe: they finish concurrently."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_dataset: dict[str, DatasetRead] = {}

    def add(self, dataset: str, read: DatasetRead) -> None:
        """A dataset loaded in batches keeps its oldest read."""
        with self._lock:
            if (seen := self._by_dataset.get(dataset)) is None or read.fetched_at < seen.fetched_at:
                self._by_dataset[dataset] = read

    def by_dataset(self) -> dict[str, DatasetRead]:
        """By dataset name, so a run's `run.json` lists them in the same order whichever load finished first."""
        with self._lock:
            return dict(sorted(self._by_dataset.items()))


@dataclass(frozen=True)
class Loader:
    """Fetches datasets through their sources, reading and filling the cache as each dataset's policy says. Without a
    cache, every dataset is read live. Thread-safe."""

    sources: Mapping[SourceName, Source]
    cache: Cache | None = None
    limits: Mapping[SourceName, threading.Semaphore] = field(default_factory=dict)  # calls each source may serve at once; none: unbounded
    refresh: bool = False  # set by `warm`: read live, overwrite the cache, and fail if the cache does
    reads: Reads | None = None  # set for a run: where each load got its data, for the run's snapshot
    clock: Callable[[], datetime] = utc_now  # stamps each source read, and judges how old a cache entry is

    def load(self, dataset: Dataset, **params: Param) -> pd.DataFrame:
        try:
            with timed(log, "loaded", dataset=dataset.name, params=brief(params)) as fields:
                frame, read = self._read(dataset, params, fields)
                fields["cache"], fields["rows"] = read.read, len(frame)
        except Exception as error:
            error.add_note(f"while loading dataset {dataset.name!r} ({dataset.query}) from {dataset.source} with {to_json(brief(params))}")
            raise
        if self.reads is not None:
            self.reads.add(dataset.name, read)
        return frame

    def _read(self, dataset: Dataset, params: Mapping[str, Param], fields: dict[str, object]) -> tuple[pd.DataFrame, DatasetRead]:
        """The frame, and where it came from. A source read adds `wait_ms` to `fields`: the time spent waiting for a
        slot."""
        ttl, cache = dataset.cache, self.cache
        source = f"file:{dataset.query}" if dataset.source == "file" else dataset.source
        if ttl is None or cache is None:
            frame, fetched_at = self._fetch(dataset, params, fields)
            return frame, DatasetRead(source=source, read="live", fetched_at=fetched_at)
        key = _cache_key(dataset, params)
        if self.refresh:
            frame, fetched_at = self._fetch(dataset, params, fields)
            if frame.empty:
                msg = f"{dataset.name} returned no rows; an empty entry would hide the data for {ttl} once it lands"
                raise ValueError(msg)
            cache.put(key, frame, fetched_at)
            return frame, DatasetRead(source=source, read="refresh", fetched_at=fetched_at)
        if (hit := self._cached(dataset, partial(cache.get, key))) is not None and self.clock() - hit[1] < ttl:
            frame, fetched_at = hit
            return frame, DatasetRead(source=source, read="hit", fetched_at=fetched_at)
        frame, fetched_at = self._fetch(dataset, params, fields)
        if not frame.empty:  # usually a feed that has not landed yet: cached, it would hide the data for a whole ttl
            self._cached(dataset, partial(cache.put, key, frame, fetched_at))
        return frame, DatasetRead(source=source, read="miss", fetched_at=fetched_at)

    def _cached[T](self, dataset: Dataset, call: Callable[[], T]) -> T | None:
        """A cache read or write that cannot fail the run, since the cache only saves time: an error, such as a corrupt
        entry or storage that cannot be reached, is logged and reads as a miss, and the live read then overwrites a bad
        entry."""
        try:
            return call()
        except Exception:
            log.warning("could not use the cache for %s; reading it live", dataset.name, exc_info=True)
        return None

    def warm(self, datasets: Iterable[Dataset], as_of_date: date) -> bool:
        """`optim warm`: read each of `datasets`, read whole, that is cached live for `as_of_date`, and overwrite its
        entry, so the day's runs read it from the cache. A failed load is logged and the rest still land; returns
        whether all of them did."""
        loader = replace(self, refresh=True)
        with ThreadPoolExecutor() as pool:
            futures = {pool.submit(loader.load, d, as_of_date=as_of_date): d.name for d in datasets if d.cache}
        failed = [(name, error) for future, name in futures.items() if (error := future.exception())]
        for name, error in failed:
            log.error("could not warm %s", name, exc_info=error)
        log.info("warmed %d of %d datasets", len(futures) - len(failed), len(futures))
        return not failed

    def load_all(self, *datasets: Dataset, **params: Param) -> dict[str, pd.DataFrame]:
        """Load independent datasets concurrently, keyed by dataset name."""
        with TaskGroup() as group:
            futures = {dataset.name: group.submit(self.load, dataset, **params) for dataset in datasets}
        return {name: future.result() for name, future in futures.items()}

    @contextmanager
    def read_only(self, source: SourceName) -> Generator[Loader]:
        """A loader whose loads from `source` share one read-only transaction, until the block ends, so reads that
        depend on each other, such as a portfolio's holdings and its cash flows, see the source at one moment. The
        transaction holds one of the source's slots throughout, so open connections never exceed its limit; its loads
        take turns on the connection, from whichever threads call them."""
        start = time.perf_counter()
        with self.limits.get(source) or nullcontext():
            _skip_if_stopped()
            with _configured(self.sources, source).read_only() as transaction:
                log.info("began read-only transaction", extra={"source": source, "wait_ms": ms_since(start)})
                yield replace(self, sources={**self.sources, source: transaction}, limits={k: v for k, v in self.limits.items() if k != source})

    def _fetch(self, dataset: Dataset, params: Mapping[str, Param], fields: dict[str, object]) -> tuple[pd.DataFrame, datetime]:
        """The frame, and when the source was asked for it, once a slot was free."""
        start = time.perf_counter()
        with self.limits.get(dataset.source) or nullcontext():
            fields["wait_ms"] = ms_since(start)
            _skip_if_stopped()
            fetched_at = self.clock()
            columns = _configured(self.sources, dataset.source, reader=dataset.name).fetch(str(dataset.query), params)
        with timed(log, "validated", dataset=dataset.name):
            return dataset.to_frame(columns), fetched_at


def _configured(sources: Mapping[SourceName, Source], name: SourceName, *, reader: str = "a dataset read per portfolio") -> Source:
    """The source, or an error that names the settings it needs."""
    if (source := sources.get(name)) is None:
        msg = f"{reader} reads from {name}, which is not configured: set OPTIM_{name.upper()}__* (see .env.example)"
        raise LookupError(msg)
    return source


def _skip_if_stopped() -> None:
    if (stop := _stop.get()) and stop.is_set():
        raise Skipped


def _cache_key(dataset: Dataset, params: Mapping[str, Param]) -> str:
    """Changes whenever the params, the SQL or the row schema change, so a deploy never reads what older code cached."""
    query = read_sql(str(dataset.query)) if dataset.source in ("oracle", "mssql") else str(dataset.query)  # a REST path is keyed as given
    payload = json.dumps([json.dumps(dataset.row.model_json_schema(), sort_keys=True), query, params], sort_keys=True, default=str)
    return f"{dataset.name}/{hashlib.sha256(payload.encode()).hexdigest()[:16]}"  # no `:`, which Windows refuses in a file name


BATCH = 1000  # portfolios per query


def load_data(loader: Loader, datasets: Mapping[str, Dataset], as_of_date: date, portfolio_ids: Sequence[str] | None) -> tuple[dict[str, pd.DataFrame], dict[str, DatasetRead], list[str]]:
    """Every dataset a model reads, by name, all loading at once: those read whole for `as_of_date`, and those read per
    portfolio in batches, each batch's datasets from one source in one read-only transaction, so a portfolio's holdings
    and the rest of its rows are from one moment. The model's `portfolios` dataset, by its attribute in `datasets`, is
    read whole; without `portfolio_ids`, it loads first, and the portfolios it lists are the ones loaded. Also returns
    where each dataset came from, for the run's snapshot, and the portfolios loaded."""
    listing = datasets.get(LISTING)
    whole = [dataset for attribute, dataset in datasets.items() if attribute == LISTING or not dataset.per_portfolio]
    per_portfolio = [dataset for attribute, dataset in datasets.items() if attribute != LISTING and dataset.per_portfolio]
    reads = Reads()
    loader = replace(loader, reads=reads)  # this run's loads alone
    listed: dict[str, pd.DataFrame] = {}
    if portfolio_ids is None:
        if listing is None:
            msg = "give the portfolios to decide: the model does not list its own, in a dataset declared as `portfolios`"
            raise ValueError(msg)
        with TaskGroup() as group:  # in a thread, as every load is: the main thread waits in Python, where a stop signal reaches it
            first = group.submit(loader.load, listing, as_of_date=as_of_date)
        listed[listing.name] = first.result()
        portfolio_ids, whole = list(dict.fromkeys(listed[listing.name]["portfolio_id"])), [dataset for dataset in whole if dataset is not listing]
    with timed(log, "loaded data", as_of_date=as_of_date, portfolios=len(portfolio_ids)), TaskGroup() as group:
        market = group.submit(loader.load_all, *whole, as_of_date=as_of_date)
        batches = [group.submit(_load_batch, loader, per_portfolio, batch, as_of_date) for batch in batched(portfolio_ids, BATCH, strict=False)]
    frames = listed | market.result() | {dataset.name: _concat(dataset, [batch.result()[dataset.name] for batch in batches]) for dataset in per_portfolio}
    return frames, reads.by_dataset(), list(portfolio_ids)


def _load_batch(loader: Loader, datasets: Sequence[Dataset], portfolios: tuple[str, ...], as_of_date: date) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for source in dict.fromkeys(dataset.source for dataset in datasets):
        with loader.read_only(source) as scoped:
            frames |= {dataset.name: scoped.load(dataset, as_of_date=as_of_date, portfolios=portfolios) for dataset in datasets if dataset.source == source}
    return frames


def _concat(dataset: Dataset, frames: list[pd.DataFrame]) -> pd.DataFrame:
    if not frames:
        return empty_frame(dataset.row)
    # each batch's key is checked as it loads; a query that ignores its batch's ids repeats rows across batches
    return dataset.unique(pd.concat(frames, ignore_index=True), why=" across batches: does the query filter on its batch's ids?")
