"""Every error an API returns is an RFC 9457 problem details response, `application/problem+json`, made by one
exception map, `problem`. A handler raises an `Error` subclass it declares, with a status and a title:

    class PeriodReversed(api.Error):
        status = HTTPStatus.UNPROCESSABLE_CONTENT
        title = "The period ends before it starts"

    raise PeriodReversed(f"{end} is before {start}")

and the response says so, with the message as its `detail`:

    {"type": "period-reversed", "title": "The period ends before it starts", "status": 422,
     "detail": "2026-01-01 is before 2026-06-01", "instance": "urn:uuid:019a..."}

`type` is the class's name in kebab case, less any `Error` suffix, unless it declares its own URI. `instance` is the request's id, which every
log line of the request carries too. A request that does not validate gets `invalid-request` with an `errors` list, one
entry per field; an error FastAPI or Starlette raise, such as no route (404) or the wrong method (405), gets
`about:blank` with the status's own phrase as its title; and any other exception gets a bare 500, which says nothing
of what failed, while the log says everything."""

import re
from collections.abc import Mapping, Sequence
from http import HTTPStatus
from types import MappingProxyType
from typing import ClassVar
from uuid import UUID

from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from starlette.responses import Response

from basis.core.model import Model

MEDIA_TYPE = "application/problem+json"
FIRST_ERROR = 400
FIRST_SERVER_ERROR = 500
_STATUSES = range(FIRST_ERROR, 600)
_NO_HEADERS: Mapping[str, str] = MappingProxyType({})


class Error(Exception):
    """An error a handler raises on purpose. Each subclass declares `status`, a 4xx or 5xx, and `title`, which says
    what kind of problem it is in the same words for every occurrence; the message given when it is raised says what
    happened this time, and is shown to the caller, so it never holds a secret."""

    status: ClassVar[int]
    title: ClassVar[str]
    type: ClassVar[str]
    """A URI for the kind of problem, by default the class's name in kebab case without an `Error` suffix, such as
    `period-reversed` for `PeriodReversed` or `PeriodReversedError`."""

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        status = getattr(cls, "status", None)
        if not isinstance(status, int) or status not in _STATUSES:
            msg = f"{cls.__qualname__} declares no error status: give it `status = HTTPStatus.NOT_FOUND`, or another 4xx or 5xx"
            raise TypeError(msg)
        title = getattr(cls, "title", None)
        if not isinstance(title, str) or not title.strip():
            msg = f'{cls.__qualname__} declares no title: give it `title = "..."`, what kind of problem it is, in the same words every time'
            raise TypeError(msg)
        if "type" not in vars(cls):
            cls.type = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "-", cls.__name__.removesuffix("Error") or cls.__name__).lower()

    def __init__(self, detail: str) -> None:
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
            detail = error.detail if error.detail != phrase else None
            return Problem(type="about:blank", title=phrase, status=error.status_code, detail=detail, instance=instance), error.headers or _NO_HEADERS
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
    found: list[FieldError] = []
    for entry in errors:
        location, message = (entry.get("loc"), entry.get("msg")) if isinstance(entry, Mapping) else ((), entry)
        parts = [str(part) for part in location] if isinstance(location, tuple) else []
        pointer = "".join(f"/{part.replace('~', '~0').replace('/', '~1')}" for part in parts[1:])
        found.append(FieldError(location=parts[0] if parts else "request", pointer=pointer, detail=str(message)))
    return tuple(found)


def _phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"
