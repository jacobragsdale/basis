"""What every server basis builds shares, an API's and a Dash app's alike, with `basis-core[serve]`, which brings
uvicorn and Starlette. A part that serves takes the extra; nothing `basis --version` or a job's commands import loads
any of it.

- This module: how a server serves (`Serve`, the settings' `serve` block), the stages of readiness (`Lifecycle`) and
  what the health routes answer (`Health`). It imports no server, so a part's settings and declarations may use it.
- `basis.core.serving.requests`: the health routes, `PREFIX/health/live` and `PREFIX/health/ready`, and the request
  boundary, `Requests`, for any ASGI app on Starlette.
- `basis.core.serving.server`: uvicorn's runner, `run`, with its three-step shutdown and the second signal.

`/health/live` answers while the process does, and `/health/ready` answers 200 only while the lifecycle is ready, each
with the commit the app runs, under the app's path prefix where a proxy in front of it keeps it (spike 14.1,
question 1)."""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from basis.core.model import Model

LOCAL = "127.0.0.1"
"""Where `basis dev` serves: this machine alone."""
HEALTH = "/health/"
"""The health routes' path, under an app's prefix where it has one: `live` and `ready` follow it."""


class Serve(Model):
    """How a server serves: the settings' `serve` block, `BASIS_SERVE__PORT` and the rest. A part's settings may
    subclass it with what its own server adds."""

    host: str = "0.0.0.0"  # noqa: S104 -- `basis serve` runs in a container, whose network decides who reaches it
    port: Annotated[int, Field(ge=1, le=65535)] = 8000
    drain_s: Annotated[float, Field(ge=0)] = 5
    """Seconds the server keeps serving after SIGTERM while readiness reports it not ready, so that the cluster stops
    sending it requests before it stops taking them. Ctrl+C stops without them."""
    shutdown_s: Annotated[int, Field(ge=1)] = 15
    """Seconds the requests still in flight then have to finish, before they are cancelled. With `drain_s` and the
    last export of spans, which `traces.timeout_s` bounds, the defaults fit in Kubernetes' 30-second grace period."""


class Stage(StrEnum):
    STARTING = "starting"
    READY = "ready"
    DRAINING = "draining"
    """Shutting down, but still serving, so that the cluster stops sending requests before the server stops taking them."""
    STOPPING = "stopping"
    STOPPED = "stopped"


_ORDER = tuple(Stage)


class Lifecycle:
    """Where an app is between being built and stopping, which its readiness reports. It only moves forward: an app's
    lifespan advances it to ready once its resources are open, and to stopping and stopped as it closes them."""

    def __init__(self) -> None:
        self._stage = Stage.STARTING

    @property
    def stage(self) -> Stage:
        return self._stage

    def advance(self, stage: Stage) -> None:
        if _ORDER.index(stage) > _ORDER.index(self._stage):
            self._stage = stage

    def drain(self) -> None:
        """Report not ready from now on, while still serving: the first step of a graceful shutdown."""
        self.advance(Stage.DRAINING)


class Health(Model):
    status: Literal["live", "ready"]
    commit: str | None = None
    """The commit the app runs, `BASIS_GIT_SHA`, which `basis smoke --commit` checks; None where it is not set."""
