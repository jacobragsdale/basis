"""A service's declarations, from which `api.build` makes its FastAPI app: its name, its settings, its resources (see
`resources.py`), and its routes, each a method, a path and the handler that answers it. A service never makes the app
itself, so every API has the same middleware, error handling, health endpoints and shutdown, and `basis serve` refuses
an app it did not build.

    service = api.Service(
        name="accruals",
        settings=Settings,
        resources=[WAREHOUSE],
        routes=[api.get("/accrued-interest", accrued_interest), api.get("/bonds/{security}", bond)],
    )

A handler is a FastAPI path operation, so its parameters are FastAPI's: path, query and body parameters by their
types. What basis gives it, its settings, its resources and the request's user, it asks for with `api.given`, which the app
answers once it has started:

    def accrued_interest(start: date, end: date, settings: Annotated[Settings, api.given(Settings)]) -> Accrual: ...
    async def bond(security: str, warehouse: Annotated[api.Reader, api.given(WAREHOUSE)]) -> Bond: ...
    def holdings(user: Annotated[User | None, api.given(User)]) -> Holdings: ...  # None for an anonymous request

What a handler asks for is checked when its route is declared, and when its service is: that it asks for what the
service gives, annotated as what it gets, and from an `async def` where the calls are awaited. A handler asks for its
resources in its own parameters, never through a dependency of its own (`Depends(helper)`), which a route refuses:
the handler's signature then shows everything the route reaches, which the checks, and the records of its requests,
read from it. A helper takes what the handler was given as an argument.

A repo lists its service as an entry point in the `basis.apis` group, `NAME = "module:attribute"`, which is how
`basis serve` finds it."""

import importlib
import inspect
import re
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from importlib.metadata import EntryPoint, entry_points
from types import FunctionType
from typing import Annotated, Literal, Protocol, assert_never, get_args, get_origin

from fastapi import params
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Route as StarletteRoute

from basis.api.errors import ServiceError
from basis.api.identity import GivenUser, User, annotation_refusal
from basis.api.resources import Cache, CacheResource, Client, Clock, ClockResource, DatabaseResource, HttpResource, Ids, IdsResource, Opened, Reader, Resource, Session, Writer
from basis.api.settings import Settings

GROUP = "basis.apis"
STATE = "basis"
"""The key of the app's lifespan state that holds its `Runtime`."""
RESERVED = ("/health", "/docs", "/redoc", "/openapi.json")
"""Paths the app itself answers: its health endpoints and its OpenAPI document and pages."""
NAME = re.compile(r"[a-z][a-z0-9-]{0,62}")
_PARAMETER = re.compile(r"\{[^}]*\}")

type Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
_RESOURCES = (DatabaseResource, HttpResource, CacheResource, ClockResource, IdsResource)


@dataclass(frozen=True)
class Ask:
    """One of a handler's parameters, which asks basis for something through `api.given`."""

    parameter: str
    annotation: object
    """The parameter's type, without the `Annotated` metadata that holds `api.given`."""
    given: type[Settings | User] | Resource


@dataclass(frozen=True)
class Route:
    method: Method
    path: str
    handler: FunctionType
    """A named function, sync where it calls blocking code, which FastAPI runs in its threadpool, and async only where
    it awaits. Its name is the route's name."""
    status: int = HTTPStatus.OK
    """The status of a response that succeeds."""
    name: str = field(init=False)
    """The handler's name, which is the route's."""
    asks: tuple[Ask, ...] = field(init=False)
    """What the handler asks basis for."""
    writes: DatabaseResource | None = field(init=False)
    """The database the handler writes, which it asks for as an `api.Writer`: one at most, since a request commits one
    transaction."""

    def __post_init__(self) -> None:
        if not self.path.startswith("/"):
            msg = f"route {self.path!r} is not a path: start it with /, as /accrued-interest"
            raise ServiceError(msg)
        if any(self.path == reserved or self.path.startswith(f"{reserved}/") for reserved in RESERVED):
            msg = f"route {self.path!r} is one the app answers itself ({', '.join(RESERVED)}): give the route another path"
            raise ServiceError(msg)
        handler = self.handler
        if not inspect.isfunction(handler) or handler.__name__ == "<lambda>":
            msg = f"route {self.path!r}'s handler is a {type(handler).__name__}, not a named function: declare it with def, whose name is the route's"
            raise ServiceError(msg)
        object.__setattr__(self, "name", handler.__name__)  # frozen: set once, from the handler
        if self.status not in range(200, 300):
            msg = f"route {self.path!r} succeeds with status {self.status}: give a 2xx, and raise an api.Error for each way it fails"
            raise ServiceError(msg)
        for path, resource in _asked_within(handler, (), set()):
            msg = (
                f"{self.name}'s dependency {' -> '.join(path)} asks for {resource.name}: ask for it in {self.name}'s own parameters,"
                " and pass what it gives to the dependency, so the route shows everything it reaches"
            )
            raise ServiceError(msg)
        asks = tuple(_asks_for(handler))
        for ask in asks:
            if isinstance(ask.given, type) and issubclass(ask.given, User) and (fix := annotation_refusal(ask.given, ask.annotation)) is not None:
                msg = f"{self.name}'s parameter {ask.parameter} asks for the user: {fix}"
                raise ServiceError(msg)
        object.__setattr__(self, "asks", asks)
        object.__setattr__(self, "writes", _written(self, asks))

    @property
    def shape(self) -> tuple[Method, str]:
        """The method and the path with its parameters unnamed: two routes of one shape match the same requests."""
        return self.method, _PARAMETER.sub("{}", self.path)


class _Declare(Protocol):
    def __call__(self, path: str, handler: FunctionType, *, status: int = HTTPStatus.OK) -> Route: ...


def _route(method: Method) -> _Declare:
    def declare(path: str, handler: FunctionType, *, status: int = HTTPStatus.OK) -> Route:
        return Route(method, path, handler, status)

    declare.__name__ = declare.__qualname__ = method.lower()
    declare.__doc__ = f"A route that answers {method} requests to `path` with `handler`, and `status` when it succeeds."
    return declare


get, post, put, patch, delete = _route("GET"), _route("POST"), _route("PUT"), _route("PATCH"), _route("DELETE")


@dataclass(frozen=True)
class Service:
    name: str
    """Lower-case letters, digits and hyphens: the app's title, and the name its spans carry."""
    settings: type[Settings]
    """`api.Settings`, or the service's subclass of it, read once when the app is built."""
    routes: Sequence[Route]
    resources: Sequence[Resource] = ()
    """Everything its handlers reach outside the process, each declared with `api.database`, `api.http`,
    `api.cache`, `api.clock` or `api.ids`."""
    user: type[User] | None = None
    """`api.User`, or the service's subclass of it with the claims it needs, for a service whose requests' bearer
    tokens are verified, which the settings' `identity` configures: see `basis.api.identity`. None for a service
    that verifies no tokens, whose requests are all anonymous."""

    def __post_init__(self) -> None:
        if not NAME.fullmatch(self.name):
            msg = f"service name {self.name!r}: a service's name is lower-case letters, digits and hyphens, starting with a letter"
            raise ServiceError(msg)
        if not (isinstance(self.settings, type) and issubclass(self.settings, Settings)):
            msg = f"service {self.name}'s settings are {self.settings!r}, not a subclass of api.Settings: declare `class Settings(api.Settings)`, or pass api.Settings itself"
            raise ServiceError(msg)
        routes = tuple(self.routes)
        object.__setattr__(self, "routes", routes)  # frozen: a list given here can no longer change the service
        resources = tuple(self.resources)
        object.__setattr__(self, "resources", resources)
        _check_resources(self.name, resources)
        if not routes:
            msg = f"service {self.name} declares no routes: give it routes=[api.get(path, handler), ...]"
            raise ServiceError(msg)
        for route in routes:
            if not isinstance(route, Route):
                msg = f"service {self.name} has a {type(route).__name__} among its routes: declare each with api.get(path, handler) or api.post, api.put, api.patch, api.delete"
                raise ServiceError(msg)
            for ask in route.asks:
                if isinstance(ask.given, type) and issubclass(ask.given, Settings) and not issubclass(self.settings, ask.given):
                    msg = f"{route.name} asks for {ask.given.__qualname__}, which service {self.name} does not give: its settings are {self.settings.__qualname__}"
                    raise ServiceError(msg)
                if not isinstance(ask.given, type) and ask.given not in resources:
                    msg = f"{route.name} asks for {ask.given.name}, which service {self.name} does not declare: add it to the service's resources=[...]"
                    raise ServiceError(msg)
        _check_user(self.name, self.user, routes)
        seen: dict[tuple[Method, str], Route] = {}
        for route in routes:
            if (first := seen.get(route.shape)) is not None:
                same = route.path if route.path == first.path else f"{first.path} and {route.path}, which match the same paths,"
                msg = f"service {self.name} declares {route.method} {same} twice: give each method and path one handler"
                raise ServiceError(msg)
            seen[route.shape] = route


@dataclass(frozen=True)
class Runtime:
    """What a started app gives its handlers through `given`: its settings, and the resources its lifespan opened."""

    settings: Settings
    resources: Opened
    writes: Mapping[object, DatabaseResource]
    """The database each handler that writes one writes, by the handler."""


def _runtime(request: Request) -> Runtime:
    state = request.scope.get("state")
    runtime = state.get(STATE) if isinstance(state, dict) else None
    if not isinstance(runtime, Runtime):
        msg = "the app has not started, so it has nothing to give: run its lifespan, as `basis serve` and `with TestClient(app.asgi)` do"
        raise RuntimeError(msg)  # noqa: TRY004 -- not a caller's wrong type: an app used before it started
    return runtime


@dataclass(frozen=True)
class _Given:
    kind: type[Settings]

    async def __call__(self, request: Request) -> Settings:
        runtime = _runtime(request)
        if not isinstance(runtime.settings, self.kind):
            msg = f"the app's settings are {type(runtime.settings).__qualname__}, not {self.kind.__qualname__}"
            raise TypeError(msg)
        return runtime.settings


@dataclass(frozen=True)
class _GivenResource:
    resource: HttpResource | CacheResource[object] | ClockResource | IdsResource

    async def __call__(self, request: Request) -> object:
        return _runtime(request).resources.handle(self.resource)


@dataclass(frozen=True)
class _GivenDatabase:
    """A request's transaction in a database, which ends when the handler has returned and FastAPI has validated its
    response, before the response is sent: FastAPI ends a dependency of scope `function` there, and throws into it
    what the handler or the validation raised."""

    resource: DatabaseResource

    async def __call__(self, request: Request) -> AsyncIterator[Session]:
        runtime = _runtime(request)
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


def given(kind: type[Settings | User] | Resource) -> params.Depends:
    """What basis gives a handler, as the metadata of its parameter's type: `Annotated[Settings, api.given(Settings)]`,
    the service's settings; `Annotated[User | None, api.given(User)]`, the request's user, or None when it is
    anonymous; or one of its resources, as `Annotated[api.Reader, api.given(WAREHOUSE)]`."""
    if isinstance(kind, type) and issubclass(kind, Settings):
        return params.Depends(_Given(kind))
    if isinstance(kind, type) and issubclass(kind, User):
        return params.Depends(GivenUser(kind))
    if isinstance(kind, DatabaseResource):
        return params.Depends(_GivenDatabase(kind), scope="function")
    if isinstance(kind, HttpResource | CacheResource | ClockResource | IdsResource):
        return params.Depends(_GivenResource(kind))
    msg = (
        f"api.given({kind!r}): basis gives a handler its service's settings, a subclass of api.Settings,"
        " the request's user, a subclass of api.User, or one of its resources, as api.database(...) declares"
    )
    raise ServiceError(msg)


def _asks_for(handler: FunctionType) -> Iterator[Ask]:
    """What each of `handler`'s parameters asks basis for, through `given`, its annotations evaluated even where they
    are strings, as FastAPI evaluates them."""
    for parameter in inspect.signature(handler, eval_str=True).parameters.values():
        annotation: object = parameter.annotation
        base, metadata = (get_args(annotation)[0], get_args(annotation)[1:]) if get_origin(annotation) is Annotated else (annotation, ())
        for marker in (*metadata, parameter.default):
            if isinstance(marker, params.Depends) and isinstance(marker.dependency, _Given | GivenUser):
                yield Ask(parameter.name, base, marker.dependency.kind)
            elif isinstance(marker, params.Depends) and isinstance(marker.dependency, (_GivenDatabase, _GivenResource)):
                yield Ask(parameter.name, base, marker.dependency.resource)


def _asked_within(call: object, path: tuple[str, ...], seen: set[int]) -> Iterator[tuple[tuple[str, ...], Resource]]:
    """Each resource that a dependency of `call`'s, at any depth, asks for, with the dependencies that lead to it:
    `call`'s own parameters ask for theirs where `path` is empty."""
    if id(call) in seen:
        return
    seen.add(id(call))
    try:
        signature = inspect.signature(call, eval_str=True)  # ty: ignore[invalid-argument-type] -- FastAPI calls any callable it is given
    except TypeError, ValueError:  # a builtin, whose parameters FastAPI does not read either
        return
    for parameter in signature.parameters.values():
        annotation: object = parameter.annotation
        base, metadata = (get_args(annotation)[0], get_args(annotation)[1:]) if get_origin(annotation) is Annotated else (annotation, ())
        for marker in (*metadata, parameter.default):
            if not isinstance(marker, params.Depends):
                continue
            dependency = marker.dependency if marker.dependency is not None else base  # `Depends()` depends on the type
            if isinstance(dependency, _GivenDatabase | _GivenResource):
                if path:
                    yield (*path, parameter.name), dependency.resource
            elif not isinstance(dependency, _Given | GivenUser) and callable(dependency):
                yield from _asked_within(dependency, (*path, getattr(dependency, "__name__", type(dependency).__name__)), seen)


def _written(route: Route, asks: Sequence[Ask]) -> DatabaseResource | None:
    """The database `route` writes, after checking each resource its handler asks for: once each, annotated as what
    the handler gets, by an `async def` where its calls are awaited, and a database written by a route that is not a GET,
    one at most."""
    seen: set[str] = set()
    written: list[DatabaseResource] = []
    for ask in asks:
        if isinstance(ask.given, type):
            continue
        resource = ask.given
        if resource.name in seen:
            msg = f"{route.name} asks for {resource.name} twice: ask once, and pass what it gives to whatever else needs it"
            raise ServiceError(msg)
        seen.add(resource.name)
        if not _annotated_as_given(resource, ask.annotation):
            msg = f"{route.name}'s parameter {ask.parameter} asks for {resource.name}, {_annotate(resource)}"
            raise ServiceError(msg)
        if not isinstance(resource, ClockResource | IdsResource) and not inspect.iscoroutinefunction(route.handler):
            msg = f"{route.name} asks for {resource.name}, whose calls are awaited: declare it `async def {route.name}`"
            raise ServiceError(msg)
        if ask.annotation is Writer and isinstance(resource, DatabaseResource):
            written.append(resource)
    if written and route.method == "GET":
        msg = f"{route.name} writes {written[0].name}, and a GET only reads: declare the route with api.post, api.put, api.patch or api.delete, or ask for an api.Reader"
        raise ServiceError(msg)
    if len(written) > 1:
        msg = f"{route.name} writes {' and '.join(found.name for found in written)}, and a request commits one transaction: write one database per route"
        raise ServiceError(msg)
    return written[0] if written else None


def _annotated_as_given(resource: Resource, annotation: object) -> bool:
    match resource:
        case DatabaseResource():
            return annotation is Reader or annotation is Writer
        case HttpResource():
            return annotation is Client
        case CacheResource():
            return get_origin(annotation) is Cache and get_args(annotation) == (resource.value,)
        case ClockResource():
            return annotation is Clock
        case IdsResource():
            return annotation is Ids
        case unreachable:
            assert_never(unreachable)


def _annotate(resource: Resource) -> str:
    """How to annotate a parameter that asks for `resource`."""
    match resource:
        case DatabaseResource():
            return f"a database: annotate it Annotated[api.Reader, api.given(...)], or api.Writer in a route that writes {resource.name}"
        case HttpResource():
            return "another service: annotate it Annotated[api.Client, api.given(...)]"
        case CacheResource():
            return f"a cache of {resource.value.__qualname__}: annotate it Annotated[api.Cache[{resource.value.__qualname__}], api.given(...)]"
        case ClockResource():
            return "a clock: annotate it Annotated[api.Clock, api.given(...)]"
        case IdsResource():
            return "new ids: annotate it Annotated[api.Ids, api.given(...)]"
        case unreachable:
            assert_never(unreachable)


def _check_user(service: str, user: object, routes: Sequence[Route]) -> None:
    """That the service's user is an `api.User`, and that each route that asks for a user asks for that one, or a base
    of it."""
    if user is not None and not (isinstance(user, type) and issubclass(user, User)):
        msg = f"service {service}'s user is {user!r}, not a subclass of api.User: declare `class User(api.User)` with the claims it needs, or pass api.User itself"
        raise ServiceError(msg)
    for route in routes:
        for ask in route.asks:
            if isinstance(ask.given, type) and issubclass(ask.given, User) and (user is None or not issubclass(user, ask.given)):
                given = f"its user is {user.__qualname__}" if user is not None else "it declares no user=..."
                msg = f"{route.name} asks for {ask.given.__qualname__}, which service {service} does not give: {given}"
                raise ServiceError(msg)


def _check_resources(service: str, resources: Sequence[object]) -> None:
    names: set[str] = set()
    for resource in resources:
        if not isinstance(resource, _RESOURCES):
            msg = f"service {service} has a {type(resource).__name__} among its resources: declare each with api.database, api.http, api.cache, api.clock or api.ids"
            raise ServiceError(msg)
        if resource.name in names:
            msg = f"service {service} declares {resource.name} twice: give each resource a name of its own"
            raise ServiceError(msg)
        names.add(resource.name)


def load(reference: str | None = None, *, listed: Iterable[EntryPoint] | None = None) -> Service:
    """The service `reference` names: an entry point in the `basis.apis` group, a module whose `service` it is, or
    `module:attribute`. With none, the one service listed. `listed` is the entry points to choose from, by default the
    installed packages' `basis.apis`."""
    entries = {entry.name: entry for entry in (listed if listed is not None else entry_points(group=GROUP))}
    if reference is None:
        if len(entries) != 1:
            listing = f'[project.entry-points."{GROUP}"]'
            msg = f"the installed packages list {sorted(entries) or 'no services'} in the {GROUP} entry points: name the service to serve, or list the repo's one under {listing} in its pyproject.toml"
            raise ServiceError(msg)
        reference = next(iter(entries))
    entry = entries.get(reference) or EntryPoint(name=reference, value=reference, group=GROUP)
    try:
        module = importlib.import_module(entry.module)
    except ModuleNotFoundError as error:
        if error.name is not None and (entry.module == error.name or entry.module.startswith(f"{error.name}.")):
            reason = f"no service named {reference!r}: the installed packages list {sorted(entries) or 'none'} in the {GROUP} entry points, and no module has that path"
            msg = f'{reason}: give its module path, as basis.api.example.service, or list it under [project.entry-points."{GROUP}"]'
            raise ServiceError(msg) from error
        raise  # the module exists, and something it imports does not
    attribute = entry.attr or "service"
    found: object = module
    for part in attribute.split("."):
        found = getattr(found, part, None)
    if isinstance(found, Service):
        return found
    where = f"{entry.module}:{attribute}"
    if isinstance(found, Starlette):
        msg = f"{where} is an app the service made itself, which basis does not serve: declare `service = api.Service(name=..., settings=..., routes=[...])`, and basis builds its app"
        raise ServiceError(msg)
    msg = f"{where} is not an api.Service: declare `service = api.Service(name=..., settings=..., routes=[...])` there"
    raise ServiceError(msg)
