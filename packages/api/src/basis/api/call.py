"""`basis call ROUTE [REQUEST]`: one request to one route, in this process, without a server, and its response printed.
The app is the one `basis serve` runs, its lifespan included: on its real resources, as its settings configure them,
with `--real`, or on the fakes its service declares (`dev.py`) with `--fake`. One of the two is always given. The
route's compute functions run in the process pool, which `BASIS_COMPUTE__WORKERS` sizes; other routes' are never
called, so they start none.

ROUTE is the route's name, its handler's. REQUEST is one JSON object, `-` to read it from stdin, or `@FILE` to read
it from a file, with any of:

    {"path": {"security": "US912810TM64"}, "query": {"face": "100", "settle": "2026-11-06"},
     "headers": {"authorization": "Bearer ..."}, "body": {...}, "user": {"sub": "jsmith"}}

A path or query value is the text the URL carries; a number or `true`/`false` is sent as JSON writes it, and a list
gives a query key once for each value. The body is sent as JSON. `user` is the claims of a user to sign in as, on the
fakes alone, with the development key, which `basis dev`'s app verifies; `--real` refuses it. A request as it was sent,
as `basis check --full` writes each one Schemathesis finds, gives its `target`, the path and the query exactly as the
URL carried them, percent-encoded, in place of `path` and `query`, and its body's bytes as `content`, its text, or
`content_base64`, in place of `body`. The route reads all of it as strictly as it reads any request. A response
that succeeds prints its JSON on stdout; a stream of live updates (`api.stream`) prints its first event's, and ends. One that fails prints its problem on stdout too, and the one `ERROR` line on
stderr, and exits 1. A response that is not JSON, of either kind, prints the one `ERROR` line alone, with its status
and its content type."""

import base64
import json
import re
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import Literal, Self
from urllib.parse import quote, urlsplit

from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError, model_validator

from basis.api import dev, errors, streams, strict, tokens
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
    target: str | None = None
    """The path and the query exactly as the URL carried them, percent-encoded, in place of `path` and `query`."""
    content: str | None = None
    """The body's bytes, as text, in place of `body`: sent as they are, with the content type `headers` give."""
    content_base64: str | None = None
    """The body's bytes, in base64, in place of `body`, for bytes that are no text."""
    user: dict[str, JsonValue] | None = None
    """The claims of a user to sign in as, on the fakes alone: `{"sub": "jsmith"}`, and any others the service's user
    needs."""

    @model_validator(mode="after")
    def _one_form(self) -> Self:
        if self.target is not None and ({"path", "query"} & self.model_fields_set):
            msg = 'a "target" carries the path and the query as sent, so give no "path" or "query" beside it'
            raise ValueError(msg)
        if len({"body", "content", "content_base64"} & self.model_fields_set) > 1:
            msg = 'give the body once, as "body", "content" or "content_base64"'
            raise ValueError(msg)
        return self

    def sent(self) -> bytes | None:
        """The body's bytes, or None when the request gives none."""
        if self.content_base64 is not None:
            return base64.b64decode(self.content_base64, validate=True)
        if self.content is not None:
            return self.content.encode()
        return json.dumps(self.body).encode() if "body" in self.model_fields_set else None


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
    if given.user is not None and resources != "fake":
        fix = "basis call signs in as a user on the fakes alone, with the development key: give --fake, or a token of the environment's in its headers"
        msg = f'the request to {name} gives "user": {fix}'
        raise DeclarationError(msg)
    logged(service)
    reaches = any(isinstance(resource, DatabaseResource | HttpResource) for resource in service.resources)
    faked: dict[Faked, Fake] = dev.fakes(service) if resources == "fake" and (service.fakes is not None or reaches) else {}  # a service that reaches nothing needs no fakes
    for other in service.routes:  # another route's compute functions are never called here, so they start no process
        faked |= {ask.given: Compute() for ask in other.asks if isinstance(ask.given, ComputeFunction) and ask.given not in {found.given for found in route.asks}}
    configured = dev.settings(service, settings) if resources == "fake" else settings
    headers = given.headers | ({"content-type": "application/json"} if "body" in given.model_fields_set else {})
    if given.user is not None:
        headers |= {"authorization": f"Bearer {signed(service, configured, given.user)}"}
    app = build(service, settings=configured, fakes=faked)
    params: list[tuple[str, str | float | None]] | None = None
    if given.target is not None:
        target = _target(route, given.target)
    else:
        target = _url(route, given)
        params = [(key, _text(value)) for key, values in given.query.items() for value in (values if isinstance(values, list) else [values])]
    with TestClient(app.asgi, raise_server_exceptions=False) as client:  # runs the lifespan, as `basis serve` does
        if route.every_s is not None:
            with client.stream(route.method, target, params=params, headers=headers, content=given.sent()) as streamed:
                if streams.MEDIA_TYPE in streamed.headers.get("content-type", ""):
                    return route, Called(streamed.status_code, _first_event(streamed.iter_lines()))  # and leaves, which ends the stream
                streamed.read()
                response = streamed
        else:
            response = client.request(route.method, target, params=params, headers=headers, content=given.sent())
    kind = response.headers.get("content-type")
    if response.content and not strict.is_json(kind):
        fix = "basis call gives JSON alone, so serve the API with basis dev, and request the route there"
        msg = f"{route.name} answered {response.status_code} with {kind or 'a body of no content type'}, not JSON: {fix}"
        raise DeclarationError(msg)
    return route, Called(response.status_code, response.json() if response.content else None)


def _first_event(lines: Iterator[str]) -> JsonValue:
    """The data of a stream's first event, which `basis call` gives as a stream's answer."""
    data = next((line.removeprefix("data: ") for line in lines if line.startswith("data: ")), "null")
    return TypeAdapter(JsonValue).validate_json(data)


def signed(service: Service, settings: Settings | None, claims: Mapping[str, JsonValue]) -> str:
    """A token for the user `claims` give, signed with the development key that `settings` verify with, valid for an
    hour."""
    identity = settings.identity if settings is not None else None
    if service.user is None or identity is None:
        msg = f'service {service.name} verifies no tokens, so no request to it signs in: drop "user"'
        raise DeclarationError(msg)
    subject = claims.get("sub")
    if not isinstance(subject, str):
        msg = f'the user to sign in to service {service.name} as gives no "sub": give its subject, as {{"sub": "jsmith"}}'
        raise DeclarationError(msg)
    now = datetime.now(UTC)
    return tokens.sign(identity, subject, issued=now, expires=now + timedelta(hours=1), claims={key: value for key, value in claims.items() if key != "sub"})


def _target(route: Route, target: str) -> str:
    """`target`, once its path is one `route` answers."""
    pattern = re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(route.path))
    if not re.fullmatch(pattern, urlsplit(target).path):
        msg = f"the request to {route.name} is sent to {target}, which is not {route.name}'s path {route.path}: give its target a path the route answers"
        raise DeclarationError(msg)
    return target


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
