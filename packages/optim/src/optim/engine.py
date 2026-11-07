"""Running one piece of work per portfolio, in this process or a process pool.

Workers only compute. The parent loads the data, ships the run's state once to each worker process through the pool
initializer, sends each a portfolio id, and writes the outputs. Workers hold no connections, and moving from one
machine to a cluster (Ray, Dask) changes only this module.
"""

import logging
import multiprocessing
import os
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from functools import partial
from typing import cast

from optim import logs

log = logging.getLogger(__name__)

# `spawn` on every platform: Windows has no other start method, and one start method means a laptop behaves as production
# does. Each worker imports numpy, pandas and cvxpy itself, which adds a second or two to a run that starts a pool.
_PROCESSES = multiprocessing.get_context("spawn")
# Workers share the cores, so each gets a one-thread BLAS: 11% more portfolios a minute at 8 workers on 8 cores. A
# spawned worker reads it from the environment it inherits; this process loaded its BLAS before.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")


def each_portfolio[S, R](work: Callable[[S, str], R], state: S, portfolio_ids: Sequence[str], *, workers: int, failed: Callable[[str, str], R]) -> list[R]:
    """`work(state, portfolio_id)` for each portfolio, in a pool of up to `workers` processes; results in the order
    given. `work` is a module-level function, which catches and logs its own errors; `failed` makes the result of a
    portfolio whose work never returned, as when its process died."""
    workers = min(workers, len(portfolio_ids))
    # one worker, the common intraday case, works in this process: no pool start-up cost
    if workers <= 1:
        return [_settle(portfolio_id, partial(work, state, portfolio_id), failed) for portfolio_id in portfolio_ids]
    results = _in_pool(work, state, portfolio_ids, workers, failed)
    if lost := [i for i, result in enumerate(results) if result is None]:
        log.warning("a worker process died, as when the kernel kills one that runs out of memory; working on its %d unfinished portfolios in a new pool", len(lost))
        for i, result in zip(lost, _in_pool(work, state, [portfolio_ids[i] for i in lost], workers, failed), strict=True):
            results[i] = result
    if lost := [portfolio_ids[i] for i, result in enumerate(results) if result is None]:
        log.error("a worker process died again, so %d portfolios have no result; give the run more memory or fewer OPTIM_WORKERS", len(lost), extra={"examples": lost[:20]})
    return [failed(portfolio_id, "error: its worker process died") if result is None else result for portfolio_id, result in zip(portfolio_ids, results, strict=True)]


def _in_pool[S, R](work: Callable[[S, str], R], state: S, portfolio_ids: Sequence[str], workers: int, failed: Callable[[str, str], R]) -> list[R | None]:
    """Work in a new pool; None for a portfolio lost when the pool broke."""
    # one machine; for a cluster, ship `state` once (ray.put / client.scatter) and submit the same ids, each carrying
    # `logs.bound()`, since a cluster's workers outlive a run
    with ProcessPoolExecutor(workers, mp_context=_PROCESSES, initializer=_start_worker, initargs=(partial(work, state), logs.bound(), logs.included())) as pool:
        futures = [cast("Future[R]", pool.submit(_in_worker, portfolio_id)) for portfolio_id in portfolio_ids]  # what `work` returns
        return [None if isinstance(future.exception(), BrokenProcessPool) else _settle(i, future.result, failed) for i, future in zip(portfolio_ids, futures, strict=True)]


def _settle[R](portfolio_id: str, outcome: Callable[[], R], failed: Callable[[str, str], R]) -> R:
    """One portfolio's failure must not block the rest: what reaches here failed between processes, as when a result
    cannot be pickled, since `work` catches its own errors."""
    try:
        return outcome()
    except Exception as error:
        log.exception("could not decide %s", portfolio_id, extra={"portfolio_id": portfolio_id})
        return failed(portfolio_id, f"error: {error!r}")


_work: Callable[[str], object] | None = None  # this worker process's work, with the run's state, set once by the initializer


def _start_worker(work: Callable[[str], object], fields: dict[str, object], packages: list[str]) -> None:
    global _work  # noqa: PLW0603 -- the ProcessPoolExecutor initializer pattern: one copy per worker, not per task
    _work = work
    logs.setup()  # a spawned worker starts without the parent's logging; it reads the same OPTIM_LOG_* variables
    logs.bind(**fields)  # nor its bound fields, such as the run's id
    for package in packages:
        logs.include(package)


def _in_worker(portfolio_id: str) -> object:
    if _work is None:
        msg = "worker started without its work"
        raise RuntimeError(msg)
    return _work(portfolio_id)
