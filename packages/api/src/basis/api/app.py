"""Building a service's FastAPI app, the same way for every service:

- its settings, read once here, so that a missing one stops the app before it serves, its resources' included;
- the request boundary (`requests.py`): request ids, log fields, `traceparent` out, and the error no handler expected;
- one exception map (`errors.problem`) for every error a handler or FastAPI raises;
- FastAPI's own telemetry, on the app's tracer provider (`traces.py`), which reads `traceparent` in and makes each
  request's server span;
- a lifespan that adds the settings' span exporter, opens the service's resources (`opened.py`), marks the app ready,
  and on shutdown closes the resources and flushes the spans;
- `/health/live`, which answers while the process does, and `/health/ready`, which answers 200 only between the
  resources being open and shutdown beginning, and a 503 problem before and after.

`server.py` runs the result with uvicorn, and tests run it with FastAPI's `TestClient`:

    app = api.build(service, settings=Settings.given(), fakes={WAREHOUSE: fakes.Database(answers=...)})  # never the environment
    with TestClient(app.asgi) as client:  # runs the lifespan, as `basis serve` does
        client.get("/accrued-interest", params=...)
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal
from uuid import UUID

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from opentelemetry.sdk.trace import TracerProvider
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import Response

from basis.api import errors, opened, traces
from basis.api.fakes import Fake
from basis.api.requests import HEALTH, REQUEST_ID, Requests
from basis.api.resources import Resource
from basis.api.service import STATE, Runtime, Service, ServiceError
from basis.api.settings import Settings, read
from basis.core.model import Model

log = logging.getLogger(__name__)


class Stage(StrEnum):
    STARTING = "starting"
    READY = "ready"
    DRAINING = "draining"
    """Shutting down, but still serving, so that the cluster stops sending requests before the server stops taking them."""
    STOPPING = "stopping"
    STOPPED = "stopped"


_ORDER = tuple(Stage)


class Lifecycle:
    """Where an app is between being built and stopping, which its readiness reports. It only moves forward."""

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


@dataclass(frozen=True)
class App:
    service: Service
    settings: Settings
    asgi: FastAPI
    """The ASGI app, for uvicorn or a `TestClient`."""
    lifecycle: Lifecycle
    provider: TracerProvider
    """The app's own tracer provider, every span of the app's on it, to which a test may add a processor of its own."""


class Health(Model):
    status: Literal["live", "ready"]


def build(service: Service, *, settings: Settings | None = None, fakes: Mapping[Resource, Fake] | None = None) -> App:
    """The app for `service`, with `settings`, or its settings read from the environment, and its resources opened
    as the settings configure them, or as `fakes` gives them, by resource. An app starts once."""
    if settings is None:
        settings = read(service.settings)
    elif not isinstance(settings, service.settings):
        msg = f"service {service.name}'s settings are {service.settings.__qualname__}, so build it with those, not a {type(settings).__qualname__}"
        raise ServiceError(msg)
    configured = settings
    planned = opened.plan(service, configured, fakes or {})
    writes = {route.handler: route.writes for route in service.routes if route.writes is not None}
    lifecycle = Lifecycle()
    provider = traces.provider(service.name)
    tracer = provider.get_tracer("basis.api")
    started = False

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[Mapping[str, object]]:
        nonlocal started
        if started:
            msg = f"service {service.name}'s app has started once already: build another with api.build"
            raise ServiceError(msg)
        started = True
        if (exporter := traces.exporter(configured.traces)) is not None:
            provider.add_span_processor(exporter)
        try:
            resources = await opened.Resources.open(planned, tracer)
        except BaseException:
            await asyncio.to_thread(provider.shutdown)  # the failed open's spans too
            raise
        lifecycle.advance(Stage.READY)
        log.info(
            "%s is ready",
            service.name,
            extra={"service": service.name, "traces": configured.traces.exporter if configured.traces is not None else None, "resources": [resource.name for resource in service.resources]},
        )
        try:
            yield {STATE: Runtime(configured, resources, writes)}
        finally:
            lifecycle.advance(Stage.STOPPING)
            try:
                await resources.close()
            finally:
                await asyncio.to_thread(provider.shutdown)  # exports the spans still queued, which blocks
            lifecycle.advance(Stage.STOPPED)
            log.info("%s stopped", service.name, extra={"service": service.name})

    app = FastAPI(
        title=service.name,
        lifespan=lifespan,
        middleware=[Middleware(Requests)],
        exception_handlers={errors.Error: _problem, HTTPException: _problem, RequestValidationError: _problem},
        telemetry={
            "tracer_provider": provider,
            "metrics": False,
            "logs": False,  # basis logs through basis-core, once per error
            "operation_spans": configured.traces is not None,  # spans for dependencies, the handler and serialization, when exported
            "auto_configure": False,  # the settings, not OTEL_* variables, say where spans go
            "exclude": _health_check,
        },
    )

    async def live() -> Health:
        return Health(status="live")

    async def ready() -> Health:
        if lifecycle.stage is not Stage.READY:
            msg = f"the service is {lifecycle.stage}"
            raise errors.NotReadyError(msg)
        return Health(status="ready")

    app.add_api_route(f"{HEALTH}live", live, methods=["GET"], include_in_schema=False)
    app.add_api_route(f"{HEALTH}ready", ready, methods=["GET"], include_in_schema=False)
    for route in service.routes:
        app.add_api_route(route.path, route.handler, methods=[route.method], status_code=route.status, name=route.name)
    return App(service, configured, app, lifecycle, provider)


async def _problem(request: Request, error: Exception) -> Response:
    request_id = request.scope.get(REQUEST_ID)
    return errors.response(error, request_id if isinstance(request_id, UUID) else None)


def _health_check(scope: Mapping[str, object]) -> bool:
    path = scope.get("path")
    return isinstance(path, str) and path.startswith(HEALTH)
