"""basis-core's serving, on a server that is not an API's: a bare Starlette app with a lifespan, as a Dash app's
server is, uses each piece as it stands. The health routes answer under its path prefix, readiness follows its
lifecycle, the request boundary logs one record per request with its id, and `run` serves it with uvicorn and drains
on SIGTERM. basis-api's own tests hold the same pieces inside an API."""

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from http import HTTPStatus
from uuid import UUID

import httpx2
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from basis.core import serving
from basis.core.serving import Lifecycle, Serve, Stage
from basis.core.serving.requests import REQUEST_ID, Requests, health, is_health

PREFIX = "/apps/pos"


def _app(lifecycle: Lifecycle, *, prefix: str = PREFIX) -> Starlette:
    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncGenerator[None]:
        lifecycle.advance(Stage.READY)
        yield
        lifecycle.advance(Stage.STOPPED)

    async def page(request: Request) -> Response:
        return PlainTextResponse(str(request.scope[REQUEST_ID]))

    async def broken(_: Request) -> Response:
        msg = "nobody expected this"
        raise RuntimeError(msg)

    routes = [*health(lifecycle, "abc123", prefix=prefix), Route(f"{prefix}/", page), Route(f"{prefix}/broken", broken)]
    return Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(Requests, health=[prefix])])


def test_health_answers_under_the_prefix_and_readiness_follows_the_lifecycle() -> None:
    lifecycle = Lifecycle()
    app = _app(lifecycle)
    with TestClient(app) as client:
        assert client.get(f"{PREFIX}/health/live").json() == {"status": "live", "commit": "abc123"}
        assert client.get(f"{PREFIX}/health/ready").json() == {"status": "ready", "commit": "abc123"}
        assert client.get("/health/live").status_code == HTTPStatus.NOT_FOUND
        lifecycle.drain()
        draining = client.get(f"{PREFIX}/health/ready")
        assert (draining.status_code, draining.json()) == (HTTPStatus.SERVICE_UNAVAILABLE, {"status": "draining"})
        assert client.get(f"{PREFIX}/health/live").status_code == HTTPStatus.OK
    assert lifecycle.stage is Stage.STOPPED


def test_a_lifecycle_only_moves_forward() -> None:
    lifecycle = Lifecycle()
    lifecycle.advance(Stage.STOPPING)
    lifecycle.advance(Stage.READY)
    assert lifecycle.stage is Stage.STOPPING


@pytest.mark.parametrize(("path", "prefix", "found"), [("/health/live", "", True), ("/apps/pos/health/ready", "/apps/pos/", True), ("/health/live", "/apps/pos", False), ("/healthy", "", False)])
def test_a_health_check_is_known_by_its_path_under_the_prefix(path: str, prefix: str, found: bool) -> None:  # noqa: FBT001 -- a parameter of the case
    assert is_health(path, prefix) is found


def test_the_boundary_gives_each_request_an_id_and_logs_one_record(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="basis.core.serving.requests")
    with TestClient(_app(Lifecycle())) as client:
        answered = client.get(f"{PREFIX}/")
        client.get(f"{PREFIX}/health/ready")
    assert answered.headers["x-request-id"] == answered.text
    assert UUID(answered.text).version == 7
    [record] = caplog.records  # the health check that passed logs nothing
    assert (record.getMessage(), record.levelno) == (f"GET {PREFIX}/: 200", logging.INFO)
    assert {name: getattr(record, name) for name in ("method", "path", "route", "status")} == {"method": "GET", "path": f"{PREFIX}/", "route": f"{PREFIX}/", "status": 200}


def test_an_error_nobody_expected_is_a_500_logged_once_with_its_traceback(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="basis.core.serving.requests")
    with TestClient(_app(Lifecycle()), raise_server_exceptions=False) as client:
        answered = client.get(f"{PREFIX}/broken")
    assert (answered.status_code, answered.json()) == (HTTPStatus.INTERNAL_SERVER_ERROR, {"status": 500})
    assert "x-request-id" in answered.headers
    [record] = caplog.records
    assert record.getMessage() == f"GET {PREFIX}/broken failed"
    assert record.exc_info is not None
    assert "nobody expected this" not in answered.text


def test_serve_settings_have_the_defaults_a_pod_fits_in_its_grace_period() -> None:
    assert Serve() == Serve(host="0.0.0.0", port=8000, drain_s=5, shutdown_s=15)  # noqa: S104 -- a container's
    assert serving.LOCAL == "127.0.0.1"


SERVER = """
import asyncio, sys
from contextlib import asynccontextmanager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from basis.core import logs
from basis.core.serving import Lifecycle, Serve, Stage
from basis.core.serving.requests import Requests, health
from basis.core.serving.server import run

logs.setup()
lifecycle = Lifecycle()

@asynccontextmanager
async def lifespan(_):
    lifecycle.advance(Stage.READY)
    yield
    lifecycle.advance(Stage.STOPPED)

async def slow(_):
    await asyncio.sleep(2)
    return PlainTextResponse("slept")

app = Starlette(routes=[*health(lifecycle, None, prefix="/apps/pos"), Route("/apps/pos/slow", slow)], lifespan=lifespan, middleware=[Middleware(Requests, health=["/apps/pos"])])
run(app, Serve(port=int(sys.argv[1]), drain_s=1, shutdown_s=10), lifecycle, host="127.0.0.1")
"""


@pytest.mark.skipif(sys.platform == "win32", reason="it signals the server as a pod's kubelet does, which Windows cannot: serving drains on SIGTERM on Linux alone")
def test_run_serves_any_asgi_app_and_drains_it_on_sigterm() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    environment = {key: value for key, value in os.environ.items() if not key.startswith("BASIS_")}
    process = subprocess.Popen([sys.executable, "-c", SERVER, str(port)], env=environment, stderr=subprocess.PIPE, text=True, start_new_session=True)
    base = f"http://127.0.0.1:{port}/apps/pos"
    try:
        deadline = time.monotonic() + 30
        while not _ready(base):
            assert process.poll() is None, process.communicate()[1]
            assert time.monotonic() < deadline, "the server was not ready within 30 seconds"
            time.sleep(0.1)
        with httpx2.Client(timeout=15) as client:
            process.send_signal(signal.SIGTERM)
            time.sleep(0.3)
            draining = client.get(f"{base}/health/ready")
            slow = client.get(f"{base}/slow")  # still served while it drains
        _, errors = process.communicate(timeout=20)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert (draining.status_code, slow.text) == (HTTPStatus.SERVICE_UNAVAILABLE, "slept")
    assert process.returncode == -signal.SIGTERM  # uvicorn raises the signal again once it has shut down, as it was asked to
    messages = [json.loads(line)["message"] for line in errors.splitlines()]
    assert "draining for 1.0s: readiness reports 503 while requests are still served" in messages
    assert "GET /apps/pos/slow: 200" in messages


def _ready(base: str) -> bool:
    try:
        return httpx2.get(f"{base}/health/ready", timeout=1).status_code == HTTPStatus.OK
    except httpx2.TransportError:
        return False
