"""The process pool a service's compute functions run in, and what a handler awaits to run one (see `compute.py`).

The pool's processes start with the `spawn` method, as optim's do, so a desktop behaves as production does, and the
app's lifespan starts every one of them, each importing the compute modules, before the app reports ready. A call
crosses to a process by pickling: the function by its name, its arguments and its result by value. The request's log
fields cross with it, so what the function logs carries the request's id.

A call has what is left of its route's deadline, its wait for a free process included. One that misses it answers 504:
a call still waiting is taken off the queue, and one that has started runs on in its process until it returns, its
result dropped and its end logged; shutdown terminates the processes while one still runs. A process that dies, as
when the system stops one that runs out of memory, fails the calls it had with a 500. The pool then starts again for
the next call, and a call made while it is broken, before it knew, starts it again and runs on the new one, since it
had not begun."""

import asyncio
import logging
import multiprocessing
import pickle
import signal
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Protocol

from opentelemetry.trace import Tracer

from basis.api import deadlines
from basis.api.compute import ComputeFunction
from basis.core import logs

log = logging.getLogger(__name__)
_SPAWN = multiprocessing.get_context("spawn")
"""On every platform: Windows has no other start method, and one method means a desktop behaves as production does."""
_SETTLE_S = 5
"""Seconds shutdown waits, once it has terminated the processes, for the calls they ran to fail and be logged."""


class Runs(Protocol):
    """Where a compute function runs: the pool, or this process where a test fakes it."""

    async def run(self, declared: ComputeFunction[..., object], args: tuple[object, ...], kwargs: Mapping[str, object], limit: deadlines.Limit) -> object: ...


class Call:
    """A compute function, as `api.Compute` gives it to a handler."""

    def __init__(self, declared: ComputeFunction[..., object], runs: Runs, tracer: Tracer) -> None:
        self._declared = declared
        self._runs = runs
        self._tracer = tracer

    async def __call__(self, *args: object, **kwargs: object) -> object:
        name = self._declared.name
        limit = deadlines.limit(f"compute {name}", None)
        with self._tracer.start_as_current_span(f"compute {name}", attributes={"basis.compute": name, "basis.deadline_s": limit.seconds}):
            if limit.seconds <= 0:
                raise limit.exceeded()
            return await self._runs.run(self._declared, args, kwargs, limit)


class Pool:
    """The service's processes, which run its compute functions."""

    def __init__(self, workers: int, functions: Sequence[ComputeFunction[..., object]]) -> None:
        self._workers = workers
        self._functions = tuple(declared.function for declared in functions)
        self._executor = self._start()
        self._outliving: set[asyncio.Task[None]] = set()
        """Calls that missed their deadlines while they ran, which shutdown stops."""

    @classmethod
    async def open(cls, workers: int, functions: Sequence[ComputeFunction[..., object]]) -> Pool:
        """A pool whose every process has started and imported the compute modules."""
        pool = cls(workers, functions)
        try:
            await asyncio.gather(*(asyncio.wrap_future(pool._executor.submit(_started)) for _ in range(workers)))  # each submit starts a process while none is free
        except BaseException:
            await pool.close()
            raise
        log.info("the compute pool's %s processes have started", workers, extra={"workers": workers})
        return pool

    def _start(self) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(self._workers, mp_context=_SPAWN, initializer=_start, initargs=(logs.included(), self._functions))

    async def run(self, declared: ComputeFunction[..., object], args: tuple[object, ...], kwargs: Mapping[str, object], limit: deadlines.Limit) -> object:
        started = time.monotonic()
        executor = self._executor
        try:
            future: Future[object] = executor.submit(_call, declared.function, logs.bound(), args, kwargs)
        except BrokenProcessPool:  # broken before this call, which has not begun: start the pool again, and run it there
            self._restart(executor)
            executor = self._executor
            future = executor.submit(_call, declared.function, logs.bound(), args, kwargs)
        waiting = asyncio.wrap_future(future)
        deadline = asyncio.timeout(limit.seconds)
        try:
            async with deadline:
                return await asyncio.shield(waiting)
        except TimeoutError as error:
            if not deadline.expired():
                raise
            self._abandon(future, waiting, declared.name, started)
            raise limit.exceeded() from error
        except asyncio.CancelledError:
            self._abandon(future, waiting, declared.name, started)
            raise
        except BrokenProcessPool as error:
            self._restart(executor)
            msg = f"a process of the compute pool stopped while it ran {declared.name}, as when the system stops one that runs out of memory: the next calls start new ones"
            raise RuntimeError(msg) from error

    def _restart(self, broken: ProcessPoolExecutor) -> None:
        """Start a new pool in place of `broken`, unless another call has already."""
        if broken is self._executor:
            log.error("a process of the compute pool stopped, so the pool starts again", extra={"workers": self._workers})
            broken.shutdown(wait=False, cancel_futures=True)
            self._executor = self._start()

    def _abandon(self, future: Future[object], waiting: asyncio.Future[object], name: str, started: float) -> None:
        """Take a call nobody waits for now off the queue, or, where it has started, log how it ends."""
        if future.cancel():
            return

        async def ended() -> None:
            await asyncio.wait([waiting])
            failed = None if waiting.cancelled() else waiting.exception()  # retrieved, so the loop logs no error nobody read
            log.warning(
                "compute %s ended %.1fs after it started, which was past its deadline; its result is dropped", name, time.monotonic() - started, extra={"compute": name, "failed": failed is not None}
            )

        task = asyncio.get_running_loop().create_task(ended())
        self._outliving.add(task)
        task.add_done_callback(self._outliving.discard)

    async def close(self) -> None:
        """Stop the processes: at once where a call that missed its deadline still runs, since nobody waits for it, and
        otherwise once each has finished what it was given."""
        executor = self._executor
        if self._outliving:
            log.warning("stopping the compute pool's processes, which still run %s calls past their deadlines", len(self._outliving), extra={"calls": len(self._outliving)})
            executor.terminate_workers()  # before any shutdown, which forgets the processes this would stop
            await asyncio.wait(self._outliving, timeout=_SETTLE_S)  # each call's end, which the terminated processes fail, is logged
        await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)


class InProcess:
    """A fake's runs: on a thread of this process, with what crosses to the pool pickled and unpickled as it would be,
    so a test still catches a value that cannot cross. The deadline bounds the wait, as the pool's does."""

    async def run(self, declared: ComputeFunction[..., object], args: tuple[object, ...], kwargs: Mapping[str, object], limit: deadlines.Limit) -> object:
        function, crossed_args, crossed_kwargs = pickle.loads(pickle.dumps((declared.function, args, dict(kwargs))))  # noqa: S301 -- made here, from the handler's own values
        deadline = asyncio.timeout(limit.seconds)
        try:
            async with deadline:
                result: object = await asyncio.to_thread(_crossing, function, crossed_args, crossed_kwargs)
        except TimeoutError as error:
            if deadline.expired():
                raise limit.exceeded() from error
            raise
        return result


def _crossing(function: Callable[..., object], args: tuple[object, ...], kwargs: Mapping[str, object]) -> object:
    return pickle.loads(pickle.dumps(function(*args, **kwargs)))  # noqa: S301 -- the function's own result, made here


def _start(included: Sequence[str], functions: Sequence[Callable[..., object]]) -> None:
    """A process's start: it logs as the app does, and unpickling `functions` has imported their modules. Ctrl+C,
    which a terminal sends every process of the command, is the app's to act on: it stops the pool when it stops."""
    del functions
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    logs.setup()  # a spawned process starts with none of the app's logging; it reads the same BASIS_LOG_* variables
    for package in included:
        logs.include(package)


def _started() -> None:
    """Nothing: a process that returns it has started."""


def _call(function: Callable[..., object], fields: Mapping[str, object], args: tuple[object, ...], kwargs: Mapping[str, object]) -> object:
    with logs.fields(**fields):
        return function(*args, **kwargs)
