"""`basis serve` and `basis dev`: run an app with uvicorn, logging JSON lines through basis-core, until it is told to
stop. `basis serve` runs it on basis-core's runner (`basis.core.serving.server`), whose SIGTERM drains first; `basis
dev` runs it for this machine alone, on 127.0.0.1, with uvicorn's reloader and Dash's dev tools and hot reload, which
`basis serve` never has.

Dash reads 24 `DASH_*` variables, and its FastAPI backend turns its dev tools on from `_DASH_FASTAPI_CONFIG` on its
first request, whatever the app was built with. basis passes Dash every setting itself, and both verbs refuse to start
while any of them is set, so no variable changes what an app does (spike 14.1, question 9)."""

import logging
import os
from collections.abc import Mapping
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from basis.core import logs
from basis.core.serving import LOCAL
from basis.core.serving.server import run
from basis.core.settings import SettingsError
from basis.dash import internals
from basis.dash.app import App
from basis.dash.building import build
from basis.dash.loading import load
from basis.dash.settings import Settings

log = logging.getLogger(__name__)

DEV_APP = "_BASIS_DASH_DEV_APP"
"""The app `basis dev` serves, as its reference, for the process uvicorn's reloader starts, which builds it again."""


def serve(app: App | str | None = None, *, settings: Settings | None = None, host: str | None = None, environment: str | None = None) -> None:
    """Serve `app`, or the app a reference names (see `basis.dash.loading.load`), or the one the installed packages list, with
    `settings`, or its settings read from the environment, on `host`, or the one the settings name; `environment` names
    the environment the settings are of, which its spans carry."""
    logs.setup()
    refuse_dash_variables(os.environ)
    found = app if isinstance(app, App) else load(app)
    logs.bind(service=found.name)
    built = build(found, settings=settings, environment=environment)
    serving = built.settings.serve
    host = host if host is not None else serving.host
    log.info("serving %s on %s:%s%s", found.name, host, serving.port, serving.prefix.rstrip("/"), extra={"host": host, "port": serving.port, "prefix": serving.prefix})
    run(built.asgi, serving, built.lifecycle, host=host)


def dev(reference: str | None, *, repo: Path) -> None:
    """Serve the app `reference` names, as `serve` finds it, on this machine alone, with Dash's dev tools, until
    Ctrl+C; uvicorn starts it again when a file under `repo` changes."""
    logs.setup()
    refuse_dash_variables(os.environ)
    found = load(reference)
    serving = found.settings().serve
    os.environ[DEV_APP] = reference if reference is not None else f"{found.module}:app"
    log.info("serving %s on %s:%s%s, until Ctrl+C", found.name, LOCAL, serving.port, serving.prefix.rstrip("/"), extra={"host": LOCAL, "port": serving.port, "prefix": serving.prefix})
    uvicorn.run(
        "basis.dash.server:dev_app",
        factory=True,
        reload=True,
        reload_dirs=[str(repo)],
        host=LOCAL,
        port=serving.port,
        lifespan="on",
        log_config=None,  # uvicorn's records go through basis-core's handler, as JSON
        access_log=False,  # the request boundary logs each request, with its id
        server_header=False,
    )


def dev_app() -> FastAPI:
    """The app `basis dev` serves, built with Dash's dev tools in the process uvicorn's reloader starts."""
    logs.setup()
    found = load(os.environ[DEV_APP])
    logs.bind(service=found.name)
    return build(found, dev_tools=True).asgi


def refuse_dash_variables(environ: Mapping[str, str]) -> None:
    """Refuse to serve while a variable that changes what Dash does is set: any `DASH_*`, or the FastAPI backend's
    `_DASH_FASTAPI_CONFIG`, which turns the dev tools on."""
    if found := sorted(name for name in environ if name.startswith("DASH_") or name == internals.FASTAPI_CONFIG):
        msg = f"the process sets {', '.join(found)}, which would change what Dash does, and basis gives Dash every setting itself: unset them, and set the app's settings as BASIS_* variables"
        raise SettingsError(msg)
