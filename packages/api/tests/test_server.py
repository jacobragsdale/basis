"""`api.serve` as a process: it serves, and on SIGTERM drains, finishes the request in flight, and stops; a second
signal stops it at once. The only tests that open a socket, on the loopback interface, to a child process."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus

import httpx2
import pytest

from basis import api

SERVICE = """
import sys
import time
from basis import api

def slow() -> dict[str, str]:
    time.sleep(float(sys.argv[1]))
    return {"slept": sys.argv[1]}

api.serve(api.Service(name="slow", settings=api.Settings, routes=[api.get("/slow", slow)]))
"""


@dataclass(frozen=True)
class Server:
    process: subprocess.Popen[str]
    base: str
    port: int


def test_sigterm_drains_then_lets_the_request_in_flight_finish() -> None:
    with _serving(slow_s=2, drain_s=1) as server, ThreadPoolExecutor(1) as pool:
        in_flight = pool.submit(httpx2.get, f"{server.base}/slow", timeout=15)
        time.sleep(0.3)  # in its handler, which sleeps past the drain
        server.process.send_signal(signal.SIGTERM)
        time.sleep(0.1)
        draining = httpx2.get(f"{server.base}/health/ready", timeout=5)
        assert (draining.status_code, draining.json()["detail"]) == (HTTPStatus.SERVICE_UNAVAILABLE, "the service is draining")
        assert in_flight.result().json() == {"slept": "2"}
        records = _records(server.process)
    assert server.process.returncode == -signal.SIGTERM  # uvicorn raises the signal again once it has shut down, as it was asked to
    assert _messages(records) == [
        f"serving slow on 127.0.0.1:{server.port}",
        "slow is ready",
        "draining for 1.0s: readiness reports 503 while requests are still served",
        "GET /slow: 200",
        "slow stopped",
    ]
    assert all(record["service"] == "slow" for record in records)
    assert not any("traceback" in record for record in records)


def test_a_second_signal_stops_at_once() -> None:
    with _serving(slow_s=8, drain_s=10) as server, ThreadPoolExecutor(1) as pool:
        in_flight = pool.submit(httpx2.get, f"{server.base}/slow", timeout=15)
        time.sleep(0.3)
        server.process.send_signal(signal.SIGTERM)
        time.sleep(0.3)
        second = time.monotonic()
        server.process.send_signal(signal.SIGTERM)
        records = _records(server.process)
        stopped_after = time.monotonic() - second
        with pytest.raises(httpx2.TransportError):
            in_flight.result()
    assert stopped_after < 3  # not the 10 seconds of drain, nor the 8 the request in flight still needed
    assert server.process.returncode == -signal.SIGTERM
    assert "slow stopped" not in _messages(records)  # a forced stop skips the lifespan's shutdown


@contextmanager
def _serving(*, slow_s: float, drain_s: float) -> Generator[Server]:
    prefix = str(api.Settings.model_config.get("env_prefix") or "")  # whatever basis-core's prefix is
    port = _free_port()
    settings = {"SERVE__HOST": "127.0.0.1", "SERVE__PORT": str(port), "SERVE__DRAIN_S": str(drain_s), "SERVE__SHUTDOWN_S": "10"}
    environment = {key: value for key, value in os.environ.items() if not key.startswith(prefix)} | {f"{prefix}{key}": value for key, value in settings.items()}
    process = subprocess.Popen([sys.executable, "-c", SERVICE, str(slow_s)], env=environment, stderr=subprocess.PIPE, text=True)
    server = Server(process, f"http://127.0.0.1:{port}", port)
    try:
        _wait_until_ready(server)
        yield server
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def _records(process: subprocess.Popen[str]) -> list[dict[str, object]]:
    """The server's log, once it has exited: one JSON object per line on stderr, and nothing else."""
    _, errors = process.communicate(timeout=20)
    records: list[dict[str, object]] = []
    for line in errors.splitlines():
        record: object = json.loads(line)
        assert isinstance(record, dict)
        records.append({str(key): value for key, value in record.items()})
    return records


def _messages(records: list[dict[str, object]]) -> list[str]:
    return [message for record in records if not (message := str(record["message"])).startswith("GET /health/ready")]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_until_ready(server: Server) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if server.process.poll() is not None:
            _, errors = server.process.communicate()
            msg = f"the server exited with {server.process.returncode}: {errors}"
            raise AssertionError(msg)
        try:
            if httpx2.get(f"{server.base}/health/ready", timeout=1).status_code == HTTPStatus.OK:
                return
        except httpx2.TransportError:
            pass
        time.sleep(0.1)
    msg = "the server was not ready within 30 seconds"
    raise AssertionError(msg)
