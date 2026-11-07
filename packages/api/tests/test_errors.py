from http import HTTPStatus
from uuid import uuid7

import pytest
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from basis import api
from basis.api.errors import problem

REQUEST = uuid7()


class Missing(api.Error):
    status = HTTPStatus.NOT_FOUND
    title = "No such position"


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
        (ValueError("password=hunter2 rejected"), api.Problem(type="about:blank", title="Internal Server Error", status=500, instance=REQUEST.urn), {}),
        (
            RequestValidationError(
                [
                    {"loc": ("query", "end"), "msg": "Field required", "type": "missing", "input": None},
                    {"loc": ("body", "legs", 0, "a/b~c"), "msg": "Input should be a valid decimal", "type": "decimal_parsing", "input": "hunter2"},
                    {"loc": ("body",), "msg": "JSON decode error", "type": "json_invalid", "input": {}},
                ]
            ),
            api.Problem(
                type="invalid-request",
                title="The request is not valid",
                status=422,
                instance=REQUEST.urn,
                errors=(
                    api.FieldError(location="query", pointer="/end", detail="Field required"),
                    api.FieldError(location="body", pointer="/legs/0/a~1b~0c", detail="Input should be a valid decimal"),
                    api.FieldError(location="body", pointer="", detail="JSON decode error"),
                ),
            ),
            {},
        ),
    ],
    ids=["declared", "declared-with-its-own-type", "no-route", "wrong-method", "http-detail", "unexpected", "invalid-request"],
)
def test_one_map_makes_every_error_a_problem(error: Exception, expected: api.Problem, headers: dict[str, str]) -> None:
    assert problem(error, REQUEST) == (expected, headers)


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
    with pytest.raises(TypeError, match=message):
        type("Undeclared", (api.Error,), declared)
