"""Each route as the app serves it, around FastAPI's own handling of the request:

- its deadline starts when it begins to serve the request, and caps every call the request makes through a resource or
  a compute function, the commit of its writes included (`deadlines.py`);
- it reads the request strictly first (`strict.py`): the path and the query by their types' grammars, and a JSON body
  in pydantic's strict mode, which FastAPI then gets as the value it is;
- an `api.Error` the route does not declare, or an `HTTPException` raised inside it, answers 500 as a defect, logged
  once by the request boundary with what the route should have declared;
- the OpenAPI document lists each error the route answers with under its status, as `application/problem+json`."""

from collections.abc import Callable, Coroutine, Iterator
from http import HTTPStatus
from typing import ClassVar, override

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import JsonValue, TypeAdapter
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from basis.api import deadlines, strict
from basis.api.errors import MEDIA_TYPE, Error, Problem
from basis.api.service import Route

_INVALID = "invalid-request: The request is not valid"


class UndeclaredError(RuntimeError):
    """A route that raised an error it does not declare: a defect, whose 500 says nothing of it to the caller."""


class _Read(Request):
    """The request as FastAPI's handler gets it, once basis has read its body: the value it validated, as its JSON."""

    def __init__(self, request: Request, raw: bytes, value: object) -> None:
        super().__init__(request.scope, request.receive)
        self._raw = raw
        self._value = value

    @override
    async def body(self) -> bytes:
        return self._raw

    @override
    async def json(self) -> object:
        return self._value


class Served(APIRoute):
    """A route's FastAPI route; `route_class` makes one for each of a service's routes."""

    declared: ClassVar[Route]

    @override
    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        declared = self.declared
        with deadlines.route(declared.name, declared.deadline_s):
            await super().handle(scope, receive, send)

    @override
    def get_route_handler(self) -> Callable[[Request], Coroutine[object, object, Response]]:
        handle = super().get_route_handler()
        declared = self.declared

        async def strictly(request: Request) -> Response:
            reads = declared.reads
            errors = [*strict.path_errors(reads, request.path_params), *strict.query_errors(reads, request.query_params.multi_items())]
            given = request
            if reads.body is not None and (raw := await request.body()):
                value, wrong = strict.body(reads.body, raw, request.headers.get("content-type"))
                errors.extend(wrong)
                given = _Read(request, raw, value)
            if errors:
                raise RequestValidationError(errors)
            try:
                return await handle(given)
            except Error as error:
                if not isinstance(error, declared.answers):
                    msg = f"{declared.name} raised {type(error).__qualname__}, which its route does not declare: add it to the route's errors=[...], or raise one it declares"
                    raise UndeclaredError(msg) from error
                raise
            except HTTPException as error:
                msg = f"{declared.name} raised an HTTPException {error.status_code}, which no route declares: raise an api.Error subclass, and declare it in the route's errors=[...]"
                raise UndeclaredError(msg) from error

        return strictly


def route_class(declared: Route) -> type[APIRoute]:
    """The FastAPI route class for `declared`."""

    class Declared(Served):
        pass

    Declared.declared = declared
    Declared.__qualname__ = Declared.__name__ = f"{declared.name}_route"
    return Declared


def responses(declared: Route) -> dict[int | str, dict[str, object]]:
    """The errors `declared` answers with, by status, as FastAPI's `responses` lists them: each a problem, which the
    description names by its type and title."""
    by_status: dict[int, list[str]] = {HTTPStatus.UNPROCESSABLE_CONTENT: [_INVALID]}
    for error in declared.answers:
        by_status.setdefault(error.status, []).append(f"{error.type}: {error.title}")
    return {status: {"model": Problem, "description": "; ".join(kinds)} for status, kinds in sorted(by_status.items())}


class Application(FastAPI):
    """FastAPI, whose OpenAPI document gives every error response as `application/problem+json`, the media type the app
    answers errors with; FastAPI puts a response's model under `application/json`."""

    _document: dict[str, JsonValue] | None = None

    @override
    def openapi(self) -> dict[str, JsonValue]:
        if self._document is None:
            document = _DOCUMENT.validate_python(super().openapi())
            for responses in _operations(document):
                for status, response in responses.items():
                    content = response.get("content") if isinstance(response, dict) else None
                    if status.isdigit() and int(status) >= HTTPStatus.BAD_REQUEST and isinstance(content, dict) and "application/json" in content:
                        content[MEDIA_TYPE] = content.pop("application/json")
            self._document = self.openapi_schema = document
        return self._document


_DOCUMENT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def _operations(document: dict[str, JsonValue]) -> Iterator[dict[str, JsonValue]]:
    """Each operation's responses in `document`."""
    paths = document.get("paths")
    for item in paths.values() if isinstance(paths, dict) else ():
        for operation in item.values() if isinstance(item, dict) else ():
            if isinstance(operation, dict) and isinstance(answers := operation.get("responses"), dict):
                yield answers
