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

Every 422 is a `ValidationProblem`, whose `errors` name each field at fault, as RFC 9457's own example does, so a UI
puts each error on its field: a field of the body by an RFC 6901 JSON Pointer in URI fragment form, `#/legs/0/rate`,
where `#` is the whole body, and a query, path or header parameter by its name.

    {"type": "invalid-request", "title": "The request is not valid", "status": 422,
     "errors": [{"detail": "Field required", "parameter": "end"},
                {"detail": "Input should be greater than 0", "pointer": "#/legs/0/face"}]}

A request that does not validate gets `invalid-request`, one entry per field. A handler's own 422 has the same shape,
and names the fields it refuses, if any, when it is raised:

    raise PeriodReversed(f"{end} is before {start}", errors=[api.ParameterError(detail="ends before start", parameter="end")])
    raise BookClosed(f"{book} is closed", errors=[api.PointerError(detail="closed", pointer=api.pointer("book"))])

An error FastAPI or Starlette raise, such as no route (404) or the wrong method (405), gets `about:blank` with the
status's own phrase as its title, and an `HTTPException` whose detail is not a sentence, such as a dict, carries it as
`data`. Any other exception gets a bare 500, which says nothing of what failed, while the log says everything."""

import re
from collections.abc import Mapping, Sequence
from http import HTTPStatus
from types import MappingProxyType
from typing import ClassVar
from urllib.parse import quote
from uuid import UUID

from fastapi.exceptions import RequestValidationError
from pydantic import JsonValue, TypeAdapter, ValidationError
from starlette.exceptions import HTTPException
from starlette.responses import Response

from basis.core.errors import BasisError
from basis.core.model import Model

MEDIA_TYPE = "application/problem+json"
FIRST_ERROR = 400
FIRST_SERVER_ERROR = 500
_STATUSES = range(FIRST_ERROR, 600)
_NO_HEADERS: Mapping[str, str] = MappingProxyType({})
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class ServiceError(BasisError, TypeError):
    """A service that does not declare what basis needs; the message says what to declare, and `basis` prints it as
    one line."""


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
    headers: ClassVar[Mapping[str, str]] = _NO_HEADERS
    """Headers its every response carries, such as a 401's `WWW-Authenticate` challenge, which RFC 9110 requires."""

    def __init_subclass__(cls, *, abstract: bool = False) -> None:
        super().__init_subclass__()
        cls._abstract = abstract
        if abstract:
            return
        status = getattr(cls, "status", None)
        if not isinstance(status, int) or status not in _STATUSES:
            msg = f"{cls.__qualname__} declares no error status: give it `status = HTTPStatus.NOT_FOUND`, or another 4xx or 5xx, or declare it `abstract=True`, a base for errors that do"
            raise ServiceError(msg)
        title = getattr(cls, "title", None)
        if not isinstance(title, str) or not title.strip():
            msg = f'{cls.__qualname__} declares no title: give it `title = "..."`, what kind of problem it is, in the same words every time'
            raise ServiceError(msg)
        if status == HTTPStatus.UNAUTHORIZED and "www-authenticate" not in {name.lower() for name in cls.headers}:
            msg = f'{cls.__qualname__} is a 401, whose response carries a challenge: raise api.SignInRequiredError instead, or give it `headers = MappingProxyType({{"WWW-Authenticate": "Bearer"}})`'
            raise ServiceError(msg)
        if "type" not in vars(cls):
            cls.type = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "-", cls.__name__.removesuffix("Error") or cls.__name__).lower()

    def __init__(self, detail: str, *, errors: Sequence[FieldError] = ()) -> None:
        if self._abstract:
            msg = f"{type(self).__qualname__} is a base for errors, and is never raised itself: raise one of its subclasses, which declare a status and a title"
            raise TypeError(msg)
        if errors and self.status != HTTPStatus.UNPROCESSABLE_CONTENT:
            msg = f"{type(self).__qualname__} is a {self.status}, and only a 422 names the fields it refuses: give its errors in the detail instead"
            raise TypeError(msg)
        super().__init__(detail)
        self.detail = detail
        self.errors: tuple[FieldError, ...] = tuple(errors)


class PointerError(Model):
    """A field of the request's body at fault."""

    detail: str
    pointer: str
    """An RFC 6901 JSON Pointer to the field in the body, in URI fragment form, as `#/legs/0/rate`; `#` is the whole
    body. `pointer` makes one."""


class ParameterError(Model):
    """A query, path or header parameter at fault."""

    detail: str
    parameter: str
    """The parameter's name, as the request gives it."""


type FieldError = PointerError | ParameterError


class Problem(Model):
    """RFC 9457 problem details."""

    type: str
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    """`urn:uuid:` and the request's id, which every log line of the request carries."""
    data: JsonValue | None = None
    """The detail of an `HTTPException` that is not a sentence, such as a dict, as it was given."""


class ValidationProblem(Problem):
    """RFC 9457 problem details for a 422, a request basis or the handler refuses, which names each field at fault."""

    errors: tuple[PointerError | ParameterError, ...]
    """Each field at fault, which may be none: a 422 that names no field says why in its `detail`."""


def pointer(*path: str | int) -> str:
    """The JSON Pointer to the body's field at `path`, in URI fragment form: `pointer("legs", 0, "rate")` is
    `#/legs/0/rate`, and `pointer()` the whole body. A key's `~` and `/` are escaped as RFC 6901 says, and what a URI's
    fragment does not allow is percent-encoded."""
    return "#" + "".join("/" + quote(str(part).replace("~", "~0").replace("/", "~1"), safe=_FRAGMENT) for part in path)


_FRAGMENT = "~!$&'()*+,;=:@"
"""What RFC 3986 allows in a fragment as it is, besides letters, digits and `-._`."""


class NotReadyError(Error):
    status = HTTPStatus.SERVICE_UNAVAILABLE
    title = "The service is not ready"


def problem(error: Exception, request_id: UUID | None) -> tuple[Problem, Mapping[str, str]]:
    """The one exception map: the problem details for `error`, with the headers its response needs, such as `Allow`."""
    instance = request_id.urn if request_id is not None else None
    match error:
        case Error() if error.status == HTTPStatus.UNPROCESSABLE_CONTENT:
            return ValidationProblem(type=error.type, title=error.title, status=error.status, detail=error.detail, instance=instance, errors=error.errors), error.headers
        case Error():
            return Problem(type=error.type, title=error.title, status=error.status, detail=error.detail, instance=instance), error.headers
        case RequestValidationError():
            invalid = HTTPStatus.UNPROCESSABLE_CONTENT
            return ValidationProblem(type="invalid-request", title="The request is not valid", status=invalid, instance=instance, errors=_field_errors(error.errors())), _NO_HEADERS
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


_PARAMETERS = frozenset({"query", "path", "header", "cookie"})


def _field_errors(errors: Sequence[object]) -> tuple[FieldError, ...]:
    """FastAPI's validation errors, each as the field it names and pydantic's message, without the input, which can
    hold what the caller should not see echoed, such as a password."""
    return tuple(_field_error(entry) for entry in errors)


def _field_error(entry: object) -> FieldError:
    if not isinstance(entry, Mapping):
        return PointerError(detail=str(entry), pointer=pointer())
    location, message, kind, context = entry.get("loc"), entry.get("msg"), entry.get("type"), entry.get("ctx")
    parts: list[str | int] = [part if isinstance(part, int) else str(part) for part in location] if isinstance(location, tuple) else []
    if kind == "json_invalid":  # the body is not JSON, and FastAPI puts where decoding stopped, a character, in `loc`
        where = f" at character {parts[1]}" if len(parts) > 1 else ""
        reason = context.get("error") if isinstance(context, Mapping) else None
        return PointerError(detail=f"{message}{where}" + (f": {reason}" if reason is not None else ""), pointer=pointer())
    if parts and parts[0] in _PARAMETERS and len(parts) > 1:
        return ParameterError(detail=str(message), parameter=str(parts[1]))  # a list's index, after the name, is not the UI's to place
    return PointerError(detail=str(message), pointer=pointer(*parts[1:]))


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
