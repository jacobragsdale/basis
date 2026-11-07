"""Every error an API returns is an RFC 9457 problem details response, `application/problem+json`, made by one
exception map, `problem`. A handler raises an `Error` subclass it declares, with a status and a title:

    class PeriodReversed(api.Error):
        status = HTTPStatus.UNPROCESSABLE_CONTENT
        title = "The period ends before it starts"

    raise PeriodReversed(f"{end} is before {start}")

and the response says so, with the message as its `detail`:

    {"type": "period-reversed", "title": "The period ends before it starts", "status": 422,
     "detail": "2026-01-01 is before 2026-06-01", "instance": "urn:uuid:019a..."}

`type` is the class's name in kebab case, less any `Error` suffix, unless it declares its own URI. `instance` is the
request's id, which every log line of the request carries too. Errors that share a base the service declares mark it
abstract, and only the classes it raises declare a status and a title:

    class PricingError(api.Error, abstract=True): ...

    class Unpriced(PricingError):
        status = HTTPStatus.NOT_FOUND
        title = "No price for the security"

A request that does not validate gets `invalid-request` with an `errors` list, one entry per field. An error FastAPI or
Starlette raise, such as no route (404) or the wrong method (405), gets `about:blank` with the status's own phrase as
its title, and an `HTTPException` whose detail is not a sentence, such as a dict, carries it as `data`. Any other
exception gets a bare 500, which says nothing of what failed, while the log says everything."""

import re
from collections.abc import Mapping, Sequence
from http import HTTPStatus
from types import MappingProxyType
from typing import ClassVar
from uuid import UUID

from fastapi.exceptions import RequestValidationError
from pydantic import JsonValue, TypeAdapter, ValidationError
from starlette.exceptions import HTTPException
from starlette.responses import Response

from basis.core.model import Model

MEDIA_TYPE = "application/problem+json"
FIRST_ERROR = 400
FIRST_SERVER_ERROR = 500
_STATUSES = range(FIRST_ERROR, 600)
_NO_HEADERS: Mapping[str, str] = MappingProxyType({})
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class Error(Exception):
    """An error a handler raises on purpose. Each subclass declares `status`, a 4xx or 5xx, and `title`, which says
    what kind of problem it is in the same words for every occurrence, or inherits them; the message given when it is
    raised says what happened this time, and is shown to the caller, so it never holds a secret. A subclass declared
    with `abstract=True` is a base for others, declares neither, and is never raised itself, as `Error` is not."""

    _abstract: ClassVar[bool] = True
    status: ClassVar[int]
    title: ClassVar[str]
    type: ClassVar[str]
    """A URI for the kind of problem, by default the class's name in kebab case without an `Error` suffix, such as
    `period-reversed` for `PeriodReversed` or `PeriodReversedError`."""

    def __init_subclass__(cls, *, abstract: bool = False) -> None:
        super().__init_subclass__()
        cls._abstract = abstract
        if abstract:
            return
        status = getattr(cls, "status", None)
        if not isinstance(status, int) or status not in _STATUSES:
            msg = f"{cls.__qualname__} declares no error status: give it `status = HTTPStatus.NOT_FOUND`, or another 4xx or 5xx, or declare it `abstract=True`, a base for errors that do"
            raise TypeError(msg)
        title = getattr(cls, "title", None)
        if not isinstance(title, str) or not title.strip():
            msg = f'{cls.__qualname__} declares no title: give it `title = "..."`, what kind of problem it is, in the same words every time'
            raise TypeError(msg)
        if "type" not in vars(cls):
            cls.type = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "-", cls.__name__.removesuffix("Error") or cls.__name__).lower()

    def __init__(self, detail: str) -> None:
        if self._abstract:
            msg = f"{type(self).__qualname__} is a base for errors, and is never raised itself: raise one of its subclasses, which declare a status and a title"
            raise TypeError(msg)
        super().__init__(detail)
        self.detail = detail


class FieldError(Model):
    location: str
    """Where in the request the field is: `body`, `query`, `path`, `header` or `cookie`."""
    pointer: str
    """A JSON Pointer to the field within its location, such as `/end` in the query or `/legs/0/rate` in the body;
    empty for the location as a whole."""
    detail: str


class Problem(Model):
    """RFC 9457 problem details, with `errors` for a request that does not validate."""

    type: str
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    """`urn:uuid:` and the request's id, which every log line of the request carries."""
    errors: tuple[FieldError, ...] | None = None
    data: JsonValue | None = None
    """The detail of an `HTTPException` that is not a sentence, such as a dict, as it was given."""


class NotReadyError(Error):
    status = HTTPStatus.SERVICE_UNAVAILABLE
    title = "The service is not ready"


def problem(error: Exception, request_id: UUID | None) -> tuple[Problem, Mapping[str, str]]:
    """The one exception map: the problem details for `error`, with the headers its response needs, such as `Allow`."""
    instance = request_id.urn if request_id is not None else None
    match error:
        case Error():
            return Problem(type=error.type, title=error.title, status=error.status, detail=error.detail, instance=instance), _NO_HEADERS
        case RequestValidationError():
            return Problem(type="invalid-request", title="The request is not valid", status=HTTPStatus.UNPROCESSABLE_CONTENT, instance=instance, errors=_field_errors(error.errors())), _NO_HEADERS
        case HTTPException():
            phrase = _phrase(error.status_code)
            detail, data = _http_detail(error.detail, phrase)
            return Problem(type="about:blank", title=phrase, status=error.status_code, detail=detail, instance=instance, data=data), error.headers or _NO_HEADERS
        case _:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            return Problem(type="about:blank", title=status.phrase, status=status, instance=instance), _NO_HEADERS


def response(error: Exception, request_id: UUID | None) -> Response:
    """`error` as the response the caller gets."""
    found, headers = problem(error, request_id)
    return Response(found.model_dump_json(exclude_none=True), status_code=found.status, headers=headers, media_type=MEDIA_TYPE)


def _field_errors(errors: Sequence[object]) -> tuple[FieldError, ...]:
    """FastAPI's validation errors, each as its location, a pointer and pydantic's message, without the input, which
    can hold what the caller should not see echoed, such as a password."""
    return tuple(_field_error(entry) for entry in errors)


def _field_error(entry: object) -> FieldError:
    if not isinstance(entry, Mapping):
        return FieldError(location="request", pointer="", detail=str(entry))
    location, message, kind, context = entry.get("loc"), entry.get("msg"), entry.get("type"), entry.get("ctx")
    parts = [str(part) for part in location] if isinstance(location, tuple) else []
    if kind == "json_invalid":  # the body is not JSON, and FastAPI puts where decoding stopped, a character, in `loc`
        where = f" at character {parts[1]}" if len(parts) > 1 else ""
        reason = context.get("error") if isinstance(context, Mapping) else None
        return FieldError(location="body", pointer="", detail=f"{message}{where}" + (f": {reason}" if reason is not None else ""))
    pointer = "".join(f"/{part.replace('~', '~0').replace('/', '~1')}" for part in parts[1:])
    return FieldError(location=parts[0] if parts else "request", pointer=pointer, detail=str(message))


def _http_detail(given: object, phrase: str) -> tuple[str | None, JsonValue | None]:
    """An `HTTPException`'s detail: a sentence as `detail`, unless it only repeats the title; anything else, such as the
    dict FastAPI allows, as `data` where it is JSON, and as text where it is not."""
    if given is None or given == phrase:
        return None, None
    if isinstance(given, str):
        return given, None
    try:
        return None, _JSON.validate_python(given)
    except ValidationError:
        return str(given), None


def _phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"
