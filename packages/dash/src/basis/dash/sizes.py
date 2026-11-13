"""Each callback's answer, measured: one log record per `POST _dash-update-component`, `answered OUTPUT`, with the
callback's output as Dash names it, the bytes of its JSON before compression, and the milliseconds the server took,
inside the request boundary, so the record carries the request's id. An answer past `BUDGET_BYTES` is a warning: 10,000
rows of 20 columns are about 3.9 MB of JSON (spike 14.1, question 11), and a page that sends more wants a narrower
query or the server-side row model (phase 18). `basis check` holds pages to the budget from 17.1."""

import logging
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

import orjson

log = logging.getLogger(__name__)

type Message = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]
type ASGI = Callable[[Message, Receive, Send], Awaitable[None]]

BUDGET_BYTES = 4 * 1024 * 1024
"""The largest answer that is not a warning: about 10,000 rows of 20 columns."""
_PEEK = 64 * 1024
"""The most bytes of a request read to find its output's name; a larger request is named by its path alone."""


class Sizes:
    """ASGI middleware that logs the size of each answer to `path`, Dash's callback route under the app's prefix."""

    def __init__(self, app: ASGI, *, path: str) -> None:
        self.app = app
        self.path = path

    async def __call__(self, scope: Message, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or scope.get("path") != self.path or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        sent = 0
        status = 0
        asked = bytearray()

        async def receiving() -> Message:
            message = await receive()
            if message.get("type") == "http.request" and len(asked) < _PEEK:
                asked.extend(message.get("body", b""))
            return message

        async def sending(message: Message) -> None:
            nonlocal sent, status
            if message.get("type") == "http.response.start":
                status = int(message.get("status", 0))
            elif message.get("type") == "http.response.body":
                sent += len(message.get("body", b""))
            await send(message)

        await self.app(scope, receiving, sending)
        output = _output(bytes(asked))
        fields = {"output": output, "bytes": sent, "status": status, "ms": round((time.perf_counter() - started) * 1000, 3)}
        if sent > BUDGET_BYTES:
            log.warning("answered %s with %.1f MB, more than the %.0f MB budget", output, sent / 1_048_576, BUDGET_BYTES / 1_048_576, extra=fields)
        else:
            log.info("answered %s", output, extra=fields)


def _output(asked: bytes) -> str:
    """The callback's output, as the request names it, or `unknown` for a request that is not one basis can read."""
    try:
        found = orjson.loads(asked)
    except orjson.JSONDecodeError:
        return "unknown"
    output = found.get("output") if isinstance(found, dict) else None
    return output if isinstance(output, str) else "unknown"
