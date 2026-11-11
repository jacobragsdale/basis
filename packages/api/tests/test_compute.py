"""Compute functions: what their declarations refuse, and how the process pool runs them, off the event loop, within
their routes' deadlines. The functions these tests run live in a `compute` module the tests write to a temporary
folder on `sys.path`, which the pool's spawned processes inherit; the pool here has one or two processes."""

import importlib
import json
import os
import signal
import sys
import textwrap
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from pathlib import Path
from types import ModuleType
from typing import Annotated

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from basis import api
from basis.api import fakes
from basis.api.example import compute as example
from basis.core.settings import SettingsError

WORKLOADS = '''
import logging
import os
import threading
import time
from pathlib import Path

from basis import api

type Amount = float


def spin(seconds: float) -> int:
    """Busy for `seconds` of CPU, as a solver is: it never sleeps."""
    turns, end = 0, time.perf_counter() + seconds
    while time.perf_counter() < end:
        turns += 1
    return turns


def tell(value: int) -> int:
    logging.getLogger("workloads").warning("computing %s", value)
    return value


def die() -> int:
    os._exit(1)


def mark(path: str, seconds: float) -> int:
    """Spins, then writes a file: the file is there only if the call ran to its end."""
    turns = spin(seconds)
    Path(path).write_text("finished", encoding="utf-8")
    return turns


def pid() -> int:
    return os.getpid()


def hold(started: str, release: str) -> int:
    """Busy, as a solver is, from writing `started` until `release` exists, which the test writes."""
    Path(started).write_text("started", encoding="utf-8")
    turns = 0
    while not Path(release).exists():
        turns += 1
    return turns


def scale(amount: Amount) -> Amount:
    return amount * 2


def identity(value: object) -> object:
    return value


def reads(warehouse: api.Reader) -> int:
    return 0


class Unsolvable(Exception):
    """An exception whose arguments are not its message, so pickle cannot make it again from what it holds."""

    def __init__(self, price: float, settle: str) -> None:
        super().__init__(f"no yield gives {price} at {settle}")


class Unpriced(Exception):
    """An exception pickle makes again as it was."""


def refuse(price: float) -> int:
    raise Unsolvable(price, "2026-11-06")


def unpriced(price: float) -> int:
    raise Unpriced(f"no price near {price}")
'''


@pytest.fixture
def workloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    """The tests' compute module, `workloads.compute`, importable here and in the pool's processes."""
    package = tmp_path / "workloads"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "compute.py").write_text(textwrap.dedent(WORKLOADS), encoding="utf-8")
    monkeypatch.syspath_prepend(tmp_path)
    for name in ("workloads", "workloads.compute"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    yield importlib.import_module("workloads.compute")
    for name in ("workloads", "workloads.compute"):
        sys.modules.pop(name, None)


def _declared(workloads: ModuleType, name: str) -> api.ComputeFunction[..., object]:
    function: object = getattr(workloads, name)
    assert callable(function)
    return api.compute(function)


def _service(workloads: ModuleType, *, deadline_s: float = 5) -> tuple[api.Service, dict[str, api.ComputeFunction[..., object]]]:
    """A service that runs each workload by a route of its name, and answers `/now` without computing."""
    declared = {name: _declared(workloads, name) for name in ("spin", "tell", "die", "identity", "mark", "pid", "hold")}
    spin, tell, die, identity, mark, pid, hold = (declared[name] for name in ("spin", "tell", "die", "identity", "mark", "pid", "hold"))

    async def spun(seconds: float, run: Annotated[api.Compute[[float], int], api.given(spin)]) -> dict[str, int]:
        return {"turns": await run(seconds)}

    async def told(value: int, run: Annotated[api.Compute[[int], int], api.given(tell)]) -> dict[str, int]:
        return {"value": await run(value)}

    async def died(run: Annotated[api.Compute[[], int], api.given(die)]) -> dict[str, int]:
        return {"never": await run()}

    async def lock(run: Annotated[api.Compute[[object], object], api.given(identity)]) -> dict[str, str]:
        import threading  # noqa: PLC0415 -- a value pickle cannot carry

        await run(threading.Lock())
        return {"never": "returned"}

    async def marked(path: str, seconds: float, run: Annotated[api.Compute[[str, float], int], api.given(mark)]) -> dict[str, int]:
        return {"turns": await run(path, seconds)}

    async def which(run: Annotated[api.Compute[[], int], api.given(pid)]) -> dict[str, int]:
        return {"pid": await run()}

    async def held(started: str, release: str, run: Annotated[api.Compute[[str, str], int], api.given(hold)]) -> dict[str, int]:
        return {"turns": await run(started, release)}

    def now() -> dict[str, float]:
        return {"at": time.monotonic()}

    routes = [
        api.get("/spin", spun, deadline_s=deadline_s, response=dict[str, int], errors=[]),
        api.get("/tell", told, deadline_s=deadline_s, response=dict[str, int], errors=[]),
        api.get("/die", died, deadline_s=deadline_s, response=dict[str, int], errors=[]),
        api.get("/lock", lock, deadline_s=deadline_s, response=dict[str, str], errors=[]),
        api.get("/mark", marked, deadline_s=deadline_s, response=dict[str, int], errors=[]),
        api.get("/pid", which, deadline_s=deadline_s, response=dict[str, int], errors=[]),
        api.get("/hold", held, deadline_s=deadline_s, response=dict[str, int], errors=[]),
        api.get("/now", now, deadline_s=deadline_s, response=dict[str, float], errors=[]),
    ]
    return api.Service(name="workloads", settings=api.Settings, routes=routes), declared


def _pooled(workloads: ModuleType, *, workers: int = 1, deadline_s: float = 5) -> api.App:
    service, _ = _service(workloads, deadline_s=deadline_s)
    return api.build(service, settings=api.Settings.given(compute={"workers": workers}))


def _nested() -> Callable[[float], int]:
    def spin(seconds: float) -> int:
        return int(seconds)

    return spin


async def _async_solve(dirty: float) -> float:
    return dirty


def solve_here(dirty: float) -> float:
    return dirty


@pytest.mark.parametrize(
    ("declare", "message"),
    [
        (lambda: api.compute(lambda seconds: seconds), "a compute function is a named function"),
        (lambda: api.compute(_async_solve), "is not a plain def"),
        (lambda: api.compute(_nested()), "is not at the top level of"),
        (lambda: api.compute(solve_here), "move it to a module named compute"),
    ],
    ids=["lambda", "async", "nested", "not-in-a-compute-module"],
)
def test_a_compute_function_is_a_plain_def_in_a_compute_module(declare: Callable[[], object], message: str) -> None:
    with pytest.raises(api.DeclarationError, match=message):
        declare()


SOLVE = api.compute(example.yield_to_maturity)


async def _wrong_annotation(solve: Annotated[api.Compute[[float], float], api.given(SOLVE)]) -> dict[str, str]:
    del solve
    return {}


def _sync(solve: Annotated[api.Compute[[float, float, int, int, float], float], api.given(SOLVE)]) -> dict[str, str]:
    del solve
    return {}


async def _solves(solve: Annotated[api.Compute[[float, float, int, int, float], float], api.given(SOLVE)]) -> float:
    return await solve(100.0, 2.25, 2, 16, 1.0)


async def _in_a_dependency(solved: Annotated[float, Depends(_solves)]) -> dict[str, str]:
    del solved
    return {}


@pytest.mark.parametrize(
    ("handler", "message"),
    [
        (_wrong_annotation, r"annotate it Annotated\[api.Compute\[\[float, float, int, int, float\], float\], api.given\(...\)\]"),
        (_sync, "whose calls are awaited: declare it `async def _sync`"),
        (_in_a_dependency, "_in_a_dependency's dependency _solves -> solve asks for yield_to_maturity"),
    ],
    ids=["wrong-annotation", "sync-handler", "in-a-dependency"],
)
def test_a_handler_asks_for_a_compute_function_as_it_is(handler: Callable[..., object], message: str) -> None:
    with pytest.raises(api.DeclarationError, match=message):
        api.get("/y", handler, deadline_s=1, response=dict[str, str], errors=[])  # ty: ignore[invalid-argument-type] -- each a function


def test_a_compute_function_reaches_no_resource(workloads: ModuleType) -> None:
    reads = _declared(workloads, "reads")

    async def handler(run: Annotated[api.Compute[[api.Reader], int], api.given(reads)]) -> dict[str, int]:
        del run
        return {}

    with pytest.raises(api.DeclarationError, match="compute function reads takes Reader, and a compute function reaches no resource"):
        api.get("/r", handler, deadline_s=1, response=dict[str, int], errors=[])


def test_a_service_with_compute_needs_its_pool_unless_a_test_fakes_it(workloads: ModuleType) -> None:
    service, declared = _service(workloads)
    with pytest.raises(SettingsError, match="set BASIS_COMPUTE__WORKERS, the processes the compute functions spin, tell, die, identity, mark, pid, hold run in"):
        api.build(service, settings=api.Settings.given())
    api.build(service, settings=api.Settings.given(), fakes=dict.fromkeys(declared.values(), fakes.Compute()))


def test_a_fake_runs_the_function_here_and_still_pickles_what_crosses(workloads: ModuleType) -> None:
    service, declared = _service(workloads)
    with TestClient(api.build(service, settings=api.Settings.given(), fakes=dict.fromkeys(declared.values(), fakes.Compute())).asgi) as client:
        assert client.get("/spin", params={"seconds": "0"}).status_code == HTTPStatus.OK
        assert client.get("/lock").status_code == HTTPStatus.INTERNAL_SERVER_ERROR  # a lock cannot cross to a process


def test_cpu_bound_compute_leaves_the_event_loop_free(workloads: ModuleType, tmp_path: Path) -> None:
    """While one request spins in the pool until the test releases it, others are answered: were the event loop held,
    they would wait for the spin, which never ends before they do, and the guard of 10s on each would fail the test."""
    started, release = tmp_path / "started", tmp_path / "release"
    with TestClient(_pooled(workloads).asgi) as client, ThreadPoolExecutor(2) as threads:
        computing = threads.submit(client.get, "/hold", params={"started": str(started), "release": str(release)})
        _until(started.exists)
        answered = [threads.submit(client.get, "/now").result(timeout=10).status_code for _ in range(5)]
        spinning = not computing.done()
        release.write_text("go", encoding="utf-8")
        turns = computing.result(timeout=10).json()["turns"]
    assert (answered, spinning, turns > 0) == ([HTTPStatus.OK] * 5, True, True)


def _until(done: Callable[[], bool]) -> None:
    """Wait until `done`, for 10s at the most, which guards against a hang and never decides an outcome."""
    for _ in range(1000):
        if done():
            return
        time.sleep(0.01)
    pytest.fail("what the test waited for never happened")


def test_a_call_past_its_routes_deadline_is_a_504_and_shutdown_kills_it(workloads: ModuleType, tmp_path: Path, logged: Callable[[], list[dict[str, object]]]) -> None:
    """The call would write its file after 2s of CPU; the app stops long before, and the file never appears. Two
    processes, so the overdue call holds one and the pool does not start new ones at its deadline: shutdown alone
    stops it."""
    marker = tmp_path / "finished"
    with TestClient(_pooled(workloads, workers=2, deadline_s=0.3).asgi) as client:
        response = client.get("/mark", params={"path": str(marker), "seconds": "2"})
    assert (response.status_code, response.json()["detail"]) == (HTTPStatus.GATEWAY_TIMEOUT, "compute mark did not answer within what was left of route marked's deadline of 0.3s")
    time.sleep(3)
    assert not marker.exists()
    assert "stopping the compute pool's processes, which still run 1 calls past their deadlines" in [record["message"] for record in logged()]


def test_a_request_id_reaches_what_its_compute_function_logs(workloads: ModuleType, capfd: pytest.CaptureFixture[str]) -> None:
    with TestClient(_pooled(workloads).asgi) as client:
        response = client.get("/tell", params={"value": 7})
    assert response.json() == {"value": 7}
    told = [record for line in capfd.readouterr().err.splitlines() if line.startswith("{") and (record := json.loads(line)).get("message") == "computing 7"]
    assert [record["request_id"] for record in told] == [response.headers["x-request-id"]]


def test_a_pool_whose_idle_process_died_starts_again_for_the_next_call(workloads: ModuleType, logged: Callable[[], list[dict[str, object]]]) -> None:
    """A process killed between calls breaks the pool before the next is made, which `submit` refuses at once."""
    with TestClient(_pooled(workloads).asgi) as client:
        first = client.get("/pid").json()["pid"]
        os.kill(first, getattr(signal, "SIGKILL", signal.SIGTERM))  # Windows has no SIGKILL; its SIGTERM ends the process at once
        time.sleep(1)  # for the pool to see it
        again = client.get("/pid")
    assert again.status_code == HTTPStatus.OK
    assert again.json()["pid"] != first
    assert "a process of the compute pool stopped, so the pool starts again" in [record["message"] for record in logged()]


def test_a_compute_function_is_asked_for_by_the_types_its_aliases_name(workloads: ModuleType) -> None:
    scale = _declared(workloads, "scale")

    async def doubled(run: Annotated[api.Compute[[float], float], api.given(scale)]) -> dict[str, str]:
        return {"amount": str(await run(1.0))}

    assert api.get("/double", doubled, deadline_s=1, response=dict[str, str], errors=[]).asks[0].given == scale


def _failing(workloads: ModuleType) -> tuple[api.Service, list[api.ComputeFunction[..., object]]]:
    """A service whose `/refuse` raises an exception that does not cross processes, and `/unpriced` one that does, each
    declared a 422; and `/hold`, which spins until the test releases it."""
    refuse, unpriced, hold = (_declared(workloads, name) for name in ("refuse", "unpriced", "hold"))
    unsolvable = api.error(workloads.Unsolvable, status=HTTPStatus.UNPROCESSABLE_CONTENT, title="No yield gives the price")
    priceless = api.error(workloads.Unpriced, status=HTTPStatus.UNPROCESSABLE_CONTENT, title="No price")

    async def refused(price: float, run: Annotated[api.Compute[[float], int], api.given(refuse)]) -> dict[str, int]:
        return {"never": await run(price)}

    async def missing(price: float, run: Annotated[api.Compute[[float], int], api.given(unpriced)]) -> dict[str, int]:
        return {"never": await run(price)}

    async def held(started: str, release: str, run: Annotated[api.Compute[[str, str], int], api.given(hold)]) -> dict[str, int]:
        return {"turns": await run(started, release)}

    routes = [
        api.get("/refuse", refused, deadline_s=10, response=dict[str, int], errors=[unsolvable]),
        api.get("/unpriced", missing, deadline_s=10, response=dict[str, int], errors=[priceless]),
        api.get("/hold", held, deadline_s=10, response=dict[str, int], errors=[]),
    ]
    return api.Service(name="failing", settings=api.Settings, routes=routes), [refuse, unpriced, hold]


def test_an_exception_that_does_not_cross_processes_fails_its_call_alone(workloads: ModuleType, tmp_path: Path, logged: Callable[[], list[dict[str, object]]]) -> None:
    """It is a defect, a 500 whose log says why, and the call the other process runs meanwhile still answers."""
    started, release = tmp_path / "started", tmp_path / "release"
    service, _ = _failing(workloads)
    with TestClient(api.build(service, settings=api.Settings.given(compute={"workers": 2})).asgi) as client, ThreadPoolExecutor(1) as threads:
        computing = threads.submit(client.get, "/hold", params={"started": str(started), "release": str(release)})
        _until(started.exists)
        refused = client.get("/refuse", params={"price": 101.5})
        crossed = client.get("/unpriced", params={"price": 101.5})
        release.write_text("go", encoding="utf-8")
        held = computing.result(timeout=10)
    assert (refused.status_code, crossed.status_code, crossed.json()["detail"], held.status_code) == (
        HTTPStatus.INTERNAL_SERVER_ERROR, HTTPStatus.UNPROCESSABLE_CONTENT, "no price near 101.5", HTTPStatus.OK,
    )  # fmt: skip
    [failure] = [record for record in logged() if "traceback" in record]
    assert "compute refuse raised Unsolvable, which pickle cannot make again in the app's process" in str(failure["error"])
    assert "a process of the compute pool stopped, so the pool starts again" not in [record["message"] for record in logged()]


def test_a_fake_answers_an_exception_as_the_pool_does(workloads: ModuleType) -> None:
    service, declared = _failing(workloads)
    with TestClient(api.build(service, settings=api.Settings.given(), fakes=dict.fromkeys(declared, fakes.Compute())).asgi) as client:
        refused = client.get("/refuse", params={"price": 101.5})
        crossed = client.get("/unpriced", params={"price": 101.5})
    assert (refused.status_code, crossed.status_code) == (HTTPStatus.INTERNAL_SERVER_ERROR, HTTPStatus.UNPROCESSABLE_CONTENT)


def test_calls_past_their_deadlines_never_starve_the_pool(workloads: ModuleType, tmp_path: Path, logged: Callable[[], list[dict[str, object]]]) -> None:
    """A call that never ends holds the pool's one process past its deadline: the pool starts a new one for the next."""
    started, release = tmp_path / "started", tmp_path / "release"
    with TestClient(_pooled(workloads, deadline_s=1).asgi) as client:
        stuck = client.get("/hold", params={"started": str(started), "release": str(release)})
        then = [client.get("/pid").status_code for _ in range(3)]
        ready = client.get("/health/ready").status_code
        release.write_text("go", encoding="utf-8")
    assert (stuck.status_code, then, ready) == (HTTPStatus.GATEWAY_TIMEOUT, [HTTPStatus.OK] * 3, HTTPStatus.OK)
    assert "every process of the compute pool runs a call past its deadline, so the pool starts new ones and stops those" in [record["message"] for record in logged()]


def test_a_process_that_dies_fails_its_call_and_the_next_call_starts_new_ones(workloads: ModuleType) -> None:
    with TestClient(_pooled(workloads).asgi) as client:
        assert client.get("/die").status_code == HTTPStatus.INTERNAL_SERVER_ERROR
        assert client.get("/spin", params={"seconds": "0"}).status_code == HTTPStatus.OK


def test_the_examples_solver_finds_the_coupon_rate_at_par() -> None:
    """Sixteen semiannual coupons of 2.25 per 100, the next a whole period away, priced at 100: the yield is 4.5%."""
    assert example.yield_to_maturity(100.0, 2.25, 2, 16, 1.0) == pytest.approx(0.045, abs=1e-9)


@pytest.mark.parametrize(("annual", "first"), [(0.04, 1.0), (0.07, 0.25)], ids=["at-a-premium", "at-a-discount-mid-period"])
def test_the_examples_solver_inverts_its_price(annual: float, first: float) -> None:
    dirty = example.price(annual, 2.25, 2, 16, first)
    assert example.yield_to_maturity(dirty, 2.25, 2, 16, first) == pytest.approx(annual, abs=1e-9)


@pytest.mark.parametrize(("dirty", "periods", "message"), [(0.0, 16, "no yield from"), (100.0, 0, "has no yield")], ids=["no-yield-prices-it", "no-coupons-left"])
def test_the_examples_solver_refuses_what_no_yield_solves(dirty: float, periods: int, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        example.yield_to_maturity(dirty, 2.25, 2, periods, 1.0)
