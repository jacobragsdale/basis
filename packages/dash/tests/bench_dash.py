"""Step 17.6's measurements, made again with one command on the development host, through `heavy`:

    heavy -- uv run python packages/dash/tests/bench_dash.py OUT_FOLDER

It prints JSON, and writes its scratch fixtures under `OUT_FOLDER`:

- **grids**: a page whose one callback loads the example's positions view and answers a `bd.grid`'s rows and totals,
  for 1,000 to 100,000 holdings sampled from the example's: the callback's milliseconds read from the database and
  answered from the cache, how long it held the event loop, its answer's bytes of JSON and gzip's at level 1, and the
  cache's bytes and the process's resident memory after it;
- **sessions**: the same app served by uvicorn on a socket, fifty sessions each asking again and again for an answer
  from the cache, while one other session's query takes two seconds: what the fifty waited, what the slow one did,
  and what `/health/live` did; for a small answer, a line of text, for a grid of 1,000 rows by `bd.rows`, whose JSON
  the event loop writes, and for the same grid's rows and totals written once per value (`bd.load_rows`,
  `GRID.load_totals`).

The numbers go in `docs/design.md`; nothing here is a test."""

import asyncio
import gzip
import json
import socket
import statistics
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import override

import httpx2
import pandas as pd
import pyarrow as pa
import uvicorn
from dash import Input, Output, callback, html

import basis.dash as bd
from basis.core.databases import Bind
from basis.dash import fixtures, internals
from basis.dash.building import build
from basis.dash.driving import driving
from basis.dash.example.data import POSITIONS, WAREHOUSE, Holding, Position
from basis.dash.fixtures import FixtureDatabase
from basis.dash.grids import Total

SIZES = (1_000, 10_000, 50_000, 100_000)
SESSIONS = 50
ASKS = 20
"""How many times each of the fifty sessions asks while the slow query runs."""
SLOW_S = 2.0
EXAMPLE = Path(fixtures.__file__).parent / "example" / "fixtures"
AS_OF = date(2026, 11, 12)

TOTALS: dict[str, Total] = {"par": "sum", "market_value": "sum", "pnl": "sum", "ytw": bd.weighted("market_value")}
GRID = bd.grid("bench-grid", Position, key="cusip", totals=TOTALS)
WRITTEN = bd.grid("bench-written", Position, key="cusip", totals=TOTALS)
"""The same grid, its rows and totals written once per value (`bd.load_rows`, `GRID.load_totals`)."""


def _slow_sql(folder: Path) -> str:
    sql = folder / "slow.sql"
    sql.parent.mkdir(parents=True, exist_ok=True)
    sql.write_text("select * from slow where as_of = %(as_of)s\n", encoding="utf-8")
    return str(sql)


OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd() / "bench"
SLOW = bd.query("slow", WAREHOUSE, _slow_sql(OUT), Holding, binds={"as_of": date}, ttl_s=1, stale_s=1)

page = bd.page("/", "Bench", layout=html.Div([html.Div(id="bench-day", children=AS_OF.isoformat()), GRID, WRITTEN, html.Div(id="bench-slow"), html.Div(id="bench-count")]))


@callback(Output("bench-grid", "rowData"), Output("bench-grid", "dashGridOptions"), Input("bench-day", "children"))
async def grid(day: str) -> tuple[object, object]:
    positions = await bd.load(POSITIONS, as_of=date.fromisoformat(day))
    return bd.rows(positions), GRID.totals(positions)


@callback(Output("bench-written", "rowData"), Output("bench-written", "dashGridOptions"), Input("bench-day", "children"))
async def written(day: str) -> tuple[object, object]:
    as_of = date.fromisoformat(day)
    return await bd.load_rows(POSITIONS, as_of=as_of), await WRITTEN.load_totals(POSITIONS, as_of=as_of)


@callback(Output("bench-count", "children"), Input("bench-count", "title"), prevent_initial_call=True)
async def count(_: str) -> str:
    return f"{len(await bd.load(POSITIONS, as_of=AS_OF))} positions"


@callback(Output("bench-slow", "children"), Input("bench-slow", "title"), prevent_initial_call=True)
async def slow(_: str) -> str:
    return f"{len(await bd.load(SLOW, as_of=AS_OF))} rows, slowly"


app = bd.App(name="bench", title="Bench", settings=bd.Settings, resources=[WAREHOUSE], pages=[page])


class Slow(FixtureDatabase):
    """The example's fixtures, the `slow` query's answer after `SLOW_S` seconds, as a database under load gives it."""

    @override
    def answer(self, sql: str, binds: Mapping[str, Bind]) -> pa.Table:
        found = super().answer(sql, binds)
        if self.queries[-1][0] == "slow":
            time.sleep(SLOW_S)
        return found


def _fixtures(size: int) -> Path:
    """A folder of the example's fixtures with `size` holdings, sampled from its 320, each CUSIP its own."""
    folder = OUT / f"holdings-{size}"
    folder.mkdir(parents=True, exist_ok=True)
    holdings = pd.read_csv(EXAMPLE / "holdings.csv", dtype=str)
    made = holdings.sample(n=size, replace=True, random_state=size).reset_index(drop=True)
    made["cusip"] = [f"{index:09d}" for index in range(size)]
    made.to_csv(folder / "holdings.csv", index=False)
    (folder / "curve.csv").write_bytes((EXAMPLE / "curve.csv").read_bytes())
    made.head(100).to_csv(folder / "slow.csv", index=False)
    return folder


def _rss_mb() -> float:
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return float("nan")


def grids() -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for size in SIZES:
        database = fixtures.database(WAREHOUSE, _fixtures(size))
        with driving(app, fakes={WAREHOUSE: database}) as drive:
            components = drive.page("/")
            values = drive.first("/", components)
            [entry] = [entry for entry in drive.calls("/", components) if "bench-grid" in str(entry["output"])]
            [written] = [entry for entry in drive.calls("/", components) if "bench-written" in str(entry["output"])]
            timings = []
            for _ in range(3):  # read once from the database, then twice from the cache
                started = time.perf_counter()
                called = drive.call(entry, values)
                timings.append(((time.perf_counter() - started) * 1000, called.held_ms, len(called.content)))
            assert not called.failed, called.failures
            drive.call(written, values)  # its rows and totals written, once
            again = [drive.call(written, values) for _ in range(2)]
            found.append(
                {
                    "rows": size,
                    "miss_ms": round(timings[0][0], 1),
                    "hit_ms": round(min(timings[1][0], timings[2][0]), 1),
                    "held_ms": round(max(timings[1][1], timings[2][1]), 1),
                    "written_held_ms": round(max(found.held_ms for found in again), 1),
                    "json_mb": round(timings[0][2] / 1_048_576, 2),
                    "gzip1_mb": round(len(gzip.compress(called.content, compresslevel=1)) / 1_048_576, 2),
                    "cache_mb": round(drive.client.built.runtime.cache.bytes / 1_048_576, 1),
                    "rss_mb": round(_rss_mb()),
                }
            )
    return found


@dataclass
class _Served:
    server: uvicorn.Server
    thread: threading.Thread
    base: str


def _serve(database: FixtureDatabase) -> _Served:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    built = build(app, settings=app.settings.given(serve={"port": port}), fakes={WAREHOUSE: database})
    server = uvicorn.Server(uvicorn.Config(built.asgi, host="127.0.0.1", port=port, log_config=None, access_log=False, lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    return _Served(server, thread, f"http://127.0.0.1:{port}")


def _body(output: str, inputs: list[dict[str, object]]) -> dict[str, object]:
    return internals.request(output, inputs, changed=[])


ANSWERS = {
    "a line of text": ("bench-count.children", "bench-count", "title", "go"),
    "1,000 rows by bd.rows": ("..bench-grid.rowData...bench-grid.dashGridOptions..", "bench-day", "children", AS_OF.isoformat()),
    "1,000 rows written once": ("..bench-written.rowData...bench-written.dashGridOptions..", "bench-day", "children", AS_OF.isoformat()),
}


async def _sessions(base: str, *, answer: str) -> dict[str, object]:
    output, component, prop, value = ANSWERS[answer]
    fast = _body(output, [{"id": component, "property": prop, "value": value}])
    slow = _body("bench-slow.children", [{"id": "bench-slow", "property": "title", "value": "go"}])
    update = f"{base}/{internals.UPDATE}"
    async with httpx2.AsyncClient(timeout=30, limits=httpx2.Limits(max_connections=SESSIONS + 5)) as client:
        assert (await client.post(update, json=fast)).status_code == 200  # the cache holds the page now
        waited: list[float] = []
        live: list[float] = []

        async def session() -> None:
            for _ in range(ASKS):
                started = time.perf_counter()
                answered = await client.post(update, json=fast)
                waited.append((time.perf_counter() - started) * 1000)
                assert answered.status_code == 200

        async def probing(done: asyncio.Event) -> None:
            while not done.is_set():
                started = time.perf_counter()
                await client.get(f"{base}/health/live")
                live.append((time.perf_counter() - started) * 1000)
                await asyncio.sleep(0.01)

        async def slowly() -> tuple[int, float]:
            asked = time.perf_counter()
            answered = await client.post(update, json=slow)
            return answered.status_code, (time.perf_counter() - asked) * 1000

        done = asyncio.Event()
        started = time.perf_counter()
        slow_task = asyncio.create_task(slowly())
        probe = asyncio.create_task(probing(done))
        await asyncio.sleep(0.05)  # the slow query has begun
        await asyncio.gather(*(session() for _ in range(SESSIONS)))
        fifty_s = time.perf_counter() - started
        slow_status, slow_ms = await slow_task
        done.set()
        await probe
    ordered = sorted(waited)
    return {
        "answer": answer,
        "sessions": SESSIONS,
        "requests": len(waited),
        "fifty_done_s": round(fifty_s, 2),
        "slow_ms": round(slow_ms),
        "slow_status": slow_status,
        "waited_median_ms": round(statistics.median(ordered), 1),
        "waited_p95_ms": round(ordered[int(len(ordered) * 0.95)], 1),
        "waited_max_ms": round(ordered[-1], 1),
        "live_median_ms": round(statistics.median(live), 1),
        "live_max_ms": round(max(live), 1),
        "live_probes": len(live),
    }


def sessions(answer: str) -> dict[str, object]:
    made = fixtures.database(WAREHOUSE, _fixtures(1_000))
    served = _serve(Slow(declared=made.declared, folder=made.folder, fixtures=made.fixtures))
    try:
        return asyncio.run(_sessions(served.base, answer=answer))
    finally:
        served.server.should_exit = True
        served.thread.join(timeout=10)


if __name__ == "__main__":
    sys.stdout.write(json.dumps({"grids": grids(), "sessions": [sessions(answer) for answer in ANSWERS]}, indent=2) + "\n")
