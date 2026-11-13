"""`basis serve` as a process: it serves the app under its prefix, and on SIGTERM drains, then stops. The only tests
of basis-dash that open a socket, on the loopback interface, to a child process."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from http import HTTPStatus
from pathlib import Path

import httpx2
import pytest

APP = """
import basis.dash as bd
from dash import html

app = bd.App(name="served", title="Served", settings=bd.Settings, pages=[bd.page("/", "Served", layout=html.Div("served", id="served"))])
"""


@pytest.mark.skipif(sys.platform == "win32", reason="it signals the server as a pod's kubelet does, which Windows cannot: serving drains on SIGTERM on Linux alone")
def test_serve_serves_the_app_under_its_prefix_and_drains_on_sigterm(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "served"\nversion = "0.1.0"\n\n[tool.basis]\nparts = ["dash"]\n', encoding="utf-8")
    (tmp_path / "served_app.py").write_text(APP, encoding="utf-8")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    settings = {"ENV": "dev", "SERVE__HOST": "127.0.0.1", "SERVE__PORT": str(port), "SERVE__DRAIN_S": "1", "SERVE__PREFIX": "/apps/served/", "GIT_SHA": "abc123"}
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("BASIS_", "DASH_"))}
    environment |= {f"BASIS_{key}": value for key, value in settings.items()} | {"XDG_CONFIG_HOME": str(tmp_path / "config"), "PYTHONPATH": str(tmp_path)}
    argv = [str(Path(sys.executable).with_name("basis")), "serve", "served_app"]
    process = subprocess.Popen(argv, cwd=tmp_path, env=environment, stderr=subprocess.PIPE, text=True, start_new_session=True)
    base = f"http://127.0.0.1:{port}/apps/served"
    try:
        deadline = time.monotonic() + 30
        while not _ready(base):
            assert process.poll() is None, process.communicate()[1]
            assert time.monotonic() < deadline, "the app was not ready within 30 seconds"
            time.sleep(0.1)
        index = httpx2.get(f"{base}/", timeout=5)
        process.send_signal(signal.SIGTERM)
        time.sleep(0.3)
        draining = httpx2.get(f"{base}/health/ready", timeout=5)
        _, errors = process.communicate(timeout=20)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert (index.status_code, "Served" in index.text) == (HTTPStatus.OK, True)
    assert draining.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert process.returncode == 128 + signal.SIGTERM  # as a shell reports a stop by SIGTERM
    lines = errors.splitlines()
    assert lines[-1] == "ERROR: basis serve was stopped by a signal before it finished: run it again, and let it finish"  # as an API's: uvicorn raises the signal again once it has shut down
    records = [json.loads(line) for line in lines[:-1]]
    messages = [str(record["message"]) for record in records if not str(record["message"]).startswith("GET /apps/served/health/ready")]
    assert messages[:2] == [f"serving served on 127.0.0.1:{port}/apps/served", "served is ready"]
    assert "draining for 1.0s: readiness reports 503 while requests are still served" in messages
    assert "served stopped" in messages
    assert "GET /apps/served/: 200" in messages
    assert all(record.get("service") == "served" for record in records[1:])


def _ready(base: str) -> bool:
    try:
        return httpx2.get(f"{base}/health/ready", timeout=1).status_code == HTTPStatus.OK
    except httpx2.TransportError:
        return False
