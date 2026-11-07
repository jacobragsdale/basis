"""A domain's errors, which are plain exceptions, and the HTTP meaning `api.error` gives each, once: a route that
declares it answers its problem when its handler raises the exception, or a subclass, the most specific meaning it
declares winning, with the exception's message as the detail; a domain exception a route does not declare is a 500."""

from collections.abc import Iterator
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient

from basis import api


class Matured(Exception):  # noqa: N818 -- a domain's own name for it, as a domain module declares one
    pass


class CalledEarly(Matured):
    pass


class Unlisted(Exception):  # noqa: N818 -- a domain's own name for it
    pass


class MissingPriceError(Exception):
    pass


MATURED = api.error(Matured, status=HTTPStatus.UNPROCESSABLE_CONTENT, title="The bond has matured")
CALLED_EARLY = api.error(CalledEarly, status=HTTPStatus.CONFLICT, title="The bond was called before its maturity")
MISSING_PRICE = api.error(MissingPriceError, status=HTTPStatus.NOT_FOUND, title="No price for the bond")


def matured() -> dict[str, str]:
    msg = "US912810TM64 matured on 2034-11-15"
    raise Matured(msg)


def called_early() -> dict[str, str]:
    msg = "US912810TM64 was called on 2030-11-15"
    raise CalledEarly(msg)


def called_early_as_matured() -> dict[str, str]:
    msg = "US912810TM64 was called on 2030-11-15"
    raise CalledEarly(msg)


def unlisted() -> dict[str, str]:
    msg = "no listing for US912810TM64"
    raise Unlisted(msg)


def missing() -> dict[str, str]:
    raise MissingPriceError


def silently() -> dict[str, str]:
    raise Matured


@pytest.fixture
def client() -> Iterator[TestClient]:
    routes = [
        api.get("/matured", matured, deadline_s=1, response=dict[str, str], errors=[MATURED]),
        api.get("/called-early", called_early, deadline_s=1, response=dict[str, str], errors=[MATURED, CALLED_EARLY]),
        api.get("/called-early-as-matured", called_early_as_matured, deadline_s=1, response=dict[str, str], errors=[MATURED]),
        api.get("/unlisted", unlisted, deadline_s=1, response=dict[str, str], errors=[MATURED]),
        api.get("/missing", missing, deadline_s=1, response=dict[str, str], errors=[MISSING_PRICE]),
        api.get("/silently", silently, deadline_s=1, response=dict[str, str], errors=[MATURED]),
    ]
    service = api.Service(name="bonds", settings=api.Settings, routes=routes)
    with TestClient(api.build(service, settings=api.Settings.given()).asgi, raise_server_exceptions=False) as started:
        yield started


def test_a_declared_domain_exception_answers_its_meaning_with_its_message() -> None:
    assert (MATURED.status, MATURED.title, MATURED.type, MATURED.exception) == (HTTPStatus.UNPROCESSABLE_CONTENT, "The bond has matured", "matured", Matured)
    assert MISSING_PRICE.type == "missing-price"


def test_a_route_answers_a_declared_domain_exception_as_its_problem(client: TestClient) -> None:
    response = client.get("/matured")
    assert (response.status_code, response.headers["content-type"]) == (HTTPStatus.UNPROCESSABLE_CONTENT, "application/problem+json")
    body = response.json()
    assert (body["type"], body["title"], body["detail"], body["errors"]) == ("matured", "The bond has matured", "US912810TM64 matured on 2034-11-15", [])


def test_the_most_specific_meaning_a_route_declares_wins(client: TestClient) -> None:
    assert client.get("/called-early").json()["type"] == "called-early"
    assert client.get("/called-early-as-matured").json() | {"instance": None} == {
        "type": "matured", "title": "The bond has matured", "status": 422, "detail": "US912810TM64 was called on 2030-11-15", "errors": [], "instance": None,
    }  # fmt: skip


def test_a_domain_exception_without_a_message_has_its_title_as_its_detail(client: TestClient) -> None:
    assert client.get("/missing").json()["detail"] == "No price for the bond"
    assert client.get("/silently").json()["detail"] == "The bond has matured"


def test_a_domain_exception_a_route_does_not_declare_is_a_500_that_says_nothing_of_it(client: TestClient) -> None:
    response = client.get("/unlisted")
    assert (response.status_code, response.json()) == (
        HTTPStatus.INTERNAL_SERVER_ERROR,
        {"type": "about:blank", "title": "Internal Server Error", "status": 500, "instance": response.json()["instance"]},
    )


def test_each_meaning_is_in_the_document_under_its_status() -> None:
    route = api.get("/called-early", called_early, deadline_s=1, response=dict[str, str], errors=[MATURED, CALLED_EARLY])
    document = api.build(api.Service(name="bonds", settings=api.Settings, routes=[route]), settings=api.Settings.given()).asgi.openapi()
    responses = document["paths"]["/called-early"]["get"]["responses"]
    assert responses["409"]["description"] == "called-early: The bond was called before its maturity"
    assert responses["422"]["description"] == "invalid-request: The request is not valid; matured: The bond has matured"


def test_a_route_declares_one_meaning_of_a_domain_exception() -> None:
    other = api.error(Matured, status=HTTPStatus.GONE, title="The bond is gone")
    with pytest.raises(api.DeclarationError) as raised:
        api.get("/matured", matured, deadline_s=1, response=dict[str, str], errors=[MATURED, other])
    assert str(raised.value) == "matured declares two meanings of Matured, 'The bond has matured' and 'The bond is gone': declare the one api.error its errors.py gives it"


class Priced(api.Error):
    status = HTTPStatus.NOT_FOUND
    title = "Priced"


@pytest.mark.parametrize(
    ("exception", "declared", "message"),
    [
        ("Matured", {"status": HTTPStatus.UNPROCESSABLE_CONTENT, "title": "x"}, "is given no exception: give it a domain's own, as `class Matured(Exception)` in domain.py"),
        (Priced, {"status": HTTPStatus.NOT_FOUND, "title": "x"}, "is given an api.Error, which has its status and title already: declare it in the route's errors=[...] as it is"),
        (api.Unavailable, {"status": HTTPStatus.SERVICE_UNAVAILABLE, "title": "x"}, "is given a kind of api.ServiceError, which basis answers as its own problem"),
        (ValueError, {"status": HTTPStatus.UNPROCESSABLE_CONTENT, "title": "x"}, "is given a builtin exception, which code anywhere raises"),
        (Matured, {"status": 200, "title": "x"}, "has status 200: give it a 4xx or 5xx, as status=HTTPStatus.UNPROCESSABLE_CONTENT"),
        (Matured, {"status": True, "title": "x"}, "has status True"),
        (Matured, {"status": HTTPStatus.UNAUTHORIZED, "title": "x"}, "is a 401, which basis answers itself"),
        (Matured, {"status": HTTPStatus.UNPROCESSABLE_CONTENT, "title": " "}, 'has no title: give it title="..."'),
    ],
    ids=["no-class", "an-api-error", "a-service-error", "a-builtin", "a-success", "a-bool", "a-401", "no-title"],
)
def test_a_meaning_is_given_to_a_domains_own_exception_with_a_status_and_a_title(exception: object, declared: dict[str, object], message: str) -> None:
    with pytest.raises(api.DeclarationError) as raised:
        api.error(exception, **declared)  # ty: ignore[invalid-argument-type] -- each case's own wrong declaration
    assert str(raised.value).startswith("api.error(")
    assert message in str(raised.value)
