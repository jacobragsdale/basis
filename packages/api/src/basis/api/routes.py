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
import logging
from collections.abc import Callable, Coroutine, Mapping
from contextvars import ContextVar
from http import HTTPStatus
from typing import ClassVar, override

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import TypeAdapter
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from basis.api import arrow, deadlines, idempotent, outboxes, streams, strict
from basis.api.dataset import Rows
from basis.api.dependencies import running
from basis.api.errors import (
    BodyTooLargeError,
    DomainError,
    Error,
    IdempotencyKeyInUseError,
    IdempotencyKeyMissingError,
    IdempotencyKeyReusedError,
    ParameterError,
    Problem,
    ValidationProblem,
    described,
)
from basis.api.identity import USER, SignInRequiredError, User
from basis.api.internals import Validated, replace_call, validate_body_once
from basis.api.service import Route, Service
from basis.api.sessions import DatabaseSession
from basis.core.errors import Conflict, ServiceError
from basis.core.opened import utc_now

log = logging.getLogger(__name__)
_INVALID = "invalid-request: The request is not valid"
_ARROW: ContextVar[bool] = ContextVar("basis.api.routes.arrow", default=False)
"""Whether the request a dataset route is answering asks for Arrow."""
_SERVING: ContextVar[Request] = ContextVar("basis.api.routes.serving")
_PRINTED: ContextVar[str] = ContextVar("basis.api.routes.printed", default="")
"""The fingerprint of the request a route that keeps its `Idempotency-Key` is answering: its method, path, query and
body as they were sent."""
_INVALID_KEY: strict.ErrorEntry = {
    "type": "value_error", "loc": ("header", idempotent.HEADER), "msg": "An Idempotency-Key is one to 255 visible ASCII characters: send one, as the id of what the request makes",
    "input": None,
}  # fmt: skip
"""The request the route is answering, which a stream reads its app and its token's expiry from."""


class UndeclaredError(RuntimeError):
    """A route that raised an error it does not declare: a defect, whose 500 says nothing of it to the caller."""


class _Read(Request):
    """The request as FastAPI's handler gets it, once basis has read its body: the value it validated, as its JSON."""

    def __init__(self, request: Request, raw: bytes, value: object) -> None:
        super().__init__(request.scope, request.receive)
        self._raw = raw
        self._value = Validated(value)

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
        if declared.every_s is not None:  # a stream, each of whose calls has the deadline (`streams.py`)
            await super().handle(scope, receive, send)
            return
        with deadlines.route(declared.name, declared.deadline_s):
            await super().handle(scope, receive, send)

    @override
    def get_route_handler(self) -> Callable[[Request], Coroutine[object, object, Response]]:
        dependant = self.dependant
        declared = self.declared
        validate_body_once(dependant)
        meanings: dict[type, type[DomainError]] = {error.exception: error for error in declared.errors if issubclass(error, DomainError)}
        # the endpoint stays the handler, which `given` finds the route's writes by
        if declared.every_s is not None:
            replace_call(dependant, declared.handler, _opening_stream(declared, meanings))
        elif declared.rows is not None:
            replace_call(dependant, declared.handler, _answering_table(declared, declared.rows) if declared.table else _answering_arrow(declared, declared.rows))
        elif declared.idempotency is not None:
            replace_call(dependant, declared.handler, _keeping(declared, _refusing_responses(declared)))
        else:
            replace_call(dependant, declared.handler, _refusing_responses(declared))
        handle = super().get_route_handler()

        async def strictly(request: Request) -> Response:
            reads = declared.reads
            errors = [*strict.path_errors(reads, request.path_params), *strict.query_errors(reads, request.query_params.multi_items())]
            given = request
            raw = b""
            if declared.idempotency is not None and (key := request.headers.get(idempotent.HEADER)) is not None and not idempotent.valid(key):
                errors.append(_INVALID_KEY)
            if reads.body is not None and (raw := await _body(request, running(request).settings.serve.max_body_bytes)):
                kind = request.headers.get("content-type")
                value, wrong = await asyncio.to_thread(strict.body, reads.body, raw, kind) if len(raw) > OFF_THE_LOOP else strict.body(reads.body, raw, kind)
                errors.extend(wrong)
                given = _Read(request, raw, value)
            elif reads.body is None and await _sent_a_body(request):
                errors.append(strict.NO_BODY)
            if errors:
                raise RequestValidationError(errors)
            token = _ARROW.set(declared.rows is not None and arrow.wanted(request.headers.get("accept")))
            serving = _SERVING.set(given)
            printed = _PRINTED.set(idempotent.fingerprint(request.method, request.url.path, request.url.query, raw) if declared.idempotency is not None else "")
            try:
                response = await handle(given)
                if declared.rows is not None:
                    response.headers.add_vary_header("Accept")
                return response  # noqa: TRY300 -- what the route answers; the handlers below refuse what it raised
            except Exception as error:
                if (found := answered(declared, meanings, error)) is error:
                    raise  # what the route answers, or a defect, which the request boundary answers 500
                raise found from error
            finally:
                _PRINTED.reset(printed)
                _SERVING.reset(serving)
                _ARROW.reset(token)

        return strictly


def answered(declared: Route, meanings: Mapping[type, type[DomainError]], error: Exception) -> Exception:
    """What `declared` answers for `error`, which its handler raised: the error itself where the route declares it, or
    a kind of service error it can meet; the meaning it declares for a domain's exception, by the exception's class;
    and `UndeclaredError`, a defect, for an `api.Error`, a kind or an `HTTPException` it does not declare. Any other
    exception is a defect, and itself."""
    match error:
        case Error() if not isinstance(error, declared.answers):
            fix = (
                "ask for the user in its parameters, as Annotated[User | None, api.given(User)], which adds it to the route's errors"
                if isinstance(error, SignInRequiredError)
                else "add it to the route's errors=[...], or raise one it declares"
            )
            undeclared = UndeclaredError(f"{declared.name} raised {type(error).__qualname__}, which its route does not declare: {fix}")
        case ServiceError() if not isinstance(error, declared.answers):
            undeclared = UndeclaredError(
                f"{declared.name} raised {type(error).__qualname__} of {error.resource}, which nothing its route asks for raises:"
                " reach each resource through what the handler asks for with api.given, which adds the kinds it raises to the route's errors"
            )
        case Error() | ServiceError():
            return error
        case HTTPException():
            undeclared = UndeclaredError(
                f"{declared.name} raised an HTTPException {error.status_code}, which no route declares: raise an api.Error subclass, and declare it in the route's errors=[...]"
            )
        case _:
            meant = next((meanings[kind] for kind in type(error).__mro__ if kind in meanings), None)
            if meant is None:
                return error
            answer = meant(str(error) or meant.title)
            answer.__cause__ = error
            return answer
    undeclared.__cause__ = error
    return undeclared


def _refused(declared: Route, found: object) -> object:
    """`found`, which `declared`'s handler returned, unless it is a `Response`, which nothing would check: a defect,
    raised before the request's writes commit, so they roll back."""
    if isinstance(found, Response):
        msg = f"{declared.name} returned a {type(found).__qualname__}, which basis cannot check: return the {strict.named(declared.response)} its route declares"
        raise UndeclaredError(msg)
    return found


def _refusing_responses(declared: Route) -> Callable[..., object]:
    """`declared`'s handler, as FastAPI calls it, refusing a `Response` it returns."""
    handler = declared.handler
    if inspect.iscoroutinefunction(handler):

        @functools.wraps(handler)
        async def answer_async(**values: object) -> object:
            return _refused(declared, await handler(**values))

        return answer_async

    @functools.wraps(handler)
    def answer_sync(**values: object) -> object:
        return _refused(declared, handler(**values))

    return answer_sync


def _keeping(declared: Route, inner: Callable[..., object]) -> Callable[..., object]:
    """`declared`'s handler, `inner`, as FastAPI calls it, for a route that keeps its requests' `Idempotency-Key`s in
    the database it writes (`idempotent.py`): a request with a key that one made before answers as that one did,
    without running the handler, or is refused where it is another request; any other runs the handler, and its answer
    is kept with its key, in its transaction."""
    parameter = next(ask.parameter for ask in declared.asks if ask.given == declared.writes)
    adapter: TypeAdapter[object] | None = TypeAdapter(declared.response) if declared.response is not None else None

    @functools.wraps(declared.handler)
    async def keep(**values: object) -> object:
        request = _SERVING.get()
        key = request.headers.get(idempotent.HEADER)
        if key is None:
            if declared.idempotency == "required":
                msg = f"{declared.name} needs an Idempotency-Key: send one, the same each time the request is sent again"
                raise IdempotencyKeyMissingError(msg)
            return await inner(**values)  # ty: ignore[invalid-await] -- the handler of a route that writes, an async def
        session = values[parameter]
        if not isinstance(session, DatabaseSession):  # pragma: no cover -- what `given` gives a route that writes
            msg = f"{declared.name}'s {parameter} is a {type(session).__qualname__}, not the request's transaction"
            raise TypeError(msg)
        user = request.scope.get(USER)
        subject = user.subject if isinstance(user, User) else None
        key_id, printed = idempotent.ident(declared.name, subject, key), _PRINTED.get()

        def kept() -> idempotent.Kept:
            return idempotent.Kept(
                id=key_id, idempotency_key=key, route=declared.name[: outboxes.ROUTE_LENGTH], subject=subject, fingerprint=printed, created_at=utc_now(),
                status_code=None, body=None,
            )  # fmt: skip

        try:
            found = await session.claim(key_id, kept)
        except Conflict as busy:
            msg = "a request with this Idempotency-Key has not finished: send it again once it has, and it answers as that one did"
            raise IdempotencyKeyInUseError(msg) from busy
        if found is not None and found.status_code is None:  # pragma: no cover -- a key commits only with its answer
            msg = "a request with this Idempotency-Key has not finished: send it again once it has, and it answers as that one did"
            raise IdempotencyKeyInUseError(msg)
        if found is not None and found.fingerprint != printed:
            detail = "was sent with another request: send a new key with a new request, and this one only with the request it was sent with"
            msg = f"This Idempotency-Key {detail}"
            raise IdempotencyKeyReusedError(msg, errors=[ParameterError(detail=detail, parameter=idempotent.HEADER)])
        if found is not None:
            log.info("%s answers as the request its Idempotency-Key was first sent with", declared.name, extra={"route": declared.name, "replayed": True})
            return Response(found.body, status_code=int(found.status_code or HTTPStatus.OK), media_type="application/json" if found.body is not None else None)
        answer = await inner(**values)  # ty: ignore[invalid-await] -- the handler of a route that writes, an async def
        body = None if adapter is None else adapter.dump_json(adapter.validate_python(answer, from_attributes=True), by_alias=True).decode()
        await session.answered(key_id, declared.status, body)
        return answer

    return keep


def _opening_stream(declared: Route, meanings: Mapping[type, type[DomainError]]) -> Callable[..., object]:
    """`declared`'s handler, as FastAPI calls it, for a stream of live updates: its first call, which answers as any
    route's does when it fails, and then the stream, which calls it again (`streams.py`)."""

    @functools.wraps(declared.handler)
    async def open_stream(**values: object) -> Response:
        stream = streams.Stream.opened(declared, _SERVING.get(), values, functools.partial(answered, declared, meanings))
        first = await stream.call()
        return streams.Events(stream.events(first), send_s=stream.runtime.settings.serve.heartbeat_s)

    return open_stream


def _answering_arrow(declared: Route, rows: Rows) -> Callable[..., object]:
    """`declared`'s handler, as FastAPI calls it, which answers the request's Arrow when it asks for it: the rows
    validated as the route's response, then written as a stream, in a thread when the handler is async."""
    handler = declared.handler

    def answer(found: object) -> Response:
        values = rows.adapter.validate_python(_refused(declared, found), from_attributes=True)  # as FastAPI validates a JSON response
        dumped = rows.adapter.dump_python(values, by_alias=True)
        return Response(arrow.encode(rows, dumped), status_code=declared.status, media_type=arrow.MEDIA_TYPE)

    if inspect.iscoroutinefunction(handler):

        @functools.wraps(handler)
        async def answer_async(**values: object) -> object:
            found = _refused(declared, await handler(**values))
            return await asyncio.to_thread(answer, found) if _ARROW.get() else found

        return answer_async

    @functools.wraps(handler)
    def answer_sync(**values: object) -> object:
        found = _refused(declared, handler(**values))
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
        table = arrow.conform(rows, _refused(declared, found), f"the table {declared.name} returned")
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


OFF_THE_LOOP = 64 * 1024
"""The size of body, in bytes, above which it is parsed and validated in a thread, so that other requests are served
meanwhile: about a millisecond's work, below which a thread costs more than it saves."""


async def _body(request: Request, limit: int) -> bytes:
    """The request's body, refused once it is larger than `limit`: at once by its `Content-Length`, or when that many
    bytes have arrived, so no more than that is ever held."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise _too_large(limit)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise _too_large(limit)
        chunks.append(chunk)
    return b"".join(chunks)


def _too_large(limit: int) -> BodyTooLargeError:
    return BodyTooLargeError(f"The body is over {limit} bytes, the most this API takes: send a smaller one")


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
    if declared.every_s is not None:
        content = {streams.MEDIA_TYPE: {"schema": {"type": "string"}}}
        listed[declared.status] = {"description": "A stream of server-sent events, each event's data one of the route's models, as x-basis-event gives it", "content": content}
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
