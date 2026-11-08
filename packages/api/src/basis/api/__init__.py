"""basis-api: HTTP APIs on FastAPI. A service declares its name, its settings and its routes, and basis builds the
FastAPI app around them, the same in every repo: request ids and JSON logs, OpenTelemetry traces, RFC 9457 errors from
one exception map, health and readiness endpoints, and a graceful shutdown. A service never makes the app itself.

    import basis.api as api


    class Settings(api.Settings):
        max_days: int = 3660


    class PeriodReversed(api.Error):
        status = HTTPStatus.UNPROCESSABLE_CONTENT
        title = "The period ends before it starts"


    def accrued_interest(period: Annotated[Period, Query()], settings: Annotated[Settings, api.given(Settings)]) -> Accrual: ...


    WAREHOUSE = api.database("warehouse", deadline_s=5, max_concurrent=8)


    async def bond(security: str, warehouse: Annotated[api.Reader, api.given(WAREHOUSE)]) -> Bond: ...


    service = api.Service(
        name="accruals",
        settings=Settings,
        resources=[WAREHOUSE],
        routes=[
            api.get("/accrued-interest", accrued_interest, deadline_s=1, response=Accrual, errors=[PeriodReversed]),
            api.get("/bonds/{security}", bond, deadline_s=5, response=Bond, errors=[UnknownBond]),
        ],
    )

Each route declares its deadline, its response and its errors (`service.py`), and reads its request strictly
(`strict.py`). A resource is anything a handler reaches outside the process: a database, another HTTP service, a cache,
the clock and new ids. `resources.py` says how each is declared, and what a handler gets for it; `basis.api.fakes`
holds their fakes, each of which fails on demand. A resource that fails raises one of basis-core's kinds of
`api.ServiceError`, `api.Timeout`, `api.Unavailable`, `api.NotFound`, `api.Conflict`, `api.Rejected`, `api.Denied`,
`api.BadReply` or `api.Failed`, whether it is real or a fake: code that has something to say about a kind catches it,
and any other answers its problem, so a handler needs no `try` (`errors.py`). A call that changes another service's state goes after the commit, through the outbox the written
database declares with `api.outbox` (`outboxes.py`). CPU-bound work a handler hands to the service's process pool is a compute function (`compute.py`).
A service that declares `user=User` has each request's bearer token verified, and its handlers ask for `User | None`:
`identity.py` says how.

`api.build` and `api.serve` load the app's machinery on first use, so a service's modules, which import this package
for their declarations, import no server, no exporter, no database driver and no HTTP client."""

from typing import TYPE_CHECKING

from basis.api import format  # noqa: A004 -- `api.format.money("USD")`, a namespace of display formats
from basis.api.compute import Compute, ComputeFunction, compute
from basis.api.errors import DeclarationError, DomainError, Error, FieldError, ParameterError, PointerError, Problem, ValidationProblem, error, pointer
from basis.api.identity import SignInRequiredError, User
from basis.api.resources import Bind, Cache, Client, Clock, Ids, Outbox, Reader, Resource, Writer, cache, clock, database, http, ids, outbox
from basis.api.service import Route, Service, delete, get, given, load, patch, post, put
from basis.api.settings import Serve, Settings, SettingsError
from basis.api.tables import Table
from basis.core.errors import BadReply, Conflict, Denied, Failed, NotFound, Rejected, ServiceError, Timeout, Unavailable
from basis.core.settings import Traces

if TYPE_CHECKING:
    from basis.api.app import App, build
    from basis.api.call import answer as call_route
    from basis.api.server import serve, serve_on_fakes

__all__ = [
    "App",
    "BadReply",
    "Bind",
    "Cache",
    "Client",
    "Clock",
    "Compute",
    "ComputeFunction",
    "Conflict",
    "DeclarationError",
    "Denied",
    "DomainError",
    "Error",
    "Failed",
    "FieldError",
    "Ids",
    "NotFound",
    "Outbox",
    "ParameterError",
    "PointerError",
    "Problem",
    "Reader",
    "Rejected",
    "Resource",
    "Route",
    "Serve",
    "Service",
    "ServiceError",
    "Settings",
    "SettingsError",
    "SignInRequiredError",
    "Table",
    "Timeout",
    "Traces",
    "Unavailable",
    "User",
    "ValidationProblem",
    "Writer",
    "build",
    "cache",
    "call_route",
    "clock",
    "compute",
    "database",
    "delete",
    "error",
    "format",
    "get",
    "given",
    "http",
    "ids",
    "load",
    "outbox",
    "patch",
    "pointer",
    "post",
    "put",
    "serve",
    "serve_on_fakes",
]


def __getattr__(name: str) -> object:
    if name in {"App", "build"}:
        from basis.api import app  # noqa: PLC0415 -- the app's machinery loads on first use, which a service's modules never make

        return getattr(app, name)
    if name in {"serve", "serve_on_fakes"}:
        from basis.api import server  # noqa: PLC0415 -- uvicorn loads on first use

        return getattr(server, name)
    if name == "call_route":
        from basis.api.call import answer  # noqa: PLC0415 -- the app loads on first use

        return answer
    msg = f"module 'basis.api' has no attribute {name!r}"
    raise AttributeError(msg)
