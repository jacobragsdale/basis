"""`api.serve` as a process: it serves, and on SIGTERM drains, finishes the request in flight, and stops. The one test
that opens a socket, on the loopback interface, to a child process."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus

import httpx2

from basis import api

SERVICE = """
import time
from basis import api

def slow() -> dict[str, str]:
    time.sleep(2)
    return {"slept": "2s"}

api.serve(api.Service(name="slow", settings=api.Settings, routes=[api.get("/slow", slow)]))
"""


def test_sigterm_drains_then_lets_the_request_in_flight_finish() -> None:
    prefix = str(api.Settings.model_config.get("env_prefix") or "")  # whatever basis-core's prefix is
    port = _free_port()
    settings = {"SERVE__HOST": "127.0.0.1", "SERVE__PORT": str(port), "SERVE__DRAIN_S": "1", "SERVE__SHUTDOWN_S": "10"}
    environment = {key: value for key, value in os.environ.items() if not key.startswith(prefix)} | {f"{prefix}{key}": value for key, value in settings.items()}
    base = f"http://127.0.0.1:{port}"
    server = subprocess.Popen([sys.executable, "-c", SERVICE], env=environment, stderr=subprocess.PIPE, text=True)
    try:
        _wait_until_ready(base, server)
        with ThreadPoolExecutor(1) as pool:
            in_flight = pool.submit(httpx2.get, f"{base}/slow", timeout=15)
            time.sleep(0.3)  # in its handler, which sleeps past the drain
            server.send_signal(signal.SIGTERM)
            time.sleep(0.1)
            draining = httpx2.get(f"{base}/health/ready", timeout=5)
            assert (draining.status_code, draining.json()["detail"]) == (HTTPStatus.SERVICE_UNAVAILABLE, "the service is draining")
            assert in_flight.result().json() == {"slept": "2s"}
        _, errors = server.communicate(timeout=20)
    finally:
        if server.poll() is None:
            server.kill()
            server.communicate()
    assert server.returncode == -signal.SIGTERM  # uvicorn raises the signal again once it has shut down, as it was asked to
    records = [json.loads(line) for line in errors.splitlines()]
    messages = [record["message"] for record in records if not str(record["message"]).startswith("GET /health/ready")]
    assert messages == [f"serving slow on 127.0.0.1:{port}", "slow is ready", "draining for 1.0s: readiness reports 503 while requests are still served", "GET /slow: 200", "slow stopped"]
    assert all(record["service"] == "slow" for record in records)
    assert not any("traceback" in record for record in records)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_until_ready(base: str, server: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if server.poll() is not None:
            _, errors = server.communicate()
            msg = f"the server exited with {server.returncode}: {errors}"
            raise AssertionError(msg)
        try:
            if httpx2.get(f"{base}/health/ready", timeout=1).status_code == HTTPStatus.OK:
                return
        except httpx2.TransportError:
            pass
        time.sleep(0.1)
    msg = "the server was not ready within 30 seconds"
    raise AssertionError(msg)
