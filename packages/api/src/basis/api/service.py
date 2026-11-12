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
- `errors`, every `api.Error` its handler may raise, and the meaning `api.error` gives each domain exception it may
  raise (see `errors.py`). Each appears in the OpenAPI document under its status, and an
  error the route does not declare, or an `HTTPException`, answers 500 as the defect it is. basis adds its own: an
  invalid request's 422 on every route, and each kind of `api.ServiceError` that what the route asks for can raise,
  which its handler need not catch (see `errors.py`): `api.Timeout`, a 504, on one that asks for a resource that
  waits or a compute function; `api.Unavailable`, a 503, and `api.Conflict`, `api.Rejected`, `api.Denied` and
  `api.Failed`, each a 502, on one that asks for a database or an `api.http`; and `api.NotFound` and `api.BadReply`,
  502s too, on one that asks for an `api.http`. It adds `api.SignInRequiredError`, the 401 a handler raises for an
  anonymous request, to each route whose parameters ask for the user. `Route.answers` holds them all.

A route that answers a dataset says so, `dataset=True`, and answers it as Arrow when the request asks for it (see
`dataset.py`). A route's name is its handler's, and its operation's id in the OpenAPI document, so a service gives each
handler a name of its own.

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

import inspect
import math
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import InitVar, dataclass, field
from http import HTTPStatus
from pathlib import PurePosixPath, PureWindowsPath
from types import FunctionType
from typing import Annotated, Literal, Protocol, TypeAliasType, assert_never, get_args, get_origin

from fastapi import params
from pydantic import BaseModel, JsonValue, PydanticSchemaGenerationError, TypeAdapter, ValidationError
from starlette.responses import Response

from basis.api import strict, tables
from basis.api.compute import Compute, ComputeFunction
from basis.api.dataset import Rows, rows
from basis.api.dependencies import GivenCompute, GivenDatabase, GivenResource, GivenSettings
from basis.api.errors import IDEMPOTENCY_ERRORS, Answerable, BodyTooLargeError, DeclarationError, DomainError, Error, IdempotencyKeyMissingError, NotReadyError, OutcomeUnknownError
from basis.api.identity import GivenUser, SignInRequiredError, User, annotation_refusal
from basis.api.resources import Cache, CacheResource, Client, Clock, ClockResource, DatabaseResource, HttpResource, Ids, IdsResource, Opened, Reader, Resource, Session, Writer, check_outboxes
from basis.api.settings import Settings
from basis.core.errors import DATABASE_KINDS, KINDS, ServiceError, Timeout

RESERVED = ("/health", "/docs", "/redoc", "/openapi.json")
"""Paths the app itself answers: its health endpoints and its OpenAPI document and pages."""
NAME = re.compile(r"[a-z][a-z0-9-]{0,62}")
_PARAMETER = re.compile(r"\{[^}]*\}")
_REFERENCE = re.compile(r"[A-Za-z_][\w.]*:[A-Za-z_]\w*")
BASIS_ERRORS: tuple[Answerable, ...] = (*KINDS, NotReadyError, BodyTooLargeError, OutcomeUnknownError, *IDEMPOTENCY_ERRORS)
"""The errors basis raises itself, which a route never declares: `Route.answers` adds each where the route can meet it:
every kind another service's calls can raise, those a database's statements can (`DATABASE_KINDS`), a cache's read's
or a compute call's `Timeout`, `BodyTooLargeError` where it takes a body, `OutcomeUnknownError` where it writes, and
the errors of an `Idempotency-Key` where it keeps them (`IDEMPOTENCY_ERRORS`)."""

type Method = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
type Idempotent = Literal["required", "optional"]
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
    dataset: InitVar[bool] = False
    """Whether it answers a dataset, rows of one model, which it gives as Arrow when the request asks for it: see
    `dataset.py`. `rows` keeps what it declares."""
    smoke: Mapping[str, object] | None = None
    """A read-only example of a request, which `basis smoke` sends the deployed API and whose response it validates:
    `{"path": {...}, "query": {...}, "headers": {...}}`, as `basis call` takes one; a GET's alone."""
    anonymous: bool = False
    """That anyone may make the route's requests, which a route that is not a GET says, where it does not ask for the
    user: a route that changes what the service holds knows who asked, or says that it need not."""
    idempotency: Idempotent | None = None
    """For a route that writes, whether a request's `Idempotency-Key` is `"required"` or `"optional"`, kept in the
    written database's `api.idempotency` table with what the request answered, so the same request sent again is
    answered the same and changes nothing more: see `idempotent.py`. None for a route that reads none."""
    every_s: float | None = None
    """For a stream of live updates, which `api.stream` declares, the seconds between one call of its handler and the
    next: see `streams.py`. None for a route that answers each request once."""
    name: str = field(init=False)
    """The handler's name, which is the route's."""
    asks: tuple[Ask, ...] = field(init=False)
    """What the handler asks basis for."""
    writes: DatabaseResource | None = field(init=False)
    """The database the handler writes, which it asks for as an `api.Writer`: one at most, since a request commits one
    transaction."""
    answers: tuple[Answerable, ...] = field(init=False)
    """Every error the route may answer with: its declared `errors`, and each of basis's own that it can meet, the
    kinds of `api.ServiceError` among them."""
    reads: strict.Shape = field(init=False)
    """What it reads from a request, strictly."""
    rows: Rows | None = field(init=False)
    """The columns of the dataset it answers; None for a route that answers no dataset, as its `dataset=` declares."""
    table: bool = field(init=False)
    """Whether it answers an `api.Table[Row]`, a frame or an Arrow table, rather than rows its handler returns as models."""

    def __post_init__(self, dataset: bool) -> None:
        if not self.path.startswith("/"):
            msg = f"route {self.path!r} is not a path: start it with /, as /accrued-interest"
            raise DeclarationError(msg)
        if any(self.path == reserved or self.path.startswith(f"{reserved}/") for reserved in RESERVED):
            msg = f"route {self.path!r} is one the app answers itself ({', '.join(RESERVED)}): give the route another path"
            raise DeclarationError(msg)
        handler = self.handler
        if not inspect.isfunction(handler) or handler.__name__ == "<lambda>":
            msg = f"route {self.path!r}'s handler is a {type(handler).__name__}, not a named function: declare it with def, whose name is the route's"
            raise DeclarationError(msg)
        object.__setattr__(self, "name", handler.__name__)  # frozen: set once, from the handler
        if self.status not in range(200, 300):
            msg = f"route {self.path!r} succeeds with status {self.status}: give a 2xx, and raise an api.Error for each way it fails"
            raise DeclarationError(msg)
        if converted := [found for found in _PARAMETER.findall(self.path) if ":" in found]:
            alone = converted[0][1:].partition(":")[0]
            msg = (
                f"route {self.path!r} converts {', '.join(converted)} in its path: name the parameter alone, as {{{alone}}}, and give its type in {self.name}'s parameters, which basis reads strictly"
            )
            raise DeclarationError(msg)
        deadline: object = self.deadline_s
        if isinstance(deadline, bool) or not isinstance(deadline, int | float) or not 0 < deadline < math.inf:
            msg = f"{self.name}'s deadline is {deadline!r}: give it deadline_s, the seconds the route has to answer, above 0"
            raise DeclarationError(msg)
        _check_response(self)
        _check_stream(self)
        _check_smoke(self)
        table_row = tables.row_of(self.response)
        object.__setattr__(self, "table", table_row is not None)
        if table_row is not None:
            object.__setattr__(self, "rows", rows(list[table_row], self.name, table=True))
        else:
            object.__setattr__(self, "rows", rows(self.response, self.name) if dataset else None)
        object.__setattr__(self, "errors", _declared_errors(self.name, self.errors))
        for path, asked in _asked_within(handler, (), set()):
            msg = (
                f"{self.name}'s dependency {' -> '.join(path)} asks for {asked}: ask for it in {self.name}'s own parameters,"
                " and pass what it gives to the dependency, so the route shows everything it reaches"
            )
            raise DeclarationError(msg)
        asks = tuple(_asks_for(handler))
        for ask in asks:
            if isinstance(ask.given, type) and issubclass(ask.given, User) and (fix := annotation_refusal(ask.given, ask.annotation)) is not None:
                msg = f"{self.name}'s parameter {ask.parameter} asks for the user: {fix}"
                raise DeclarationError(msg)
        object.__setattr__(self, "asks", asks)
        _check_anonymous(self, asks)
        object.__setattr__(self, "writes", _written(self, asks))
        _check_idempotency(self)
        object.__setattr__(self, "reads", strict.shape(self.path, handler, self.name))
        body = (BodyTooLargeError,) if self.reads.body is not None else ()
        unknown = (OutcomeUnknownError,) if self.writes is not None else ()
        keyed = () if self.idempotency is None else tuple(error for error in IDEMPOTENCY_ERRORS if self.idempotency == "required" or error is not IdempotencyKeyMissingError)
        object.__setattr__(self, "answers", (*self.errors, *_met(asks), *body, *unknown, *keyed))

    @property
    def shape(self) -> tuple[Method, str]:
        """The method and the path with its parameters unnamed: two routes of one shape match the same requests."""
        return self.method, _PARAMETER.sub("{}", self.path)


class _Declare(Protocol):
    def __call__(
        self,
        path: str,
        handler: FunctionType,
        *,
        deadline_s: float,
        response: object,
        errors: Sequence[type[Error]],
        status: int = HTTPStatus.OK,
        dataset: bool = False,
        smoke: Mapping[str, object] | None = None,
        anonymous: bool = False,
        idempotency: Idempotent | None = None,
    ) -> Route: ...


def _route(method: Method) -> _Declare:
    def declare(
        path: str,
        handler: FunctionType,
        *,
        deadline_s: float,
        response: object,
        errors: Sequence[type[Error]],
        status: int = HTTPStatus.OK,
        dataset: bool = False,
        smoke: Mapping[str, object] | None = None,
        anonymous: bool = False,
        idempotency: Idempotent | None = None,
    ) -> Route:
        return Route(method, path, handler, deadline_s, response, errors, status, dataset, smoke, anonymous, idempotency)

    declare.__name__ = declare.__qualname__ = method.lower()
    declare.__doc__ = (
        f"A route that answers {method} requests to `path` with `handler`: within `deadline_s` seconds, with `response` and `status` when it succeeds, and with one of `errors` when it fails;"
        " with `dataset=True`, `response` is rows, which it answers as Arrow when the request asks for it."
        + ("" if method == "GET" else " Its handler asks for the user, or the route declares `anonymous=True`, that anyone may make it; with `idempotency=`, a request's Idempotency-Key is kept.")
    )
    return declare


def stream(path: str, handler: FunctionType, *, deadline_s: float, every_s: float, response: object, errors: Sequence[type[Error]]) -> Route:
    """A stream of live updates, as server-sent events, that GET requests to `path` open: basis calls `handler` once
    when the stream opens and again every `every_s` seconds, each call within `deadline_s` and in a transaction of its
    own, and sends `response`, the model it returns, as an event whenever it changes, the first one always. A failure of
    the first call answers its problem, with its status, as any route's does; a failure of a later one is the stream's
    last event, its problem. See `streams.py`."""
    return Route("GET", path, handler, deadline_s, response, errors, every_s=every_s)


def _check_idempotency(route: Route) -> None:
    """That a route that keeps its requests' keys says how, and writes one database, whose answer is a model's JSON."""
    declared: object = route.idempotency
    if declared is None:
        return
    if declared not in {"required", "optional"}:
        msg = f"{route.name} declares idempotency={declared!r}: give idempotency='required' or 'optional', whether its requests must send an Idempotency-Key"
        raise DeclarationError(msg)
    if route.writes is None:
        msg = f"{route.name} declares idempotency=..., and writes no database to keep its requests' keys in: ask for an api.Writer of a database that declares api.idempotency(...), or drop it"
        raise DeclarationError(msg)
    if route.rows is not None:
        msg = f"{route.name} declares idempotency=..., and answers a dataset, which its key cannot keep: answer a model"
        raise DeclarationError(msg)


def _check_stream(route: Route) -> None:
    """That a stream of live updates waits a time between its calls, and sends a model, from an `async def`, which a
    stream awaits again and again. `stream` declares no dataset and no smoke example."""
    every: object = route.every_s
    if every is None:
        return
    if isinstance(every, bool) or not isinstance(every, int | float) or not 0 < every < math.inf:
        msg = f"{route.name}'s every_s is {every!r}: give the seconds between one call of its handler and the next, above 0"
        raise DeclarationError(msg)
    if not (isinstance(route.response, type) and issubclass(route.response, BaseModel)):
        msg = f"{route.name} streams {strict.named(route.response)}: a stream sends one event at a time, so declare a pydantic model of what each event holds"
        raise DeclarationError(msg)
    if not inspect.iscoroutinefunction(route.handler):
        msg = f"{route.name} streams, and basis awaits its handler every every_s: declare it `async def {route.name}`"
        raise DeclarationError(msg)


def _check_anonymous(route: Route, asks: Sequence[Ask]) -> None:
    """That a route which is not a GET asks for the user or declares that it is anonymous, and that one which declares
    it is anonymous is neither a GET nor asks for the user."""
    signed = any(isinstance(ask.given, type) and issubclass(ask.given, User) for ask in asks)
    anonymous: object = route.anonymous
    if not isinstance(anonymous, bool):
        msg = f"{route.name} declares anonymous={anonymous!r}: give anonymous=True, or leave it out"
        raise DeclarationError(msg)
    if anonymous and route.method == "GET":
        msg = f"{route.name} is a GET, which asks for no user unless it says so: drop anonymous=True"
        raise DeclarationError(msg)
    if anonymous and signed:
        msg = f"{route.name} asks for the user, and declares anonymous=True: drop anonymous=True, since the handler sees who asked, or None"
        raise DeclarationError(msg)
    if not anonymous and not signed and route.method != "GET":
        msg = (
            f"{route.name} is a {route.method}, which changes what the service holds, and asks for no user: ask for it in its parameters,"
            " as Annotated[User | None, api.given(User)], or declare anonymous=True where anyone may make it"
        )
        raise DeclarationError(msg)


def _check_smoke(route: Route) -> None:
    """That a route's read-only example is a GET's, and holds only what a request gives."""
    if route.smoke is None:
        return
    if route.method != "GET":
        msg = f"{route.name} declares a smoke example, and is a {route.method}: basis smoke sends read-only requests alone, so give the example to a GET"
        raise DeclarationError(msg)
    if not isinstance(route.smoke, Mapping) or (sorted(set(route.smoke) - {"path", "query", "headers"})):
        msg = f"{route.name}'s smoke example is not a request: give a mapping of path, query and headers, as basis call takes one"
        raise DeclarationError(msg)


def _check_response(route: Route) -> None:
    """That the route declares its response as its handler's return annotation, and None exactly when it answers 204."""
    returns = inspect.signature(route.handler, eval_str=True).return_annotation
    declared = route.response
    if returns is inspect.Signature.empty:
        msg = f"{route.name} has no return annotation: annotate it `-> {strict.named(declared)}`, the response its route declares"
        raise DeclarationError(msg)
    if returns != declared and not (tables.row_of(declared) is not None and tables.is_frame_or_table(returns)):
        msg = f"{route.name} returns {strict.named(returns)}, and its route declares response={strict.named(declared)}: declare the one it returns"
        raise DeclarationError(msg)
    if isinstance(declared, type) and issubclass(declared, Response):
        msg = (
            f"{route.name} answers a {declared.__qualname__}, which basis cannot check, nor write in the document: declare the pydantic model it answers,"
            " or api.stream(...) for a stream of live updates"
        )
        raise DeclarationError(msg)
    if declared is not None and tables.row_of(declared) is None:
        try:
            TypeAdapter(declared)
        except PydanticSchemaGenerationError:
            msg = f"{route.name}'s response is {strict.named(declared)}, which pydantic cannot write as JSON: declare a pydantic model of what it answers"
            raise DeclarationError(msg) from None
    strict.aware(route.name, "response", declared)
    if (declared is None) != (route.status == HTTPStatus.NO_CONTENT):
        fix = "give it status=HTTPStatus.NO_CONTENT" if declared is None else "declare the response it has, or response=None with no content"
        msg = f"{route.name} answers {route.status} with response={strict.named(declared)}: a route answers no body exactly when its status is 204, so {fix}"
        raise DeclarationError(msg)


def _declared_errors(name: str, declared: object) -> tuple[type[Error], ...]:
    if isinstance(declared, str | bytes) or not isinstance(declared, Sequence):
        msg = f"{name}'s errors are {declared!r}: give errors=[...], each api.Error its handler may raise, or errors=[] for none"
        raise DeclarationError(msg)
    found: list[type[Error]] = []
    for error in declared:
        if isinstance(error, type) and issubclass(error, ServiceError):
            msg = f"{name} declares {error.__qualname__}, a kind of api.ServiceError, which basis adds to each route that asks for what can raise it: remove it from errors=[...]"
            raise DeclarationError(msg)
        if not (isinstance(error, type) and issubclass(error, Error)):
            msg = f"{name} declares {error!r} among its errors: give each api.Error subclass its handler may raise"
            raise DeclarationError(msg)
        if error in BASIS_ERRORS:
            msg = f"{name} declares {error.__qualname__}, which basis raises itself and adds to each route that can meet it: remove it from errors=[...]"
            raise DeclarationError(msg)
        if error is SignInRequiredError:
            msg = f"{name} declares SignInRequiredError, which basis adds to each route whose parameters ask for the user: remove it from errors=[...]"
            raise DeclarationError(msg)
        if vars(error).get("_abstract", False):
            msg = f"{name} declares {error.__qualname__}, a base that is never raised itself: declare each of its subclasses the handler raises"
            raise DeclarationError(msg)
        if error in found:
            msg = f"{name} declares {error.__qualname__} twice: declare each error once"
            raise DeclarationError(msg)
        if issubclass(error, DomainError) and (same := next((other for other in found if issubclass(other, DomainError) and other.exception is error.exception), None)) is not None:
            msg = f"{name} declares two meanings of {error.exception.__qualname__}, {same.title!r} and {error.title!r}: declare the one api.error its errors.py gives it"
            raise DeclarationError(msg)
        found.append(error)
    return tuple(found)


def _met(asks: Sequence[Ask]) -> Iterator[Answerable]:
    """The errors of basis's own that a route which asks for `asks` can meet, each kind of `ServiceError` once, in
    `KINDS`' order, and `SignInRequiredError` where it asks for the user, which its handler raises for an anonymous
    request that needs one."""
    met: set[type[ServiceError]] = set()
    for ask in asks:
        match ask.given:
            case HttpResource():
                met.update(KINDS)
            case DatabaseResource():
                met.update(DATABASE_KINDS)
            case CacheResource() | ComputeFunction():
                met.add(Timeout)
            case _:  # the settings, the user, the clock and the ids, which never fail
                pass
    yield from (kind for kind in KINDS if kind in met)
    if any(isinstance(ask.given, type) and issubclass(ask.given, User) for ask in asks):
        yield SignInRequiredError


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
    example_user: Mapping[str, JsonValue] | None = None
    """The claims of a user, as a token carries them, `{"sub": "jsmith", "desk": "rates"}`, whom `basis check --full`
    signs in as, with the development key, when it sends the app on its fakes Schemathesis's requests, so a signed-in
    handler's body runs; checked against `user` when the service is declared. None for `{"sub": "example-user"}`,
    which a user with no claim but its subject takes."""
    fakes: str | None = None
    """Where its fakes for `basis dev` and `basis call --fake` are made: `module:function`, a function that takes
    nothing and returns a fake for each database and other service it declares, by resource, as `api.build` takes them.
    None for a service that has none yet."""
    ui: str | None = None
    """The folder of a built UI the app serves at `/`, beside its routes, which then mount under `/api`, so one
    container serves a UI and its API on one origin: a relative path, as `web/dist`, found from the folder the app runs
    in, and holding `index.html`. None for an app that serves no UI, whose routes are at their own paths."""

    def __post_init__(self) -> None:
        if not NAME.fullmatch(self.name):
            msg = f"service name {self.name!r}: a service's name is lower-case letters, digits and hyphens, starting with a letter"
            raise DeclarationError(msg)
        if self.fakes is not None and not _REFERENCE.fullmatch(self.fakes):
            msg = f"service {self.name}'s fakes are {self.fakes!r}: give fakes='module:function', the function that makes them, as 'accruals.dev:fakes'"
            raise DeclarationError(msg)
        _check_ui(self.name, self.ui)
        if not (isinstance(self.settings, type) and issubclass(self.settings, Settings)):
            msg = f"service {self.name}'s settings are {self.settings!r}, not a subclass of api.Settings: declare `class Settings(api.Settings)`, or pass api.Settings itself"
            raise DeclarationError(msg)
        routes = tuple(self.routes)
        object.__setattr__(self, "routes", routes)  # frozen: a list given here can no longer change the service
        resources = tuple(self.resources)
        object.__setattr__(self, "resources", resources)
        _check_resources(self.name, resources)
        if not routes:
            msg = f"service {self.name} declares no routes: give it routes=[api.get(path, handler), ...]"
            raise DeclarationError(msg)
        for route in routes:
            if not isinstance(route, Route):
                msg = f"service {self.name} has a {type(route).__name__} among its routes: declare each with api.get(path, handler) or api.post, api.put, api.patch, api.delete"
                raise DeclarationError(msg)
            for ask in route.asks:
                if isinstance(ask.given, ComputeFunction):
                    continue  # a function, which the service runs and never configures
                if isinstance(ask.given, type) and issubclass(ask.given, Settings) and not issubclass(self.settings, ask.given):
                    msg = f"{route.name} asks for {ask.given.__qualname__}, which service {self.name} does not give: its settings are {self.settings.__qualname__}"
                    raise DeclarationError(msg)
                if not isinstance(ask.given, type) and ask.given not in resources:
                    msg = f"{route.name} asks for {ask.given.name}, which service {self.name} does not declare: add it to the service's resources=[...]"
                    raise DeclarationError(msg)
        _check_user(self.name, self.user, routes)
        _check_example_user(self.name, self.user, self.example_user)
        check_outboxes(self.name, [(route.name, route.deadline_s, route.writes) for route in routes if route.writes is not None])
        _check_keys(routes)
        seen: dict[tuple[Method, str], Route] = {}
        for route in routes:
            if (first := seen.get(route.shape)) is not None:
                same = route.path if route.path == first.path else f"{first.path} and {route.path}, which match the same paths,"
                msg = f"service {self.name} declares {route.method} {same} twice: give each method and path one handler"
                raise DeclarationError(msg)
            seen[route.shape] = route
        _check_names(self.name, routes)


def _check_keys(routes: Sequence[Route]) -> None:
    """That each route that keeps its requests' `Idempotency-Key`s writes a database that declares a table for them."""
    for route in routes:
        if route.idempotency is not None and route.writes is not None and route.writes.idempotency is None:
            msg = (
                f"{route.name} keeps its requests' Idempotency-Keys, and {route.writes.name} declares no table for them:"
                f" declare one, as api.database({route.writes.name!r}, ..., idempotency=api.idempotency(TABLE, keep_s=...)), and create it with `basis ddl`"
            )
            raise DeclarationError(msg)


def _check_ui(name: str, folder: str | None) -> None:
    """That a service's UI folder, if it serves one, is a relative path inside the app's own folder."""
    if folder is None:
        return
    if not folder or PurePosixPath(folder).is_absolute() or PureWindowsPath(folder).is_absolute() or ".." in PurePosixPath(folder.replace("\\", "/")).parts:
        msg = f"service {name}'s ui is {folder!r}: give the built UI's folder as a relative path inside the app's own folder, as ui='web/dist'"
        raise DeclarationError(msg)


def _annotated(annotation: object) -> tuple[object, tuple[object, ...]]:
    """`annotation`'s type and its metadata, through type aliases, as `type Warehouse = Annotated[api.Reader,
    api.given(WAREHOUSE)]` names one, and nested `Annotated`s, as FastAPI reads them."""
    metadata: list[object] = []
    while True:
        if isinstance(annotation, TypeAliasType):
            annotation = annotation.__value__
        elif get_origin(annotation) is Annotated:
            metadata.extend(get_args(annotation)[1:])
            annotation = get_args(annotation)[0]
        else:
            return annotation, tuple(metadata)


def _asks_for(handler: FunctionType) -> Iterator[Ask]:
    """What each of `handler`'s parameters asks basis for, through `given`, its annotations evaluated even where they
    are strings, as FastAPI evaluates them."""
    for parameter in inspect.signature(handler, eval_str=True).parameters.values():
        base, metadata = _annotated(parameter.annotation)
        for marker in (*metadata, parameter.default):
            if isinstance(marker, params.Depends) and isinstance(marker.dependency, GivenSettings | GivenUser):
                yield Ask(parameter.name, base, marker.dependency.kind)
            elif isinstance(marker, params.Depends) and isinstance(marker.dependency, (GivenDatabase, GivenResource)):
                yield Ask(parameter.name, base, marker.dependency.resource)
            elif isinstance(marker, params.Depends) and isinstance(marker.dependency, GivenCompute):
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
        base, metadata = _annotated(parameter.annotation)
        for marker in (*metadata, parameter.default):
            if not isinstance(marker, params.Depends):
                continue
            dependency = marker.dependency if marker.dependency is not None else base  # `Depends()` depends on the type
            if isinstance(dependency, GivenDatabase | GivenResource):
                if path:
                    yield (*path, parameter.name), dependency.resource.name
            elif isinstance(dependency, GivenUser):
                if path:
                    yield (*path, parameter.name), "the user"
            elif isinstance(dependency, GivenCompute):
                if path:
                    yield (*path, parameter.name), dependency.declared.name
            elif not isinstance(dependency, GivenSettings) and callable(dependency):
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
            raise DeclarationError(msg)
        seen.add(resource.name)
        if not _annotated_as_given(resource, ask.annotation):
            msg = f"{route.name}'s parameter {ask.parameter} asks for {resource.name}, {_annotate(resource)}"
            raise DeclarationError(msg)
        if not isinstance(resource, ClockResource | IdsResource) and not inspect.iscoroutinefunction(route.handler):
            msg = f"{route.name} asks for {resource.name}, whose calls are awaited: declare it `async def {route.name}`"
            raise DeclarationError(msg)
        if ask.annotation is Writer and isinstance(resource, DatabaseResource):
            written.append(resource)
    if written and route.method == "GET":
        msg = f"{route.name} writes {written[0].name}, and a GET only reads: declare the route with api.post, api.put, api.patch or api.delete, or ask for an api.Reader"
        raise DeclarationError(msg)
    if len(written) > 1:
        msg = f"{route.name} writes {' and '.join(found.name for found in written)}, and a request commits one transaction: write one database per route"
        raise DeclarationError(msg)
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
            raise DeclarationError(msg)
    expected = f"api.Compute[[{', '.join(strict.named(parameter) for parameter in declared.parameters)}], {strict.named(declared.result)}]"
    if (
        get_origin(ask.annotation) is not Compute
        or [strict.bare(found) for found in get_args(ask.annotation)[0]] != [strict.bare(found) for found in declared.parameters]
        or strict.bare(get_args(ask.annotation)[1]) != strict.bare(declared.result)
    ):
        msg = f"{route.name}'s parameter {ask.parameter} asks for the compute function {declared.name}: annotate it Annotated[{expected}, api.given(...)], its parameters and its result"
        raise DeclarationError(msg)
    if not inspect.iscoroutinefunction(route.handler):
        msg = f"{route.name} asks for the compute function {declared.name}, whose calls are awaited: declare it `async def {route.name}`"
        raise DeclarationError(msg)


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
        raise DeclarationError(msg)
    for route in routes:
        for ask in route.asks:
            if isinstance(ask.given, type) and issubclass(ask.given, User) and (user is None or not issubclass(user, ask.given)):
                given = f"its user is {user.__qualname__}" if user is not None else "it declares no user=..."
                msg = f"{route.name} asks for {ask.given.__qualname__}, which service {service} does not give: {given}"
                raise DeclarationError(msg)


def _check_example_user(service: str, user: type[User] | None, example: Mapping[str, JsonValue] | None) -> None:
    """That the service's example user is one its user reads, where it declares one."""
    if example is None:
        return
    if user is None:
        msg = f"service {service} declares example_user, and no user: declare user=..., the api.User its tokens name, or drop example_user"
        raise DeclarationError(msg)
    try:
        user.model_validate(example)
    except ValidationError as error:
        wrong = "; ".join(f"{'.'.join(map(str, entry['loc']))}: {entry['msg']}" for entry in error.errors(include_url=False, include_input=False))
        msg = f"service {service}'s example_user is not a {user.__qualname__}: {wrong}: give each claim it needs, by the claim's name"
        raise DeclarationError(msg) from None


def _check_names(service: str, routes: Sequence[Route]) -> None:
    """That each route's name, its handler's, which is its operation's id in the OpenAPI document, is its own."""
    names: set[str] = set()
    for route in routes:
        if route.name in names:
            msg = f"service {service} has two routes named {route.name}: a route's name is its handler's, and its operation's id in the OpenAPI document, so give each handler a name of its own"
            raise DeclarationError(msg)
        names.add(route.name)


def _check_resources(service: str, resources: Sequence[object]) -> None:
    names: set[str] = set()
    for resource in resources:
        if not isinstance(resource, _RESOURCES):
            msg = f"service {service} has a {type(resource).__name__} among its resources: declare each with api.database, api.http, api.cache, api.clock or api.ids"
            raise DeclarationError(msg)
        if resource.name in names:
            msg = f"service {service} declares {resource.name} twice: give each resource a name of its own"
            raise DeclarationError(msg)
        names.add(resource.name)
