"""Building a Dash app from its declaration, the same way for every app, as basis-api builds a service's:

- its resources and datasets, checked before anything else, each query's SQL file against its declared params
  (`basis.dash.declarations.check`), then opened, or their fakes, which connects to nothing (`basis.dash.loads`);
- its settings, read once here, so that a missing one stops the app before it serves;
- Dash on its FastAPI backend, on a FastAPI server of basis's own, with every setting Dash would otherwise read from a
  `DASH_*` variable given (`enable_mcp=False`, `shared_storage=None`, `compress=False`, `serve_locally=True`), its
  log handler left out, so its records go through basis-core's, and `on_error` basis's (`basis.dash.errors`);
- the app's path prefix (`serve.prefix`), as Dash's `requests_pathname_prefix` where the proxy strips it, and its
  `url_base_pathname` where it keeps it (spike 14.1, question 1);
- the app's pages registered with Dash Pages, the process's registry holding them alone, inside basis's chrome
  (`basis.dash.shell`), and the callbacks of the app's own modules, its declaration's and its pages'
  (`internals.own_callbacks`);
- basis-core's health routes, at the server's root for the cluster's probes and, where the proxy keeps the prefix,
  under it too; the request boundary, outermost but for gzip at level 1; FastAPI's telemetry on the app's own tracer
  provider; and a lifespan that adds the settings' span exporter, marks the app ready, and on shutdown flushes the
  spans;
- the server set up at once, so a callback's setup error fails the build, not the first request.

`basis.dash.server` runs the result with uvicorn, and tests run it with FastAPI's `TestClient` (`basis.dash.testing`).
Dash keeps one app per process (`dash.get_app`, and the registry of pages), so the app built last is the process's."""

import asyncio
import logging
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from opentelemetry.sdk.trace import TracerProvider
from starlette.middleware.gzip import GZipMiddleware

import dash
from basis.core import traces
from basis.core.serving import Lifecycle, Stage
from basis.core.serving import requests as boundary
from basis.dash import declarations, errors, internals, shell
from basis.dash.app import App
from basis.dash.errors import DeclarationError
from basis.dash.runtime import Runtime
from basis.dash.settings import Settings

log = logging.getLogger(__name__)

COMPRESS_FROM = 1024
"""The smallest response body, in bytes, the app compresses for a client that accepts gzip."""
COMPRESS_LEVEL = 1
"""gzip's level 1, as an API's: 25 ms for 10,000 rows of 20 columns where Dash's own, level 9, takes 340 ms for a
result 21% smaller (spike 14.1, question 11)."""


@dataclass(frozen=True)
class Built:
    app: App
    settings: Settings
    dash: dash.Dash
    asgi: FastAPI
    """The ASGI app, for uvicorn or a `TestClient`."""
    lifecycle: Lifecycle
    provider: TracerProvider
    """The app's own tracer provider, every span of the app's on it, to which a test may add a processor of its own."""
    runtime: Runtime
    """What the app opened for its datasets, and their cache."""


def build(app: App, *, settings: Settings | None = None, environment: str | None = None, dev_tools: bool = False, fakes: Mapping[Any, object] | None = None) -> Built:
    """`app`, built, with `settings`, or its settings read from the environment. `environment` names the environment
    the settings are of, which its spans carry. `dev_tools` turns on Dash's dev tools and hot reload, which `basis dev`
    alone asks for. `fakes`, by resource or its name, stand in for every resource the app lists, or none. An app starts once.
    Its resources and datasets are checked first, every SQL file opened, before any environment is read."""
    datasets = declarations.check(app.name, app.resources)
    configured = _settings(app, settings)
    serve = configured.serve
    lifecycle = Lifecycle()
    provider = traces.provider(app.name, version=configured.git_sha, environment=environment)
    runtime = Runtime(app.name, datasets=datasets, resources=app.resources, settings=configured, tracer=provider.get_tracer("basis.dash"), fakes=fakes)
    started = False

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[Mapping[str, object]]:
        nonlocal started
        if started:
            msg = f"app {app.name} has started once already: build another with bd.build"
            raise DeclarationError(msg)
        started = True
        if (exporter := traces.exporter(configured.traces)) is not None:
            provider.add_span_processor(exporter)
        runtime.start()
        lifecycle.advance(Stage.READY)
        log.info("%s is ready", app.name, extra={"app": app.name, "traces": configured.traces.exporter if configured.traces is not None else None})
        try:
            yield {}
        finally:
            runtime.stop()
            await runtime.close()
            lifecycle.advance(Stage.STOPPING)
            await asyncio.to_thread(provider.shutdown)  # exports the spans still queued, which blocks
            lifecycle.advance(Stage.STOPPED)
            log.info("%s stopped", app.name, extra={"app": app.name})

    kept = "" if serve.prefix == "/" or serve.prefix_stripped else serve.prefix.rstrip("/")
    health = ("", kept) if kept else ("",)
    server = FastAPI(
        title=app.name,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        telemetry={
            "tracer_provider": provider,
            "metrics": False,
            "logs": False,  # basis logs through basis-core, once per error
            "operation_spans": False,
            "auto_configure": False,  # the settings, not OTEL_* variables, say where spans go
            "exclude": lambda scope: _health_check(scope, health),
        },
    )
    for prefix in health:  # before Dash's routes, whose catch-all would answer them with its index
        server.router.routes.extend(boundary.health(lifecycle, configured.git_sha, prefix=prefix))
    base, requests, routes = _prefixes(serve.prefix, stripped=serve.prefix_stripped)
    made = dash.Dash(
        app.module,
        server=internals.server_of(server),
        title=app.title,
        update_title="",  # the tab keeps the page's title while a callback runs
        use_pages=True,
        pages_folder="",
        include_pages_meta=False,
        serve_locally=True,
        compress=False,
        enable_mcp=False,
        shared_storage=None,
        add_log_handler=False,
        on_error=errors.on_error,
        suppress_callback_exceptions=False,
        url_base_pathname=base,
        requests_pathname_prefix=requests,
        routes_pathname_prefix=routes,
    )
    made.logger.setLevel(logging.NOTSET)  # Dash sets INFO on its own logger, which basis-core's level governs instead
    if dev_tools:
        made.enable_dev_tools(debug=True, dev_tools_hot_reload=True, dev_tools_disable_version_check=True)  # the check calls plotly.com
    internals.forget_pages()
    for index, page in enumerate(app.pages):
        dash.register_page(f"{app.name}.page{index}", path=page.path, title=f"{page.title} \N{MIDDLE DOT} {app.title}", name=page.title, order=index, layout=page.layout)
    dash.register_page(f"{app.name}.not_found_404", path=shell.NOT_FOUND, title=f"Not found \N{MIDDLE DOT} {app.title}", name="Not found", layout=shell.not_found(app))
    made.layout = shell.chrome(app, environment=environment)
    shell.wire(made)
    internals.own_callbacks(made, {app.module, *(page.module for page in app.pages)})
    internals.set_up(made)
    server.add_middleware(boundary.Requests, health=health)
    server.add_middleware(GZipMiddleware, minimum_size=COMPRESS_FROM, compresslevel=COMPRESS_LEVEL)  # outside the boundary, which logs what the app answered
    return Built(app, configured, made, server, lifecycle, provider, runtime)


def _settings(app: App, settings: Settings | None) -> Settings:
    """`settings`, which must be the app's, or the app's read from the environment."""
    if settings is None:
        return app.settings()
    if not isinstance(settings, app.settings):
        msg = f"app {app.name}'s settings are {app.settings.__qualname__}, so build it with those, not a {type(settings).__qualname__}"
        raise DeclarationError(msg)
    return settings


def _prefixes(prefix: str, *, stripped: bool) -> tuple[str | None, str | None, str | None]:
    """Dash's `url_base_pathname`, `requests_pathname_prefix` and `routes_pathname_prefix` for the app's prefix."""
    if stripped and prefix != "/":
        return None, prefix, "/"
    return prefix, None, None


def _health_check(scope: Mapping[str, object], prefixes: tuple[str, ...]) -> bool:
    path = scope.get("path")
    return isinstance(path, str) and any(boundary.is_health(path, prefix) for prefix in prefixes)
