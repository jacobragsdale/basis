from http import HTTPStatus
from uuid import uuid7

import pytest
from fastapi import HTTPException as FastAPIHTTPException
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from basis import api
from basis.api.errors import problem

REQUEST = uuid7()


class Missing(api.Error):
    status = HTTPStatus.NOT_FOUND
    title = "No such position"


class Closed(api.Error):
    status = HTTPStatus.UNPROCESSABLE_CONTENT
    title = "The book is closed"


class Upstream(api.Error):
    status = HTTPStatus.BAD_GATEWAY
    title = "The pricing service failed"
    type = "https://problems.example/upstream"


@pytest.mark.parametrize(
    ("error", "expected", "headers"),
    [
        (Missing("position 7"), api.Problem(type="missing", title="No such position", status=404, detail="position 7", instance=REQUEST.urn), {}),
        (Upstream("timed out"), api.Problem(type="https://problems.example/upstream", title="The pricing service failed", status=502, detail="timed out", instance=REQUEST.urn), {}),
        (HTTPException(404), api.Problem(type="about:blank", title="Not Found", status=404, instance=REQUEST.urn), {}),
        (HTTPException(405, headers={"Allow": "GET"}), api.Problem(type="about:blank", title="Method Not Allowed", status=405, instance=REQUEST.urn), {"Allow": "GET"}),
        (HTTPException(400, detail="the cookie is malformed"), api.Problem(type="about:blank", title="Bad Request", status=400, detail="the cookie is malformed", instance=REQUEST.urn), {}),
        (FastAPIHTTPException(409, detail={"conflicts": ["T1"]}), api.Problem(type="about:blank", title="Conflict", status=409, instance=REQUEST.urn, data={"conflicts": ["T1"]}), {}),
        (FastAPIHTTPException(400, detail=["no side", "no quantity"]), api.Problem(type="about:blank", title="Bad Request", status=400, instance=REQUEST.urn, data=["no side", "no quantity"]), {}),
        (ValueError("password=hunter2 rejected"), api.Problem(type="about:blank", title="Internal Server Error", status=500, instance=REQUEST.urn), {}),
        (
            RequestValidationError(
                [
                    {"loc": ("query", "end"), "msg": "Field required", "type": "missing", "input": None},
                    {"loc": ("body", "legs", 0, "a/b~c"), "msg": "Input should be a valid decimal", "type": "decimal_parsing", "input": "hunter2"},
                    {"loc": ("body", 12), "msg": "JSON decode error", "type": "json_invalid", "input": {}, "ctx": {"error": "Expecting value"}},
                ]
            ),
            api.ValidationProblem(
                type="invalid-request",
                title="The request is not valid",
                status=422,
                instance=REQUEST.urn,
                errors=(
                    api.ParameterError(detail="Field required", parameter="end"),
                    api.PointerError(detail="Input should be a valid decimal", pointer="#/legs/0/a~1b~0c"),
                    api.PointerError(detail="JSON decode error at character 12: Expecting value", pointer="#"),
                ),
            ),
            {},
        ),
        (
            RequestValidationError(
                [
                    {"loc": ("path", "security"), "msg": "Input should be a ISIN", "type": "strict_text", "input": "x"},
                    {"loc": ("query", "symbol", 1), "msg": "Input should be text", "type": "strict_text", "input": "x"},
                    {"loc": ("header", "x-desk"), "msg": "Field required", "type": "missing", "input": None},
                    {"loc": ("body",), "msg": "This route takes no body: send the request without one", "type": "extra_forbidden", "input": None},
                    {"loc": ("body", "notes", "50% off", "#tag"), "msg": "Input should be text", "type": "string_type", "input": 1},
                ]
            ),
            api.ValidationProblem(
                type="invalid-request",
                title="The request is not valid",
                status=422,
                instance=REQUEST.urn,
                errors=(
                    api.ParameterError(detail="Input should be a ISIN", parameter="security"),
                    api.ParameterError(detail="Input should be text", parameter="symbol"),
                    api.ParameterError(detail="Field required", parameter="x-desk"),
                    api.PointerError(detail="This route takes no body: send the request without one", pointer="#"),
                    api.PointerError(detail="Input should be text", pointer="#/notes/50%25%20off/%23tag"),
                ),
            ),
            {},
        ),
        (
            Closed("the book ARCHIVE is closed", errors=[api.PointerError(detail="closed to new positions", pointer=api.pointer("book"))]),
            api.ValidationProblem(
                type="closed", title="The book is closed", status=422, detail="the book ARCHIVE is closed", instance=REQUEST.urn,
                errors=(api.PointerError(detail="closed to new positions", pointer="#/book"),),
            ),
            {},
        ),
        (
            Closed("the book ARCHIVE is closed"),
            api.ValidationProblem(type="closed", title="The book is closed", status=422, detail="the book ARCHIVE is closed", instance=REQUEST.urn, errors=()),
            {},
        ),
    ],
    ids=[
        "declared", "declared-with-its-own-type", "no-route", "wrong-method", "http-detail", "http-dict-detail", "http-list-detail", "unexpected", "invalid-request",
        "each-field-where-a-ui-places-it", "a-handlers-422-names-its-fields", "a-handlers-422-that-names-none",
    ],
)  # fmt: skip
def test_one_map_makes_every_error_a_problem(error: Exception, expected: api.Problem, headers: dict[str, str]) -> None:
    assert problem(error, REQUEST) == (expected, headers)


@pytest.mark.parametrize(("path", "pointer"), [((), "#"), (("legs", 0, "price"), "#/legs/0/price"), (("a/b", "c~d"), "#/a~1b/c~0d"), (("caf\u00e9 au lait",), "#/caf%C3%A9%20au%20lait")])
def test_a_pointer_is_rfc_6901_in_a_uris_fragment(path: tuple[str | int, ...], pointer: str) -> None:
    assert api.pointer(*path) == pointer


def test_only_a_422_names_the_fields_it_refuses() -> None:
    with pytest.raises(TypeError, match="only a 422 names the fields it refuses"):
        Missing("position 7", errors=[api.ParameterError(detail="unknown", parameter="position")])


@pytest.mark.parametrize(("name", "kind"), [("PositionNotFound", "position-not-found"), ("HTTPTimeout", "http-timeout"), ("NotReadyError", "not-ready"), ("Error", "error")])
def test_a_declared_errors_type_is_its_name_in_kebab_case(name: str, kind: str) -> None:
    declared = type(name, (api.Error,), {"status": HTTPStatus.CONFLICT, "title": "A conflict"})
    assert declared.type == kind


@pytest.mark.parametrize(
    ("declared", "message"),
    [({"title": "No status"}, "declares no error status"), ({"status": HTTPStatus.OK, "title": "Fine"}, "declares no error status"), ({"status": HTTPStatus.NOT_FOUND}, "declares no title")],
    ids=["no-status", "success-status", "no-title"],
)
def test_an_error_declares_its_status_and_title(declared: dict[str, object], message: str) -> None:
    with pytest.raises(api.DeclarationError, match=message):
        type("Undeclared", (api.Error,), declared)


class PricingError(api.Error, abstract=True):
    """A base the service's pricing errors share, which declares no status or title of its own."""


class UnpricedError(PricingError):
    status = HTTPStatus.NOT_FOUND
    title = "No price for the security"


class UnpricedTodayError(UnpricedError):
    title = "No price for the security today"


def test_an_abstract_base_declares_nothing_and_its_subclasses_everything() -> None:
    assert problem(UnpricedError("no price for T1"), REQUEST)[0].type == "unpriced"
    assert (UnpricedTodayError.status, UnpricedTodayError.type) == (HTTPStatus.NOT_FOUND, "unpriced-today")  # a status may be inherited
    with pytest.raises(api.DeclarationError, match="declares no error status"):
        type("Bare", (PricingError,), {})


@pytest.mark.parametrize("base", [PricingError, api.Error])
def test_an_abstract_error_is_never_raised(base: type[api.Error]) -> None:
    with pytest.raises(TypeError, match="is a base for errors, and is never raised itself"):
        base("no price")
