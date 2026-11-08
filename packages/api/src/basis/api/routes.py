"""Each route as the app serves it, around FastAPI's own handling of the request:

- its deadline starts when it begins to serve the request, and caps every call the request makes through a resource or
  a compute function, the commit of its writes included (`deadlines.py`);
- it reads the request strictly first (`strict.py`): the path and the query by their types' grammars, and a JSON body
  in pydantic's strict mode, which FastAPI's handler then gets as the value basis made, and never validates again, so a
  validator runs once; a body sent to a route that declares none is refused;
- a domain's exception that the route declares the meaning of, with `api.error`, answers as that meaning, the
  most specific one the route declares for its class; any other is a defect, a 500;
- an `api.Error` the route does not declare, a kind of `api.ServiceError` that nothing it asks for raises, or an
  `HTTPException` raised inside it, answers 500 as a defect, logged once by the request boundary with what the route
  should have declared;
- a dataset route answers Arrow when the request asks for it (`arrow.py`), the rows validated as its response first;
- its operation in the OpenAPI document has its handler's name as its id, and lists each error the route answers with
  under its status, a 422 as a `ValidationProblem` and any other as a `Problem`, and a dataset route's Arrow beside its
  JSON (`openapi.py` finishes the document)."""

import asyncio
import functools
import inspect
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from dataclasses import dataclass
from http import HTTPStatus
from typing import ClassVar, override

from fastapi import FastAPI
from fastapi._compat import ModelField  # the field of a parameter, whose `validate` is where FastAPI's handler validates a body
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from basis.api import arrow, deadlines, strict
from basis.api.dataset import Rows
from basis.api.errors import DomainError, Error, Problem, ValidationProblem, described
from basis.api.identity import SignInRequiredError
from basis.api.service import Route, Service
from basis.core.errors import ServiceError

_INVALID = "invalid-request: The request is not valid"
_ARROW: ContextVar[bool] = ContextVar("basis.api.routes.arrow", default=False)
"""Whether the request a dataset route is answering asks for Arrow."""


class UndeclaredError(RuntimeError):
    """A route that raised an error it does not declare: a defect, whose 500 says nothing of it to the caller."""


@dataclass(frozen=True)
class _Validated:
    """A body basis has read and validated, as FastAPI's handler gets it for its JSON."""

    value: object


class _Read(Request):
    """The request as FastAPI's handler gets it, once basis has read its body: the value it validated, as its JSON."""

    def __init__(self, request: Request, raw: bytes, value: object) -> None:
        super().__init__(request.scope, request.receive)
        self._raw = raw
        self._value = _Validated(value)

    @override
    async def body(self) -> bytes:
        return self._raw

    @override
    async def json(self) -> object:
        return self._value


class _ValidatedOnce(ModelField):
    """A route's body field, as FastAPI's handler validates it: the value basis validated passes as it is, so no
    validator of the body's runs twice. The OpenAPI document reads the field as FastAPI made it."""

    @override
    def validate(self, value: object, values: dict[str, object] | None = None, *, loc: tuple[int | str, ...] = ()) -> tuple[object, list[dict[str, object]]]:
        if isinstance(value, _Validated):
            return value.value, []
        validated, wrong = super().validate(value, values if values is not None else {}, loc=loc)
        found: object = validated
        errors: list[dict[str, object]] = [dict(entry) for entry in wrong]
        return found, errors


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
        dependant = self.dependant
        declared = self.declared
        if dependant.body_params and not isinstance(field := dependant.body_params[0], _ValidatedOnce):  # the handler's own, as strict.shape sees to
            once = _ValidatedOnce(field_info=field.field_info, name=field.name, mode=field.mode, config=field.config)
            dependant.body_params[0] = once
        if declared.rows is not None and dependant.call is declared.handler:  # the endpoint stays the handler, which `given` finds the route's writes by
            dependant.call = _answering_table(declared, declared.rows) if declared.table else _answering_arrow(declared, declared.rows)
        handle = super().get_route_handler()
        meanings: dict[type, type[DomainError]] = {error.exception: error for error in declared.errors if issubclass(error, DomainError)}

        async def strictly(request: Request) -> Response:
            reads = declared.reads
            errors = [*strict.path_errors(reads, request.path_params), *strict.query_errors(reads, request.query_params.multi_items())]
            given = request
            if reads.body is not None and (raw := await request.body()):
                value, wrong = strict.body(reads.body, raw, request.headers.get("content-type"))
                errors.extend(wrong)
                given = _Read(request, raw, value)
            elif reads.body is None and await _sent_a_body(request):
                errors.append(strict.NO_BODY)
            if errors:
                raise RequestValidationError(errors)
            token = _ARROW.set(declared.rows is not None and arrow.wanted(request.headers.get("accept")))
            try:
                response = await handle(given)
                if declared.rows is not None:
                    response.headers.add_vary_header("Accept")
                return response  # noqa: TRY300 -- what the route answers; the handlers below refuse what it raised
            except Error as error:
                if not isinstance(error, declared.answers):
                    fix = (
                        "ask for the user in its parameters, as Annotated[User | None, api.given(User)], which adds it to the route's errors"
                        if isinstance(error, SignInRequiredError)
                        else "add it to the route's errors=[...], or raise one it declares"
                    )
                    msg = f"{declared.name} raised {type(error).__qualname__}, which its route does not declare: {fix}"
                    raise UndeclaredError(msg) from error
                raise
            except ServiceError as error:
                if not isinstance(error, declared.answers):
                    msg = (
                        f"{declared.name} raised {type(error).__qualname__} of {error.resource}, which nothing its route asks for raises:"
                        " reach each resource through what the handler asks for with api.given, which adds the kinds it raises to the route's errors"
                    )
                    raise UndeclaredError(msg) from error
                raise
            except HTTPException as error:
                msg = f"{declared.name} raised an HTTPException {error.status_code}, which no route declares: raise an api.Error subclass, and declare it in the route's errors=[...]"
                raise UndeclaredError(msg) from error
            except Exception as error:
                meant = next((meanings[kind] for kind in type(error).__mro__ if kind in meanings), None)
                if meant is None:
                    raise  # a defect, which the request boundary answers 500
                raise meant(str(error) or meant.title) from error
            finally:
                _ARROW.reset(token)

        return strictly


def _answering_arrow(declared: Route, rows: Rows) -> Callable[..., object]:
    """`declared`'s handler, as FastAPI calls it, which answers the request's Arrow when it asks for it: the rows
    validated as the route's response, then written as a stream, in a thread when the handler is async."""
    handler = declared.handler

    def answer(found: object) -> Response:
        values = rows.adapter.validate_python(found, from_attributes=True)  # as FastAPI validates a JSON response
        dumped = rows.adapter.dump_python(values, by_alias=True)
        return Response(arrow.encode(rows, dumped), status_code=declared.status, media_type=arrow.MEDIA_TYPE)

    if inspect.iscoroutinefunction(handler):

        @functools.wraps(handler)
        async def answer_async(**values: object) -> object:
            found = await handler(**values)
            return await asyncio.to_thread(answer, found) if _ARROW.get() else found

        return answer_async

    @functools.wraps(handler)
    def answer_sync(**values: object) -> object:
        found = handler(**values)
        return answer(found) if _ARROW.get() else found

    return answer_sync


def _answering_table(declared: Route, rows: Rows) -> Callable[..., object]:
    """`declared`'s handler, as FastAPI calls it, for a route that answers an `api.Table[Row]`: the frame or table it
    returns checked once against the rows' columns, then answered as Arrow, or as JSON written from the Arrow, in a
    thread when the handler is async. A table that does not hold the columns raises, a defect, so the request's writes
    roll back."""
    handler = declared.handler
    media = {True: arrow.MEDIA_TYPE, False: "application/json"}

    def answer(found: object) -> Response:
        table = arrow.conform(rows, found, f"the table {declared.name} returned")
        wanted = _ARROW.get()
        return Response(arrow.write(table) if wanted else arrow.as_json(table), status_code=declared.status, media_type=media[wanted])

    if inspect.iscoroutinefunction(handler):

        @functools.wraps(handler)
        async def answer_async(**values: object) -> object:
            found = await handler(**values)
            return await asyncio.to_thread(answer, found)

        return answer_async

    @functools.wraps(handler)
    def answer_sync(**values: object) -> object:
        return answer(handler(**values))

    return answer_sync


async def _sent_a_body(request: Request) -> bool:
    """Whether the request has a body, read only as far as its first byte."""
    async for chunk in request.stream():
        if chunk:
            return True
    return False


def route_class(declared: Route) -> type[APIRoute]:
    """The FastAPI route class for `declared`."""

    class Declared(Served):
        pass

    Declared.declared = declared
    Declared.__qualname__ = Declared.__name__ = f"{declared.name}_route"
    return Declared


def responses(declared: Route) -> dict[int | str, dict[str, object]]:
    """What `declared` answers with, by status, as FastAPI's `responses` lists them: each error a problem, which the
    description names by its type and title, a 422 a `ValidationProblem`; and a dataset's rows as Arrow beside its
    JSON."""
    by_status: dict[int, list[str]] = {HTTPStatus.UNPROCESSABLE_CONTENT: [_INVALID]}
    for error in declared.answers:
        status, kind, title = described(error)
        by_status.setdefault(status, []).append(f"{kind}: {title}")
    listed: dict[int | str, dict[str, object]] = {
        status: {"model": ValidationProblem if status == HTTPStatus.UNPROCESSABLE_CONTENT else Problem, "description": "; ".join(kinds)} for status, kinds in sorted(by_status.items())
    }
    if declared.rows is not None:
        content = {arrow.MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}}
        listed[declared.status] = {"description": "The rows, as JSON, or as an Arrow IPC stream when the request's Accept asks for it", "content": content}
    return listed


API = "/api"
"""Where an app that serves a UI mounts its routes."""


def add(app: FastAPI, service: Service, *, prefix: str = "") -> None:
    """Add `service`'s routes to `app`, each as `Served`, its operation's id its handler's name, each path after
    `prefix`: `API` in an app that serves a UI, and nothing in its document."""
    for route in service.routes:
        app.router.add_api_route(
            prefix + route.path, route.handler, methods=[route.method], status_code=route.status, name=route.name, response_model=documented(route),
            responses=responses(route), route_class_override=route_class(route), generate_unique_id_function=_operation_id,
        )  # fmt: skip


def documented(route: Route) -> object:
    """The response FastAPI documents for `route`: its rows' list for a route that answers an `api.Table`, whose JSON
    they are, and its response for any other."""
    return list[route.rows.model] if route.table and route.rows is not None else route.response  # ty: ignore[invalid-type-form] -- the row model, a value


def _operation_id(route: APIRoute) -> str:
    return route.name
