"""What basis gives a handler, as FastAPI's dependencies: `given`, which a handler's parameter's type carries as its
metadata, and what each answers once the app has started (`Runtime`, `running`): the service's settings, the request's
user, one of its resources, or a compute function. A database's is the request's transaction, which ends once the
handler has returned and FastAPI has validated its response (`GivenDatabase`). `basis.api.service` checks what each
handler asks for when its route is declared."""

from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass

from fastapi import params
from starlette.requests import Request
from starlette.routing import Route as StarletteRoute

from basis.api.compute import ComputeFunction
from basis.api.errors import DeclarationError
from basis.api.identity import GivenUser, User
from basis.api.resources import CacheResource, ClockResource, DatabaseResource, HttpResource, IdsResource, Opened, Resource, Session
from basis.api.settings import Settings

STATE = "basis"
"""The key of the app's lifespan state that holds its `Runtime`."""


@dataclass(frozen=True)
class Runtime:
    """What a started app gives its handlers through `given`: its settings, and the resources its lifespan opened."""

    settings: Settings
    resources: Opened
    writes: Mapping[object, DatabaseResource]
    """The database each handler that writes one writes, by the handler."""
    draining: Callable[[], bool] = lambda: False
    """Whether the app is shutting down, so that each stream of live updates ends, and its UI connects to another
    replica."""


def running(request: Request) -> Runtime:
    """The started app's `Runtime`, which serves `request`."""
    state = request.scope.get("state")
    runtime = state.get(STATE) if isinstance(state, dict) else None
    if not isinstance(runtime, Runtime):
        msg = "the app has not started, so it has nothing to give: run its lifespan, as `basis serve` and `with TestClient(app.asgi)` do"
        raise RuntimeError(msg)  # noqa: TRY004 -- not a caller's wrong type: an app used before it started
    return runtime


@dataclass(frozen=True)
class GivenSettings:
    kind: type[Settings]

    async def __call__(self, request: Request) -> Settings:
        runtime = running(request)
        if not isinstance(runtime.settings, self.kind):
            msg = f"the app's settings are {type(runtime.settings).__qualname__}, not {self.kind.__qualname__}"
            raise TypeError(msg)
        return runtime.settings


@dataclass(frozen=True)
class GivenResource:
    resource: HttpResource | CacheResource[object] | ClockResource | IdsResource

    async def __call__(self, request: Request) -> object:
        return running(request).resources.handle(self.resource)


@dataclass(frozen=True)
class GivenCompute:
    declared: ComputeFunction[..., object]

    async def __call__(self, request: Request) -> object:
        return running(request).resources.compute(self.declared)


@dataclass(frozen=True)
class GivenDatabase:
    """A request's transaction in a database, which ends when the handler has returned and FastAPI has validated its
    response, before the response is sent: FastAPI ends a dependency of scope `function` there, and throws into it
    what the handler or the validation raised."""

    resource: DatabaseResource

    async def __call__(self, request: Request) -> AsyncIterator[Session]:
        runtime = running(request)
        route = request.scope.get("route")
        writes = isinstance(route, StarletteRoute) and runtime.writes.get(route.endpoint) == self.resource
        session = runtime.resources.session(self.resource, writes=writes)
        try:
            yield session
        except GeneratorExit:  # closed without being finished, so nothing may be awaited here
            session.abandon()
            raise
        except BaseException:
            await session.end(commit=False)
            raise
        await session.end(commit=True)


def given(kind: type[Settings | User] | Resource | ComputeFunction[..., object]) -> params.Depends:
    """What basis gives a handler, as the metadata of its parameter's type: `Annotated[Settings, api.given(Settings)]`,
    the service's settings; `Annotated[User | None, api.given(User)]`, the request's user, or None when it is
    anonymous; one of its resources, as `Annotated[api.Reader, api.given(WAREHOUSE)]`; or a compute function, as
    `Annotated[api.Compute[[Bond, Decimal], Decimal], api.given(SOLVE)]`."""
    if isinstance(kind, type) and issubclass(kind, Settings):
        return params.Depends(GivenSettings(kind))
    if isinstance(kind, type) and issubclass(kind, User):
        return params.Depends(GivenUser(kind))
    if isinstance(kind, ComputeFunction):
        return params.Depends(GivenCompute(kind))
    if isinstance(kind, DatabaseResource):
        return params.Depends(GivenDatabase(kind), scope="function")
    if isinstance(kind, HttpResource | CacheResource | ClockResource | IdsResource):
        return params.Depends(GivenResource(kind))
    msg = (
        f"api.given({kind!r}): basis gives a handler its service's settings, a subclass of api.Settings, the request's user,"
        " a subclass of api.User, one of its resources, as api.database(...) declares, or a compute function, as api.compute(...) declares"
    )
    raise DeclarationError(msg)
