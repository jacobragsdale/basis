"""A service's declarations, from which `api.build` makes its FastAPI app: its name, its settings, its resources (see
`resources.py`), and its routes, each a method, a path and the handler that answers it. A service never makes the app
itself, so every API has the same middleware, error handling, health endpoints and shutdown, and `basis serve` refuses
an app it did not build.

    service = api.Service(
        name="accruals",
        settings=Settings,
        resources=[WAREHOUSE],
        routes=[
            api.get("/accrued-interest", accrued_interest, deadline_s=1, response=Accrual, errors=[PeriodReversed]),
            api.get("/bonds/{security}", bond, deadline_s=5, response=Bond, errors=[UnknownBond]),
        ],
    )

Every route declares three things, none of which has a default, so a route that leaves one out is refused:

- `deadline_s`, the seconds it has to answer, from when it begins to serve a request: every call it makes through a
  resource or a compute function has what is left of it, or the resource's own deadline where that is shorter, the
  commit of its writes included (see `deadlines.py`);
- `response`, what it answers when it succeeds, which its handler's return annotation must be; None for a route
  that answers 204 with no body;
- `errors`, every `api.Error` its handler may raise. Each appears in the OpenAPI document under its status, and an
  error the route does not declare, or an `HTTPException`, answers 500 as the defect it is. basis adds its own: an
  invalid request's 422 on every route, a deadline's 504 on one that asks for a resource or a compute function, and
  another service's 502 on one that asks for an `api.http`. `Route.answers` holds them all.

A route reads its request strictly (see `strict.py`): no unknown key, and no value coerced, in its body, its query or
its path.

A handler is a FastAPI path operation, so its parameters are FastAPI's: path, query and body parameters by their
types. What basis gives it, its settings, its resources and the request's user, it asks for with `api.given`, which the app
answers once it has started:

    def accrued_interest(start: date, end: date, settings: Annotated[Settings, api.given(Settings)]) -> Accrual: ...
    async def bond(security: str, warehouse: Annotated[api.Reader, api.given(WAREHOUSE)]) -> Bond: ...
    def holdings(user: Annotated[User | None, api.given(User)]) -> Holdings: ...  # None for an anonymous request

and it asks for a compute function the same way (see `compute.py`), which runs in the service's process pool.

What a handler asks for is checked when its route is declared, and when its service is: that it asks for what the
service gives, annotated as what it gets, and from an `async def` where the calls are awaited. A handler asks for its
resources in its own parameters, never through a dependency of its own (`Depends(helper)`), which a route refuses:
the handler's signature then shows everything the route reaches, which the checks, and the records of its requests,
read from it. A helper takes what the handler was given as an argument.

A repo lists its service as an entry point in the `basis.apis` group, `NAME = "module:attribute"`, which is how
`basis serve` finds it."""

import importlib
import inspect
import math
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

from basis.api import strict
from basis.api.compute import Compute, ComputeFunction
from basis.api.errors import Error, NotReadyError, ServiceError
from basis.api.identity import GivenUser, User, annotation_refusal
from basis.api.resources import (
    Cache,
    CacheResource,
    CallFailedError,
    Client,
    Clock,
    ClockResource,
    DatabaseResource,
    DeadlineExceededError,
    HttpResource,
    Ids,
    IdsResource,
    Opened,
    Reader,
    Resource,
    Session,
    Writer,
)
from basis.api.settings import Settings

GROUP = "basis.apis"
STATE = "basis"
"""The key of the app's lifespan state that holds its `Runtime`."""
RESERVED = ("/health", "/docs", "/redoc", "/openapi.json")
"""Paths the app itself answers: its health endpoints and its OpenAPI document and pages."""
NAME = re.compile(r"[a-z][a-z0-9-]{0,62}")
_PARAMETER = re.compile(r"\{[^}]*\}")
_REFERENCE = re.compile(r"[A-Za-z_][\w.]*:[A-Za-z_]\w*")
BASIS_ERRORS: tuple[type[Error], ...] = (DeadlineExceededError, CallFailedError, NotReadyError)
"""The errors basis raises itself, which a route never declares: `Route.answers` adds each where the route can meet it."""

type Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
_RESOURCES = (DatabaseResource, HttpResource, CacheResource, ClockResource, IdsResource)


@dataclass(frozen=True)
class Ask:
    """One of a handler's parameters, which asks basis for something through `api.given`."""

    parameter: str
    annotation: object
    """The parameter's type, without the `Annotated` metadata that holds `api.given`."""
    given: type[Settings | User] | Resource | ComputeFunction[..., object]


@dataclass(frozen=True)
class Route:
    method: Method
    path: str
    handler: FunctionType
    """A named function, sync where it calls blocking code, which FastAPI runs in its threadpool, and async only where
    it awaits. Its name is the route's name."""
    deadline_s: float
    """Seconds the route has to answer a request, which cap every call it makes through a resource or a compute
    function."""
    response: object
    """What a request that succeeds answers, which is the handler's return annotation: a model, a list of them, or
    None for a 204 with no body."""
    errors: Sequence[type[Error]]
    """Each `api.Error` the handler may raise, but basis's own, which `answers` adds."""
    status: int = HTTPStatus.OK
    """The status of a response that succeeds."""
    name: str = field(init=False)
    """The handler's name, which is the route's."""
    asks: tuple[Ask, ...] = field(init=False)
    """What the handler asks basis for."""
    writes: DatabaseResource | None = field(init=False)
    """The database the handler writes, which it asks for as an `api.Writer`: one at most, since a request commits one
    transaction."""
    answers: tuple[type[Error], ...] = field(init=False)
    """Every `api.Error` the route may answer with: its declared `errors`, and each of basis's own that it can meet."""
    reads: strict.Shape = field(init=False)
    """What it reads from a request, strictly."""

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
        if converted := [found for found in _PARAMETER.findall(self.path) if ":" in found]:
            alone = converted[0][1:].partition(":")[0]
            msg = (
                f"route {self.path!r} converts {', '.join(converted)} in its path: name the parameter alone, as {{{alone}}}, and give its type in {self.name}'s parameters, which basis reads strictly"
            )
            raise ServiceError(msg)
        deadline: object = self.deadline_s
        if isinstance(deadline, bool) or not isinstance(deadline, int | float) or not 0 < deadline < math.inf:
            msg = f"{self.name}'s deadline is {deadline!r}: give it deadline_s, the seconds the route has to answer, above 0"
            raise ServiceError(msg)
        _check_response(self)
        object.__setattr__(self, "errors", _declared_errors(self.name, self.errors))
        for path, asked in _asked_within(handler, (), set()):
            msg = (
                f"{self.name}'s dependency {' -> '.join(path)} asks for {asked}: ask for it in {self.name}'s own parameters,"
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
        object.__setattr__(self, "answers", (*self.errors, *_met(asks)))
        object.__setattr__(self, "reads", strict.shape(self.path, handler, self.name))

    @property
    def shape(self) -> tuple[Method, str]:
        """The method and the path with its parameters unnamed: two routes of one shape match the same requests."""
        return self.method, _PARAMETER.sub("{}", self.path)


class _Declare(Protocol):
    def __call__(  # noqa: PLR0913 -- what a route declares, each by keyword
        self, path: str, handler: FunctionType, *, deadline_s: float, response: object, errors: Sequence[type[Error]], status: int = HTTPStatus.OK
    ) -> Route: ...


def _route(method: Method) -> _Declare:
    def declare(path: str, handler: FunctionType, *, deadline_s: float, response: object, errors: Sequence[type[Error]], status: int = HTTPStatus.OK) -> Route:  # noqa: PLR0913 -- by keyword
        return Route(method, path, handler, deadline_s, response, errors, status)

    declare.__name__ = declare.__qualname__ = method.lower()
    declare.__doc__ = (
        f"A route that answers {method} requests to `path` with `handler`: within `deadline_s` seconds, with `response` and `status` when it succeeds, and with one of `errors` when it fails."
    )
    return declare


def _check_response(route: Route) -> None:
    """That the route declares its response as its handler's return annotation, and None exactly when it answers 204."""
    returns = inspect.signature(route.handler, eval_str=True).return_annotation
    declared = route.response
    if returns is inspect.Signature.empty:
        msg = f"{route.name} has no return annotation: annotate it `-> {strict.named(declared)}`, the response its route declares"
        raise ServiceError(msg)
    if returns != declared:
        msg = f"{route.name} returns {strict.named(returns)}, and its route declares response={strict.named(declared)}: declare the one it returns"
        raise ServiceError(msg)
    if (declared is None) != (route.status == HTTPStatus.NO_CONTENT):
        fix = "give it status=HTTPStatus.NO_CONTENT" if declared is None else "declare the response it has, or response=None with no content"
        msg = f"{route.name} answers {route.status} with response={strict.named(declared)}: a route answers no body exactly when its status is 204, so {fix}"
        raise ServiceError(msg)


def _declared_errors(name: str, declared: object) -> tuple[type[Error], ...]:
    if isinstance(declared, str | bytes) or not isinstance(declared, Sequence):
        msg = f"{name}'s errors are {declared!r}: give errors=[...], each api.Error its handler may raise, or errors=[] for none"
        raise ServiceError(msg)
    found: list[type[Error]] = []
    for error in declared:
        if not (isinstance(error, type) and issubclass(error, Error)):
            msg = f"{name} declares {error!r} among its errors: give each api.Error subclass its handler may raise"
            raise ServiceError(msg)
        if error in BASIS_ERRORS:
            msg = f"{name} declares {error.__qualname__}, which basis raises itself and adds to each route that can meet it: remove it from errors=[...]"
            raise ServiceError(msg)
        if vars(error).get("_abstract", False):
            msg = f"{name} declares {error.__qualname__}, a base that is never raised itself: declare each of its subclasses the handler raises"
            raise ServiceError(msg)
        if error in found:
            msg = f"{name} declares {error.__qualname__} twice: declare each error once"
            raise ServiceError(msg)
        found.append(error)
    return tuple(found)


def _met(asks: Sequence[Ask]) -> Iterator[type[Error]]:
    """The errors of basis's own that a route which asks for `asks` can meet."""
    given = [ask.given for ask in asks if not isinstance(ask.given, type)]
    if any(not isinstance(found, ClockResource | IdsResource) for found in given):
        yield DeadlineExceededError
    if any(isinstance(found, HttpResource) for found in given):
        yield CallFailedError


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
    fakes: str | None = None
    """Where its fakes for `basis dev` and `basis call --fake` are made: `module:function`, a function that takes
    nothing and returns a fake for each database and other service it declares, by resource, as `api.build` takes them.
    None for a service that has none yet."""

    def __post_init__(self) -> None:
        if not NAME.fullmatch(self.name):
            msg = f"service name {self.name!r}: a service's name is lower-case letters, digits and hyphens, starting with a letter"
            raise ServiceError(msg)
        if self.fakes is not None and not _REFERENCE.fullmatch(self.fakes):
            msg = f"service {self.name}'s fakes are {self.fakes!r}: give fakes='module:function', the function that makes them, as 'accruals.dev:fakes'"
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
                if isinstance(ask.given, ComputeFunction):
                    continue  # a function, which the service runs and never configures
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
class _GivenCompute:
    declared: ComputeFunction[..., object]

    async def __call__(self, request: Request) -> object:
        return _runtime(request).resources.compute(self.declared)


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


def given(kind: type[Settings | User] | Resource | ComputeFunction[..., object]) -> params.Depends:
    """What basis gives a handler, as the metadata of its parameter's type: `Annotated[Settings, api.given(Settings)]`,
    the service's settings; `Annotated[User | None, api.given(User)]`, the request's user, or None when it is
    anonymous; one of its resources, as `Annotated[api.Reader, api.given(WAREHOUSE)]`; or a compute function, as
    `Annotated[api.Compute[[Bond, Decimal], Decimal], api.given(SOLVE)]`."""
    if isinstance(kind, type) and issubclass(kind, Settings):
        return params.Depends(_Given(kind))
    if isinstance(kind, type) and issubclass(kind, User):
        return params.Depends(GivenUser(kind))
    if isinstance(kind, ComputeFunction):
        return params.Depends(_GivenCompute(kind))
    if isinstance(kind, DatabaseResource):
        return params.Depends(_GivenDatabase(kind), scope="function")
    if isinstance(kind, HttpResource | CacheResource | ClockResource | IdsResource):
        return params.Depends(_GivenResource(kind))
    msg = (
        f"api.given({kind!r}): basis gives a handler its service's settings, a subclass of api.Settings, the request's user,"
        " a subclass of api.User, one of its resources, as api.database(...) declares, or a compute function, as api.compute(...) declares"
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
            elif isinstance(marker, params.Depends) and isinstance(marker.dependency, _GivenCompute):
                yield Ask(parameter.name, base, marker.dependency.declared)


def _asked_within(call: object, path: tuple[str, ...], seen: set[int]) -> Iterator[tuple[tuple[str, ...], str]]:
    """Each resource or compute function, by name, or the user, that a dependency of `call`'s, at any depth, asks for,
    with the dependencies that lead to it: `call`'s own parameters ask for theirs where `path` is empty. The user is
    refused there as a resource is, so that a route's own parameters show it reads the user, and the 401 it may answer."""
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
                    yield (*path, parameter.name), dependency.resource.name
            elif isinstance(dependency, GivenUser):
                if path:
                    yield (*path, parameter.name), "the user"
            elif isinstance(dependency, _GivenCompute):
                if path:
                    yield (*path, parameter.name), dependency.declared.name
            elif not isinstance(dependency, _Given) and callable(dependency):
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
        if isinstance(ask.given, ComputeFunction):
            _check_compute(route, ask, ask.given)
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


_HANDLES = (Reader, Writer, Session, Client, Clock, Ids, Opened)
"""What a handler gets for a resource, which a compute function never takes, as it reaches no resource."""


def _check_compute(route: Route, ask: Ask, declared: ComputeFunction[..., object]) -> None:
    """That `route` asks for the compute function `declared` as an `api.Compute` of its parameters and its result,
    from an `async def`, and that the function takes no resource."""
    for parameter in declared.parameters:
        kind = strict.bare(parameter)
        if kind in _HANDLES or get_origin(kind) is Cache or (isinstance(kind, type) and issubclass(kind, Settings)):
            msg = f"compute function {declared.name} takes {strict.named(kind)}, and a compute function reaches no resource or setting: give it the values it computes from"
            raise ServiceError(msg)
    expected = f"api.Compute[[{', '.join(strict.named(parameter) for parameter in declared.parameters)}], {strict.named(declared.result)}]"
    if (
        get_origin(ask.annotation) is not Compute
        or [strict.bare(found) for found in get_args(ask.annotation)[0]] != [strict.bare(found) for found in declared.parameters]
        or strict.bare(get_args(ask.annotation)[1]) != strict.bare(declared.result)
    ):
        msg = f"{route.name}'s parameter {ask.parameter} asks for the compute function {declared.name}: annotate it Annotated[{expected}, api.given(...)], its parameters and its result"
        raise ServiceError(msg)
    if not inspect.iscoroutinefunction(route.handler):
        msg = f"{route.name} asks for the compute function {declared.name}, whose calls are awaited: declare it `async def {route.name}`"
        raise ServiceError(msg)


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
