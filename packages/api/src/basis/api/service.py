"""A service's declarations, from which `api.build` makes its FastAPI app: its name, its settings, and its routes, each a
method, a path and the handler that answers it. A service never makes the app itself, so every API has the same
middleware, error handling, health endpoints and shutdown, and `basis serve` refuses an app it did not build.

    service = api.Service(
        name="accruals",
        settings=Settings,
        routes=[api.get("/accrued-interest", accrued_interest)],
    )

A handler is a FastAPI path operation, so its parameters are FastAPI's: path, query and body parameters by their
types. What basis gives it, it asks for with `api.given`, which the app answers once it has started:

    def accrued_interest(start: date, end: date, settings: Annotated[Settings, api.given(Settings)]) -> Accrual: ...

A repo lists its service as an entry point in the `basis.apis` group, `NAME = "module:attribute"`, which is how
`basis serve` finds it."""

import importlib
import inspect
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from importlib.metadata import entry_points
from typing import Annotated, Literal, get_args, get_origin

from fastapi import params
from starlette.applications import Starlette
from starlette.requests import Request

from basis.api.settings import Settings

GROUP = "basis.apis"
STATE = "basis"
"""The key of the app's lifespan state that holds its `Runtime`."""
RESERVED = ("/health", "/docs", "/redoc", "/openapi.json")
"""Paths the app itself answers: its health endpoints and its OpenAPI document and pages."""
NAME = re.compile(r"[a-z][a-z0-9-]{0,62}")

type Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
type Handler = Callable[..., object]


class ServiceError(TypeError):
    """A service that does not declare what basis needs; the message says what to declare."""


@dataclass(frozen=True)
class Route:
    method: Method
    path: str
    handler: Handler
    """A named function, sync where it calls blocking code, which FastAPI runs in its threadpool, and async only where
    it awaits. Its name is the route's name."""
    status: int = HTTPStatus.OK
    """The status of a response that succeeds."""

    def __post_init__(self) -> None:
        if not self.path.startswith("/"):
            msg = f"route {self.path!r} is not a path: start it with /, as /accrued-interest"
            raise ServiceError(msg)
        if any(self.path == reserved or self.path.startswith(f"{reserved}/") for reserved in RESERVED):
            msg = f"route {self.path!r} is one the app answers itself ({', '.join(RESERVED)}): give the route another path"
            raise ServiceError(msg)
        if not inspect.isfunction(self.handler) or self.handler.__name__ == "<lambda>":
            msg = f"route {self.path!r}'s handler is a {type(self.handler).__name__}, not a named function: declare it with def, whose name is the route's"
            raise ServiceError(msg)
        if self.status not in range(200, 300):
            msg = f"route {self.path!r} succeeds with status {self.status}: give a 2xx, and raise an api.Error for each way it fails"
            raise ServiceError(msg)

    @property
    def name(self) -> str:
        return str(getattr(self.handler, "__name__", ""))


def get(path: str, handler: Handler, *, status: int = HTTPStatus.OK) -> Route:
    return Route("GET", path, handler, status)


def post(path: str, handler: Handler, *, status: int = HTTPStatus.OK) -> Route:
    return Route("POST", path, handler, status)


def put(path: str, handler: Handler, *, status: int = HTTPStatus.OK) -> Route:
    return Route("PUT", path, handler, status)


def patch(path: str, handler: Handler, *, status: int = HTTPStatus.OK) -> Route:
    return Route("PATCH", path, handler, status)


def delete(path: str, handler: Handler, *, status: int = HTTPStatus.OK) -> Route:
    return Route("DELETE", path, handler, status)


@dataclass(frozen=True)
class Service:
    name: str
    """Lower-case letters, digits and hyphens: the app's title, and the name its spans carry."""
    settings: type[Settings]
    """`api.Settings`, or the service's subclass of it, read once when the app is built."""
    routes: Sequence[Route]

    def __post_init__(self) -> None:
        if not NAME.fullmatch(self.name):
            msg = f"service name {self.name!r}: a service's name is lower-case letters, digits and hyphens, starting with a letter"
            raise ServiceError(msg)
        if not (isinstance(self.settings, type) and issubclass(self.settings, Settings)):
            msg = f"service {self.name}'s settings are {self.settings!r}, not a subclass of api.Settings: declare `class Settings(api.Settings)`, or pass api.Settings itself"
            raise ServiceError(msg)
        routes = tuple(self.routes)
        object.__setattr__(self, "routes", routes)  # frozen: a list given here can no longer change the service
        if not routes:
            msg = f"service {self.name} declares no routes: give it routes=[api.get(path, handler), ...]"
            raise ServiceError(msg)
        for route in routes:
            if not isinstance(route, Route):
                msg = f"service {self.name} has a {type(route).__name__} among its routes: declare each with api.get(path, handler) or api.post, api.put, api.patch, api.delete"
                raise ServiceError(msg)
            for kind in _asks_for(route.handler):
                if not issubclass(self.settings, kind):
                    msg = f"{route.name} asks for {kind.__qualname__}, which service {self.name} does not give: its settings are {self.settings.__qualname__}"
                    raise ServiceError(msg)
        seen: set[tuple[str, str]] = set()
        for route in routes:
            if (route.method, route.path) in seen:
                msg = f"service {self.name} declares {route.method} {route.path} twice: give each method and path one handler"
                raise ServiceError(msg)
            seen.add((route.method, route.path))


@dataclass(frozen=True)
class Runtime:
    """What a started app gives its handlers through `given`: its settings."""

    settings: Settings


@dataclass(frozen=True)
class _Given:
    kind: type[Settings]

    async def __call__(self, request: Request) -> Settings:
        state = request.scope.get("state")
        runtime = state.get(STATE) if isinstance(state, dict) else None
        if not isinstance(runtime, Runtime):
            msg = "the app has not started, so it has nothing to give: run its lifespan, as `basis serve` and `with TestClient(app.asgi)` do"
            raise RuntimeError(msg)  # noqa: TRY004 -- not a caller's wrong type: an app used before it started
        if not isinstance(runtime.settings, self.kind):
            msg = f"the app's settings are {type(runtime.settings).__qualname__}, not {self.kind.__qualname__}"
            raise TypeError(msg)
        return runtime.settings


def given(kind: type[Settings]) -> params.Depends:
    """What basis gives a handler, as the metadata of its parameter's type: `Annotated[Settings, api.given(Settings)]`,
    the service's settings."""
    if not (isinstance(kind, type) and issubclass(kind, Settings)):
        msg = f"api.given({kind!r}): basis gives a handler its service's settings, a subclass of api.Settings"
        raise ServiceError(msg)
    return params.Depends(_Given(kind))


def _asks_for(handler: Handler) -> Iterator[type[Settings]]:
    """What each of `handler`'s parameters asks basis for, through `given`."""
    for parameter in inspect.signature(handler).parameters.values():
        metadata = get_args(parameter.annotation)[1:] if get_origin(parameter.annotation) is Annotated else ()
        for marker in (*metadata, parameter.default):
            if isinstance(marker, params.Depends) and isinstance(marker.dependency, _Given):
                yield marker.dependency.kind


def load(reference: str | None = None) -> Service:
    """The service `reference` names: an entry point in the `basis.apis` group, a module whose `service` it is, or
    `module:attribute`. With none, the one service the installed packages list."""
    listed = {entry.name: entry.value for entry in entry_points(group=GROUP)}
    if reference is None:
        if len(listed) != 1:
            listing = f'[project.entry-points."{GROUP}"]'
            msg = f"the installed packages list {sorted(listed) or 'no services'} in the {GROUP} entry points: name the service to serve, or list the repo's one under {listing} in its pyproject.toml"
            raise ServiceError(msg)
        reference = next(iter(listed))
    target = listed.get(reference, reference)
    module_name, _, attribute = target.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name is not None and module_name.startswith(error.name):
            msg = f"no service named {reference!r}: the installed packages list {sorted(listed) or 'none'}, and it is not a module path"
            raise ServiceError(msg) from error
        raise
    found = getattr(module, attribute or "service", None)
    if isinstance(found, Service):
        return found
    where = f"{module_name}.{attribute or 'service'}"
    if isinstance(found, Starlette):
        msg = f"{where} is an app the service made itself, which basis does not serve: declare `service = api.Service(name=..., settings=..., routes=[...])`, and basis builds its app"
        raise ServiceError(msg)
    msg = f"{where} is not an api.Service: declare `service = api.Service(name=..., settings=..., routes=[...])` there"
    raise ServiceError(msg)
