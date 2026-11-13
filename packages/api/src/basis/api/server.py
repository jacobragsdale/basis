"""`basis serve` and `basis dev`: run a service's app with uvicorn, logging JSON lines through basis-core, until it is
told to stop. `basis dev` runs it on the service's declared fakes (`dev.py`), and only for this machine, on 127.0.0.1.

The server is basis-core's (`basis.core.serving.server`), whose docstring gives its three-step shutdown: SIGTERM
drains first, for `serve.drain_s` seconds, Ctrl+C skips the drain, and a second signal stops at once."""

import ipaddress
import logging

from basis.api.app import build
from basis.api.fakes import Fakes
from basis.api.identity import Development
from basis.api.loading import load
from basis.api.service import Service
from basis.api.settings import Settings
from basis.core import logs
from basis.core.serving import LOCAL
from basis.core.serving.server import run
from basis.core.settings import SettingsError, variable

log = logging.getLogger(__name__)

__all__ = ["LOCAL", "logged", "serve", "serve_on_fakes"]


def serve(service: Service | str | None = None, *, settings: Settings | None = None, fakes: Fakes | None = None, host: str | None = None, environment: str | None = None) -> None:
    """Serve `service`, or the service a reference names (see `api.load`), or the one the installed packages list,
    with `settings`, or its settings read from the environment, on `fakes` where given, and on `host`, or the one the
    settings name; `environment` names the environment the settings are of, which its spans carry."""
    logs.setup()
    found = service if isinstance(service, Service) else load(service)
    logged(found)
    app = build(found, settings=settings, fakes=fakes, environment=environment)
    serving = app.settings.serve
    host = host if host is not None else serving.host
    _refuse_development(app.settings, host)
    on = " on its fakes" if fakes is not None else ""
    log.info("serving %s on %s:%s%s", found.name, host, serving.port, on, extra={"host": host, "port": serving.port, "fakes": fakes is not None})
    run(app.asgi, serving, app.lifecycle, host=host)


def _refuse_development(settings: Settings, host: str) -> None:
    """Refuse to serve on `host` with the development key, whose tokens anyone can sign, unless only this machine
    reaches it: whatever else the settings say, a test's own among them."""
    identity = settings.identity
    if identity is None or not isinstance(identity.keys, Development) or _loopback(host):
        return
    kind = variable(("identity", "keys", "kind"))
    msg = (
        f"{kind} is development, whose key anyone can sign tokens with, and the API would serve on {host}, which other machines reach:"
        f" serve on {LOCAL}, as basis dev does, with {variable(('serve', 'host'))}={LOCAL}, or verify with the sign-in system's keys, setting {kind} to jwks or public_key"
    )
    raise SettingsError(msg)


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:  # a name, which may resolve to any address
        return False


def serve_on_fakes(service: Service | str | None = None, *, settings: Settings | None = None) -> None:
    """`api.serve_on_fakes`: serve `service` on its fakes, on this machine alone, as `basis dev` does: with `settings`, or its settings read
    from the environment, so a caller in Python, such as a platform's own command, gives them in code."""
    from basis.api import dev as fakes_of  # noqa: PLC0415 -- the fakes load when it runs

    found = service if isinstance(service, Service) else load(service)
    serve(found, settings=fakes_of.settings(found, settings), fakes=fakes_of.fakes(found), host=LOCAL)


def logged(service: Service) -> None:
    """Log as `service`: its own loggers at the configured level, as basis's are, and its name on every record."""
    for package in {route.handler.__module__.partition(".")[0] for route in service.routes}:
        logs.include(package)
    logs.bind(service=service.name)
