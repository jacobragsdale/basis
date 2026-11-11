"""`basis call ROUTE [REQUEST]`: one request to one route, in this process, without a server, and its response printed.
The app is the one `basis serve` runs, its lifespan included: on its real resources, as its settings configure them,
with `--real`, or on the fakes its service declares (`dev.py`) with `--fake`. One of the two is always given. The
route's compute functions run in the process pool, which `BASIS_COMPUTE__WORKERS` sizes; other routes' are never
called, so they start none.

ROUTE is the route's name, its handler's. REQUEST is one JSON object, or `-` to read it from stdin, with any of:

    {"path": {"security": "US912810TM64"}, "query": {"face": "100", "settle": "2026-11-06"},
     "headers": {"authorization": "Bearer ..."}, "body": {...}}

A path or query value is the text the URL carries; a number or `true`/`false` is sent as JSON writes it, and a list
gives a query key once for each value. The body is sent as JSON. The route reads all of it as strictly as it reads
any request. A response that succeeds prints its JSON on stdout. One that fails prints its problem on stdout too, and
the one `ERROR` line on stderr, and exits 1. A response that is not JSON, of either kind, prints the one `ERROR` line
alone, with its status and its content type."""

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Literal
from urllib.parse import quote

from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from basis.api import dev, errors, strict
from basis.api.app import build
from basis.api.compute import ComputeFunction
from basis.api.fakes import Compute, Fake, Faked
from basis.api.resources import DatabaseResource, HttpResource
from basis.api.server import logged
from basis.api.service import DeclarationError, Route, Service
from basis.api.settings import Settings
from basis.core.verbs import fail

type Scalar = str | int | float | bool


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: dict[str, Scalar] = Field(default_factory=dict)
    query: dict[str, Scalar | list[Scalar]] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    body: JsonValue = None
    """The body, when the request gives one: `null` is a body too."""


@dataclass(frozen=True)
class Called:
    status: int
    body: object
    """The response's JSON, or None when it has no body."""


def call(service: Service, name: str, request: str, *, resources: Literal["real", "fake"], settings: Settings | None = None) -> int:
    """Run the route of `service` named `name` on `request`, print its response, and return the exit code: `basis call`."""
    route, called = _called(service, name, request, resources=resources, settings=settings)
    if called.body is not None:
        sys.stdout.write(f"{json.dumps(called.body, indent=2)}\n")
    if called.status < errors.FIRST_ERROR:
        return 0
    return _failed(route, called)


def answer(service: Service, name: str, request: str | Mapping[str, object], *, resources: Literal["real", "fake"] = "fake", settings: Settings | None = None) -> Called:
    """`api.call_route`: one request to the route of `service` named `name`, in this process, and its answer, as `basis call`
    makes it, for a caller in Python, such as a platform's own command or a test."""
    return _called(service, name, request, resources=resources, settings=settings)[1]


def _called(service: Service, name: str, request: str | Mapping[str, object], *, resources: Literal["real", "fake"], settings: Settings | None = None) -> tuple[Route, Called]:
    """One request to the route of `service` named `name`, in this process: `request` a JSON object, as text or a
    mapping, of `path`, `query`, `headers` and `body`; on the service's fakes or its real resources; with `settings`, or
    its settings read from the environment. Its answer, its status and its JSON, which `api.call` gives a caller in
    Python, as a library, and `basis call` prints."""
    route = next((found for found in service.routes if found.name == name), None)
    if route is None:
        msg = f"service {service.name} has no route named {name}: give one of {', '.join(found.name for found in service.routes)}, its handler's name"
        raise DeclarationError(msg)
    try:
        given = Request.model_validate_json(request) if isinstance(request, str) else Request.model_validate(request)
    except ValidationError as error:
        wrong = "; ".join(f"{'.'.join(map(str, entry['loc'])) or 'the request'}: {entry['msg']}" for entry in error.errors(include_url=False, include_input=False))
        msg = f'the request to {name} is not valid: {wrong}: give one JSON object of "path", "query", "headers" and "body", each where the route needs it'
        raise DeclarationError(msg) from None
    logged(service)
    reaches = any(isinstance(resource, DatabaseResource | HttpResource) for resource in service.resources)
    faked: dict[Faked, Fake] = dev.fakes(service) if resources == "fake" and (service.fakes is not None or reaches) else {}  # a service that reaches nothing needs no fakes
    for other in service.routes:  # another route's compute functions are never called here, so they start no process
        faked |= {ask.given: Compute() for ask in other.asks if isinstance(ask.given, ComputeFunction) and ask.given not in {found.given for found in route.asks}}
    app = build(service, settings=dev.settings(service, settings) if resources == "fake" else settings, fakes=faked)
    with TestClient(app.asgi, raise_server_exceptions=False) as client:  # runs the lifespan, as `basis serve` does
        response = client.request(
            route.method,
            _url(route, given),
            params=[(key, _text(value)) for key, values in given.query.items() for value in (values if isinstance(values, list) else [values])],
            headers=given.headers | ({"content-type": "application/json"} if "body" in given.model_fields_set else {}),
            content=json.dumps(given.body).encode() if "body" in given.model_fields_set else None,
        )
    kind = response.headers.get("content-type")
    if response.content and not strict.is_json(kind):
        fix = "basis call gives JSON alone, so serve the API with basis dev, and request the route there"
        msg = f"{route.name} answered {response.status_code} with {kind or 'a body of no content type'}, not JSON: {fix}"
        raise DeclarationError(msg)
    return route, Called(response.status_code, response.json() if response.content else None)


def _url(route: Route, given: Request) -> str:
    keys = {parameter.key for parameter in route.reads.path}
    if missing := sorted(keys - set(given.path)):
        msg = f'{route.name}\'s path {route.path} needs {", ".join(missing)}: give each in the request\'s path, as {{"path": {{"{missing[0]}": ...}}}}'
        raise DeclarationError(msg)
    if unknown := sorted(set(given.path) - keys):
        msg = f"{route.name}'s path {route.path} has no {', '.join(unknown)}: give only {', '.join(sorted(keys)) or 'none'} in the request's path"
        raise DeclarationError(msg)
    url = route.path
    for key, value in given.path.items():
        url = url.replace(f"{{{key}}}", quote(_text(value), safe=""))
    return url


def _text(value: Scalar) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def _failed(route: Route, called: Called) -> int:
    kind = errors.ValidationProblem if called.status == HTTPStatus.UNPROCESSABLE_CONTENT else errors.Problem
    try:
        problem = kind.model_validate(called.body)
    except ValidationError:
        return fail(f"{route.name} answered {called.status}, without a problem", "see its log lines above")
    fields = problem.errors if isinstance(problem, errors.ValidationProblem) else ()
    if problem.detail is not None:
        why = problem.detail
    elif fields:
        why = "; ".join(f"body {found.pointer}: {found.detail}" if isinstance(found, errors.PointerError) else f"parameter {found.parameter}: {found.detail}" for found in fields)
    else:
        why = problem.title
    return fail(f"{route.name} answered {problem.status} {problem.type}, {problem.title}", why)
