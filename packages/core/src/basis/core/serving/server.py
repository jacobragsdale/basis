"""uvicorn's runner, for any ASGI app basis builds, an API's and a Dash app's alike.

A graceful shutdown, on SIGTERM as Kubernetes sends it, goes in three steps. First the app drains: readiness reports
503 while the server keeps serving, for `serve.drain_s` seconds, so that the cluster takes the pod out of its endpoints
before the server stops taking requests. Then uvicorn closes its sockets and gives the requests in flight
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
from starlette.types import ASGIApp

from basis.core.serving import Lifecycle, Serve

log = logging.getLogger(__name__)


def run(asgi: ASGIApp, serve: Serve, lifecycle: Lifecycle, *, host: str | None = None) -> None:
    """Serve `asgi` with uvicorn on `host`, or the one `serve` names, at its port, until it is told to stop, as the
    module's docstring says, with `lifecycle` the app's, which a SIGTERM drains first. uvicorn logs through basis-core's
    handler, as JSON, and the request boundary logs each request, so uvicorn's access log is off."""
    config = uvicorn.Config(
        asgi,
        host=serve.host if host is None else host,
        port=serve.port,
        lifespan="on",  # a lifespan that fails stops the server before it takes a request
        log_config=None,  # uvicorn's records go through basis-core's handler, as JSON, and only its warnings and up
        access_log=False,  # the request boundary logs each request, with its id
        timeout_graceful_shutdown=serve.shutdown_s,
        server_header=False,
    )
    try:
        Server(config, lifecycle, drain_s=serve.drain_s).run()
    except KeyboardInterrupt:  # Ctrl+C, which uvicorn raises again once it has shut down: the stop that was asked for
        return


class Server(uvicorn.Server):
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
