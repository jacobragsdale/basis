"""`basis serve` and `basis dev`: run a service's app with uvicorn, logging JSON lines through basis-core, until it is
told to stop. `basis dev` runs it on the service's declared fakes (`dev.py`), and only for this machine, on 127.0.0.1.

A graceful shutdown, on SIGTERM as Kubernetes sends it, goes in three steps. First the app drains: readiness reports
503 while the server keeps serving, for `serve.drain_s` seconds, so that the cluster takes the pod out of its
endpoints before the server stops taking requests. Then uvicorn closes its sockets and gives the requests in flight
`serve.shutdown_s` seconds to finish. Then the app's lifespan shuts down, which flushes its spans. Ctrl+C skips the
drain, as a desktop has no load balancer to wait for. A second signal, of either kind, stops at once: requests in
flight are cut off, and the lifespan's shutdown is skipped."""

import asyncio
import logging
import signal
import socket
import time
from types import FrameType
from typing import override

import uvicorn

from basis.api.app import Lifecycle, build
from basis.api.fakes import Fakes
from basis.api.service import Service, load
from basis.api.settings import Settings
from basis.core import logs

log = logging.getLogger(__name__)

LOCAL = "127.0.0.1"
"""Where `basis dev` serves: this machine alone."""


def serve(service: Service | str | None = None, *, settings: Settings | None = None, fakes: Fakes | None = None, host: str | None = None) -> None:
    """Serve `service`, or the service a reference names (see `api.load`), or the one the installed packages list,
    with `settings`, or its settings read from the environment, on `fakes` where given, and on `host`, or the one the
    settings name."""
    logs.setup()
    found = service if isinstance(service, Service) else load(service)
    logged(found)
    app = build(found, settings=settings, fakes=fakes)
    serving = app.settings.serve
    host = host if host is not None else serving.host
    config = uvicorn.Config(
        app.asgi,
        host=host,
        port=serving.port,
        lifespan="on",  # a lifespan that fails stops the server before it takes a request
        log_config=None,  # uvicorn's records go through basis-core's handler, as JSON, and only its warnings and up
        access_log=False,  # the request boundary logs each request, with its id
        timeout_graceful_shutdown=serving.shutdown_s,
        server_header=False,
    )
    on = " on its fakes" if fakes is not None else ""
    log.info("serving %s on %s:%s%s", found.name, host, serving.port, on, extra={"host": host, "port": serving.port, "fakes": fakes is not None})
    try:
        _Server(config, app.lifecycle, drain_s=serving.drain_s).run()
    except KeyboardInterrupt:  # Ctrl+C, which uvicorn raises again once it has shut down: the stop that was asked for
        return


def serve_on_fakes(service: Service | str | None = None, *, settings: Settings | None = None) -> None:
    """`api.serve_on_fakes`: serve `service` on its fakes, on this machine alone, as `basis dev` does: with `settings`, or its settings read
    from the environment, so a caller in Python, such as a platform's own command, gives them in code."""
    from basis.api import dev as fakes_of  # noqa: PLC0415 -- the fakes load when it runs

    found = service if isinstance(service, Service) else load(service)
    serve(found, settings=settings, fakes=fakes_of.fakes(found), host=LOCAL)


def logged(service: Service) -> None:
    """Log as `service`: its own loggers at the configured level, as basis's are, and its name on every record."""
    for package in {route.handler.__module__.partition(".")[0] for route in service.routes}:
        logs.include(package)
    logs.bind(service=service.name)


class _Server(uvicorn.Server):
    """uvicorn's server, which drains first on SIGTERM, and stops at once on a second signal. A signal handler only
    sets attributes and hands work to the loop; the loop's tick, which uvicorn runs ten times a second, does the rest,
    since logging from a signal handler can deadlock."""

    def __init__(self, config: uvicorn.Config, lifecycle: Lifecycle, *, drain_s: float) -> None:
        super().__init__(config)
        self._lifecycle = lifecycle
        self._drain_s = drain_s
        self._drain: tuple[int, FrameType | None, float] | None = None  # the signal, its frame and when the drain ends
        self._signalled = False
        self._logged = False
        self._loop: asyncio.AbstractEventLoop | None = None

    @override
    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        self._loop = asyncio.get_running_loop()
        await super().serve(sockets)

    @override
    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self._lifecycle.drain()
        if self._signalled:  # a second signal: stop now, as uvicorn does only for a second Ctrl+C
            super().handle_exit(sig, frame)
            self.should_exit = self.force_exit = True
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._cut_off)  # on the loop, not in the signal handler
            return
        self._signalled = True
        if sig == signal.SIGTERM and self._drain_s > 0:
            self._drain = (sig, frame, time.monotonic() + self._drain_s)
            return
        super().handle_exit(sig, frame)  # Ctrl+C, or no drain: uvicorn's graceful shutdown, now

    @override
    async def on_tick(self, counter: int) -> bool:
        if (drain := self._drain) is not None and not self.should_exit:
            if not self._logged:
                self._logged = True
                log.info("draining for %ss: readiness reports 503 while requests are still served", self._drain_s, extra={"drain_s": self._drain_s})
            if time.monotonic() >= drain[2]:
                super().handle_exit(drain[0], drain[1])
        return await super().on_tick(counter)

    def _cut_off(self) -> None:
        """Close every connection, the ones with a request in flight too: uvicorn's forced exit stops waiting for
        requests, but its sockets still wait for their connections to close."""
        for connection in list(self.server_state.connections):
            connection.transport.close()
