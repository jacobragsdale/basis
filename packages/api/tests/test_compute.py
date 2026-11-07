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
from decimal import Decimal
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
from decimal import Decimal
from pathlib import Path

from basis import api

type Amount = Decimal


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


def scale(amount: Amount) -> Amount:
    return amount * 2


def identity(value: object) -> object:
    return value


def reads(warehouse: api.Reader) -> int:
    return 0
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
    declared = {name: _declared(workloads, name) for name in ("spin", "tell", "die", "identity", "mark", "pid")}
    spin, tell, die, identity, mark, pid = (declared[name] for name in ("spin", "tell", "die", "identity", "mark", "pid"))

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

    def now() -> dict[str, float]:
        return {"at": time.monotonic()}

    routes = [
        api.get("/spin", spun, deadline_s=deadline_s, response=dict[str, int], errors=[]),
        api.get("/tell", told, deadline_s=deadline_s, response=dict[str, int], errors=[]),
        api.get("/die", died, deadline_s=deadline_s, response=dict[str, int], errors=[]),
        api.get("/lock", lock, deadline_s=deadline_s, response=dict[str, str], errors=[]),
        api.get("/mark", marked, deadline_s=deadline_s, response=dict[str, int], errors=[]),
        api.get("/pid", which, deadline_s=deadline_s, response=dict[str, int], errors=[]),
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


async def _async_solve(dirty: Decimal) -> Decimal:
    return dirty


def solve_here(dirty: Decimal) -> Decimal:
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
    with pytest.raises(api.ServiceError, match=message):
        declare()


SOLVE = api.compute(example.yield_to_maturity)


async def _wrong_annotation(solve: Annotated[api.Compute[[Decimal], Decimal], api.given(SOLVE)]) -> dict[str, str]:
    del solve
    return {}


def _sync(solve: Annotated[api.Compute[[Decimal, Decimal, int, int, Decimal], Decimal], api.given(SOLVE)]) -> dict[str, str]:
    del solve
    return {}


async def _solves(solve: Annotated[api.Compute[[Decimal, Decimal, int, int, Decimal], Decimal], api.given(SOLVE)]) -> Decimal:
    return await solve(Decimal(100), Decimal("2.25"), 2, 16, Decimal(1))


async def _in_a_dependency(solved: Annotated[Decimal, Depends(_solves)]) -> dict[str, str]:
    del solved
    return {}


@pytest.mark.parametrize(
    ("handler", "message"),
    [
        (_wrong_annotation, r"annotate it Annotated\[api.Compute\[\[Decimal, Decimal, int, int, Decimal\], Decimal\], api.given\(...\)\]"),
        (_sync, "whose calls are awaited: declare it `async def _sync`"),
        (_in_a_dependency, "_in_a_dependency's dependency _solves -> solve asks for yield_to_maturity"),
    ],
    ids=["wrong-annotation", "sync-handler", "in-a-dependency"],
)
def test_a_handler_asks_for_a_compute_function_as_it_is(handler: Callable[..., object], message: str) -> None:
    with pytest.raises(api.ServiceError, match=message):
        api.get("/y", handler, deadline_s=1, response=dict[str, str], errors=[])  # ty: ignore[invalid-argument-type] -- each a function


def test_a_compute_function_reaches_no_resource(workloads: ModuleType) -> None:
    reads = _declared(workloads, "reads")

    async def handler(run: Annotated[api.Compute[[api.Reader], int], api.given(reads)]) -> dict[str, int]:
        del run
        return {}

    with pytest.raises(api.ServiceError, match="compute function reads takes Reader, and a compute function reaches no resource"):
        api.get("/r", handler, deadline_s=1, response=dict[str, int], errors=[])


def test_a_service_with_compute_needs_its_pool_unless_a_test_fakes_it(workloads: ModuleType) -> None:
    service, declared = _service(workloads)
    with pytest.raises(SettingsError, match="set BASIS_COMPUTE__WORKERS, the processes the compute functions spin, tell, die, identity, mark, pid run in"):
        api.build(service, settings=api.Settings.given())
    api.build(service, settings=api.Settings.given(), fakes=dict.fromkeys(declared.values(), fakes.Compute()))


def test_a_fake_runs_the_function_here_and_still_pickles_what_crosses(workloads: ModuleType) -> None:
    service, declared = _service(workloads)
    with TestClient(api.build(service, settings=api.Settings.given(), fakes=dict.fromkeys(declared.values(), fakes.Compute())).asgi) as client:
        assert client.get("/spin", params={"seconds": "0"}).status_code == HTTPStatus.OK
        assert client.get("/lock").status_code == HTTPStatus.INTERNAL_SERVER_ERROR  # a lock cannot cross to a process


def test_cpu_bound_compute_leaves_the_event_loop_free(workloads: ModuleType) -> None:
    """While one request spends 1.5s of CPU in the pool, others are answered at once."""
    with TestClient(_pooled(workloads).asgi) as client, ThreadPoolExecutor(1) as thread:
        computing = thread.submit(client.get, "/spin", params={"seconds": "1.5"})
        time.sleep(0.3)
        answered = []
        for _ in range(5):
            started = time.monotonic()
            client.get("/now").raise_for_status()
            answered.append(time.monotonic() - started)
        assert not computing.done()
        assert computing.result().json()["turns"] > 0
    assert max(answered) < 0.5


def test_a_call_past_its_routes_deadline_is_a_504_and_shutdown_kills_it(workloads: ModuleType, tmp_path: Path, logged: Callable[[], list[dict[str, object]]]) -> None:
    """The call would write its file after 2s of CPU; the app stops long before, and the file never appears."""
    marker = tmp_path / "finished"
    with TestClient(_pooled(workloads, deadline_s=0.3).asgi) as client:
        response = client.get("/mark", params={"path": str(marker), "seconds": "2"})
    assert (response.status_code, response.json()["detail"]) == (HTTPStatus.GATEWAY_TIMEOUT, "compute mark did not answer within what was left of route marked's deadline of 0.3s")
    time.sleep(3)
    assert not marker.exists()
    assert "stopping the compute pool's processes, which still run 1 calls past their deadlines" in [record["message"] for record in logged()]


def test_a_request_id_reaches_what_its_compute_function_logs(workloads: ModuleType, capfd: pytest.CaptureFixture[str]) -> None:
    with TestClient(_pooled(workloads).asgi) as client:
        response = client.get("/tell", params={"value": "7"})
    assert response.json() == {"value": 7}
    told = [record for line in capfd.readouterr().err.splitlines() if line.startswith("{") and (record := json.loads(line)).get("message") == "computing 7"]
    assert [record["request_id"] for record in told] == [response.headers["x-request-id"]]


def test_a_pool_whose_idle_process_died_starts_again_for_the_next_call(workloads: ModuleType, logged: Callable[[], list[dict[str, object]]]) -> None:
    """A process killed between calls breaks the pool before the next is made, which `submit` refuses at once."""
    with TestClient(_pooled(workloads).asgi) as client:
        first = client.get("/pid").json()["pid"]
        os.kill(first, signal.SIGKILL)
        time.sleep(1)  # for the pool to see it
        again = client.get("/pid")
    assert again.status_code == HTTPStatus.OK
    assert again.json()["pid"] != first
    assert "a process of the compute pool stopped, so the pool starts again" in [record["message"] for record in logged()]


def test_a_compute_function_is_asked_for_by_the_types_its_aliases_name(workloads: ModuleType) -> None:
    scale = _declared(workloads, "scale")

    async def doubled(run: Annotated[api.Compute[[Decimal], Decimal], api.given(scale)]) -> dict[str, str]:
        return {"amount": str(await run(Decimal(1)))}

    assert api.get("/double", doubled, deadline_s=1, response=dict[str, str], errors=[]).asks[0].given == scale


def test_a_process_that_dies_fails_its_call_and_the_next_call_starts_new_ones(workloads: ModuleType) -> None:
    with TestClient(_pooled(workloads).asgi) as client:
        assert client.get("/die").status_code == HTTPStatus.INTERNAL_SERVER_ERROR
        assert client.get("/spin", params={"seconds": "0"}).status_code == HTTPStatus.OK


def test_the_examples_solver_finds_the_coupon_rate_at_par() -> None:
    """Sixteen semiannual coupons of 2.25 per 100, the next a whole period away, priced at 100: the yield is 4.5%."""
    assert example.yield_to_maturity(Decimal(100), Decimal("2.25"), 2, 16, Decimal(1)) == Decimal("0.0450000000")


@pytest.mark.parametrize(("annual", "first"), [(Decimal("0.04"), Decimal(1)), (Decimal("0.07"), Decimal("0.25"))], ids=["at-a-premium", "at-a-discount-mid-period"])
def test_the_examples_solver_inverts_its_price(annual: Decimal, first: Decimal) -> None:
    dirty = example.price(annual, Decimal("2.25"), 2, 16, first)
    assert example.yield_to_maturity(dirty, Decimal("2.25"), 2, 16, first) == annual.quantize(example.PLACES)


@pytest.mark.parametrize(("dirty", "periods", "message"), [(Decimal(0), 16, "no yield from"), (Decimal(100), 0, "has no yield")], ids=["no-yield-prices-it", "no-coupons-left"])
def test_the_examples_solver_refuses_what_no_yield_solves(dirty: Decimal, periods: int, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        example.yield_to_maturity(dirty, Decimal("2.25"), 2, periods, Decimal(1))
