"""Building a service's FastAPI app, the same way for every service:

- its settings, read once here, so that a missing one stops the app before it serves, its resources' included;
- the request boundary (`requests.py`): request ids, log fields, `traceparent` out, and the error no handler expected;
- inside it, for a service that declares a user, each request's bearer token verified (`tokens.py`);
- outside it, the answers to the pages of the other origins the settings allow, if any (`origins.py`);
- one exception map (`errors.problem`) for every error a handler or FastAPI raises;
- FastAPI's own telemetry, on the app's tracer provider (`basis.core.traces`), which reads `traceparent` in and makes
  each request's server span;
- a lifespan that adds the settings' span exporter, opens the service's resources (`opened.py`) and starts its
  compute pool (`pool.py`), marks the app ready, and on shutdown closes them and flushes the spans;
- each route as `routes.py` serves it: within its deadline, reading its request strictly, and answering only the errors
  it declares, which its OpenAPI operation lists, in the document `openapi.py` finishes;
- `/health/live`, which answers while the process does, and `/health/ready`, which answers 200 only between the
  resources being open and shutdown beginning, and a 503 problem before and after.

`server.py` runs the result with uvicorn, and tests run it with FastAPI's `TestClient`:

    app = api.build(service, settings=Settings.given(), fakes={WAREHOUSE: fakes.Database(answers=...)})  # never the environment
    with TestClient(app.asgi) as client:  # runs the lifespan, as `basis serve` does
        client.get("/accrued-interest", params=...)
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal
from uuid import UUID

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from opentelemetry.sdk.trace import TracerProvider
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from basis.api import errors, openapi, opened, origins, routes, tokens, ui
from basis.api.fakes import Fakes
from basis.api.requests import HEALTH, REQUEST_ID, Requests
from basis.api.service import STATE, DeclarationError, Runtime, Service
from basis.api.settings import Settings, read
from basis.core import traces
from basis.core.errors import ServiceError
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


COMPRESS_FROM = 1024
"""The smallest response body, in bytes, the app compresses for a client that accepts gzip: its JSON, its Arrow, and a
UI file the build wrote no compressed copy of. One already compressed, as a UI file's copy is, is sent as it is."""
COMPRESS_LEVEL = 1
"""gzip's level 1, measured on 100,000 rows (docs/roadmap.md, Later): Arrow 7.2 MB to 3.44 MB in 63 ms, where level 6
gives 3.28 MB in 290 ms and level 9, Starlette's default, 3.23 MB in 1.76 s; JSON 20.9 MB to 5.00 MB in 82 ms, 4.23 MB
in 254 ms at 6. Over a slow link the bytes cost more than the CPU at every level but 1, and a body over 128 KiB is
compressed off the event loop."""


class Health(Model):
    status: Literal["live", "ready"]
    commit: str | None = None
    """The commit the app runs, `BASIS_GIT_SHA`, which `basis smoke --commit` checks; None where it is not set."""


def build(service: Service, *, settings: Settings | None = None, fakes: Fakes | None = None, environment: str | None = None) -> App:
    """The app for `service`, with `settings`, or its settings read from the environment, and its resources opened
    as the settings configure them, or as `fakes` gives them, by resource or compute function. `environment` names
    the environment the settings are of, as `--env` gave it, which its spans carry. An app starts once."""
    if settings is None:
        settings = read(service.settings)
    elif not isinstance(settings, service.settings):
        msg = f"service {service.name}'s settings are {service.settings.__qualname__}, so build it with those, not a {type(settings).__qualname__}"
        raise DeclarationError(msg)
    configured = settings
    planned = opened.plan(service, configured, fakes or {})
    identity = tokens.plan(service, configured)
    writes = {route.handler: route.writes for route in service.routes if route.writes is not None}
    lifecycle = Lifecycle()
    provider = traces.provider(service.name, version=configured.git_sha, environment=environment)
    tracer = provider.get_tracer(traces.SCOPE)
    started = False

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[Mapping[str, object]]:
        nonlocal started
        if started:
            msg = f"service {service.name}'s app has started once already: build another with api.build"
            raise DeclarationError(msg)
        started = True
        if (exporter := traces.exporter(configured.traces)) is not None:
            provider.add_span_processor(exporter)
        try:
            resources = await opened.Resources.open(planned, tracer, identity)  # the identity's keys first, then the resources
        except BaseException:
            await asyncio.to_thread(provider.shutdown)  # the failed open's spans too
            raise
        lifecycle.advance(Stage.READY)
        log.info(
            "%s is ready",
            service.name,
            extra={
                "service": service.name,
                "traces": configured.traces.exporter if configured.traces is not None else None,
                "resources": [resource.name for resource in service.resources],
                "identity": configured.identity.keys.kind if configured.identity is not None else None,
            },
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

    app = openapi.Application(
        title=service.name,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        middleware=[
            *origins.middleware(configured.allowed_origins),
            Middleware(GZipMiddleware, minimum_size=COMPRESS_FROM, compresslevel=COMPRESS_LEVEL),  # outside the request boundary, which logs what the handler answered
            Middleware(Requests),
            *([Middleware(tokens.Bearer, verifier=identity)] if identity is not None else []),  # Bearer inside Requests
        ],
        exception_handlers={errors.Error: _problem, ServiceError: _problem, HTTPException: _problem, RequestValidationError: _problem},
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
        return Health(status="live", commit=configured.git_sha)

    async def ready() -> Health:
        if lifecycle.stage is not Stage.READY:
            msg = f"the service is {lifecycle.stage}"
            raise errors.NotReadyError(msg)
        return Health(status="ready", commit=configured.git_sha)

    _health(app, service, live, ready)
    _routes(app, service, docs=configured.serve.docs)
    return App(service, configured, app, lifecycle, provider)


def _health(app: openapi.Application, service: Service, live: Callable[[], Awaitable[Health]], ready: Callable[[], Awaitable[Health]]) -> None:
    """The health endpoints, and for an app that serves a UI, them and the document under /api too, the base its
    document's paths are under, where `basis smoke` reaches them."""

    async def document() -> JSONResponse:
        return JSONResponse(app.openapi())

    for prefix in ("", routes.API) if service.ui is not None else ("",):
        app.add_api_route(f"{prefix}{HEALTH}live", live, methods=["GET"], include_in_schema=False)
        app.add_api_route(f"{prefix}{HEALTH}ready", ready, methods=["GET"], include_in_schema=False)
    if service.ui is not None:
        app.add_api_route(f"{routes.API}/openapi.json", document, methods=["GET"], include_in_schema=False)


SWAGGER_UI = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.17.14"
"""Swagger UI's files, at one exact version, so a page of `/docs` loads the script it loaded when the version was
chosen, never whatever the CDN calls the latest."""


def _docs(app: openapi.Application, service: Service) -> None:
    """`/docs`, Swagger UI's page of the app's document, for a developer who turns it on (`serve.docs`)."""
    document = f"{routes.API}/openapi.json" if service.ui is not None else "/openapi.json"

    async def docs() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url=document, title=f"{service.name} docs", swagger_js_url=f"{SWAGGER_UI}/swagger-ui-bundle.js", swagger_css_url=f"{SWAGGER_UI}/swagger-ui.css")

    app.add_api_route("/docs", docs, methods=["GET"], include_in_schema=False)


def _routes(app: openapi.Application, service: Service, *, docs: bool) -> None:
    """The service's routes on `app`: at their own paths, or, for an app that serves a UI, under /api, with the
    document theirs as the repo commits it, and the UI at / after every route; and `/docs` where the settings ask."""
    if docs:
        _docs(app, service)
    if service.ui is None:
        routes.add(app, service)
        return
    routes.add(app, service, prefix=routes.API)
    app._document = openapi.document(service)  # noqa: SLF001 -- the one document, unprefixed, which the app serves
    app.mount("/", ui.Folder(service.ui, [route.path for route in service.routes]), name="ui")


async def _problem(request: Request, error: Exception) -> Response:
    if isinstance(error, ServiceError) and (cause := error.__cause__) is not None:  # what a driver said, which the problem never holds
        log.warning("%s: %s", error, cause, extra={"resource": error.resource, "kind": type(error).__name__, "reason": f"{type(cause).__qualname__}: {cause}"})
    request_id = request.scope.get(REQUEST_ID)
    return errors.response(error, request_id if isinstance(request_id, UUID) else None)


def _health_check(scope: Mapping[str, object]) -> bool:
    path = scope.get("path")
    return isinstance(path, str) and path.startswith(HEALTH)
