"""basis-api: HTTP APIs on FastAPI. A service declares its name, its settings and its routes, and basis builds the
FastAPI app around them, the same in every repo: request ids and JSON logs, OpenTelemetry traces, RFC 9457 errors from
one exception map, health and readiness endpoints, and a graceful shutdown. A service never makes the app itself.

    import basis.api as api


    class Settings(api.Settings):
        max_days: int = 3660


    class PeriodReversed(api.Error):
        status = HTTPStatus.UNPROCESSABLE_CONTENT
        title = "The period ends before it starts"


    def accrued_interest(start: date, end: date, settings: Annotated[Settings, api.given(Settings)]) -> Accrual: ...


    service = api.Service(name="accruals", settings=Settings, routes=[api.get("/accrued-interest", accrued_interest)])

`api.build` and `api.serve` load the app's machinery on first use, so a service's modules, which import this package
for their declarations, import no server and no exporter."""

from typing import TYPE_CHECKING

from basis.api.errors import Error, FieldError, Problem, ServiceError
from basis.api.service import Route, Service, delete, get, given, load, patch, post, put
from basis.api.settings import Serve, Settings, SettingsError, Traces

if TYPE_CHECKING:
    from basis.api.app import App, build
    from basis.api.server import serve

__all__ = [
    "App",
    "Error",
    "FieldError",
    "Problem",
    "Route",
    "Serve",
    "Service",
    "ServiceError",
    "Settings",
    "SettingsError",
    "Traces",
    "build",
    "delete",
    "get",
    "given",
    "load",
    "patch",
    "post",
    "put",
    "serve",
]


def __getattr__(name: str) -> object:
    if name in {"App", "build"}:
        from basis.api import app  # noqa: PLC0415 -- the app's machinery loads on first use, which a service's modules never make

        return getattr(app, name)
    if name == "serve":
        from basis.api import server  # noqa: PLC0415 -- uvicorn loads on first use

        return server.serve
    msg = f"module 'basis.api' has no attribute {name!r}"
    raise AttributeError(msg)
