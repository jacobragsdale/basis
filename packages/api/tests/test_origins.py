"""The origins whose pages may call the API from a browser: none unless the settings name them, and then only those."""

import os
from collections.abc import Iterator
from http import HTTPStatus

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from basis import api

UI = "https://positions.example"


class Book(BaseModel):
    name: str


def book() -> Book:
    return Book(name="RATES")


def broken() -> Book:
    msg = "a defect"
    raise RuntimeError(msg)


SERVICE = api.Service(name="books", settings=api.Settings, routes=[api.get("/book", book, deadline_s=1, response=Book, errors=[]), api.get("/broken", broken, deadline_s=1, response=Book, errors=[])])
PREFLIGHT = {"origin": UI, "access-control-request-method": "POST", "access-control-request-headers": "authorization, content-type"}


def client(*allowed: str) -> Iterator[TestClient]:
    with TestClient(api.build(SERVICE, settings=api.Settings.given(allowed_origins=list(allowed))).asgi, raise_server_exceptions=False) as started:
        yield started


@pytest.fixture
def alone() -> Iterator[TestClient]:
    yield from client()


@pytest.fixture
def allowing() -> Iterator[TestClient]:
    yield from client(UI, "http://localhost:5173")


def test_without_the_setting_no_other_origins_page_may_call_the_api(alone: TestClient) -> None:
    preflight = alone.options("/book", headers=PREFLIGHT)
    assert (preflight.status_code, "access-control-allow-origin" in preflight.headers) == (HTTPStatus.METHOD_NOT_ALLOWED, False)
    response = alone.get("/book", headers={"origin": UI})
    assert (response.status_code, "access-control-allow-origin" in response.headers) == (HTTPStatus.OK, False)


def test_an_allowed_origins_page_may_send_its_token_and_read_the_request_id(allowing: TestClient) -> None:
    preflight = allowing.options("/book", headers=PREFLIGHT)
    assert preflight.status_code == HTTPStatus.OK
    assert preflight.headers["access-control-allow-origin"] == UI
    assert {"authorization", "content-type"} <= {header.strip().lower() for header in preflight.headers["access-control-allow-headers"].split(",")}
    assert "access-control-allow-credentials" not in preflight.headers
    response = allowing.get("/book", headers={"origin": UI})
    assert response.headers["access-control-allow-origin"] == UI
    assert {"x-request-id", "traceparent", "www-authenticate"} <= {header.strip().lower() for header in response.headers["access-control-expose-headers"].split(",")}


def test_an_origin_the_settings_do_not_name_is_refused(allowing: TestClient) -> None:
    preflight = allowing.options("/book", headers=PREFLIGHT | {"origin": "https://elsewhere.example"})
    assert (preflight.status_code, "access-control-allow-origin" in preflight.headers) == (HTTPStatus.BAD_REQUEST, False)
    assert "access-control-allow-origin" not in allowing.get("/book", headers={"origin": "https://elsewhere.example"}).headers


def test_an_allowed_origins_page_reads_a_500_too(allowing: TestClient) -> None:
    response = allowing.get("/broken", headers={"origin": UI})
    assert (response.status_code, response.headers["access-control-allow-origin"]) == (HTTPStatus.INTERNAL_SERVER_ERROR, UI)


@pytest.mark.parametrize("origin", [UI, "http://localhost:5173", "https://positions.example:8443", "http://[::1]:5173", "http://127.0.0.1"])
def test_an_origin_is_written_as_a_browser_sends_it(origin: str) -> None:
    assert api.Settings.given(allowed_origins=[origin]).allowed_origins == (origin,)


@pytest.mark.parametrize(
    ("origin", "message"),
    [
        ("*", "basis allows no wildcard: name each origin"),
        ("https://positions.example/", "give 'https://positions.example', with nothing after its host and port"),
        ("https://positions.example/app", "give 'https://positions.example', with nothing after its host and port"),
        ("https://Positions.Example", "give 'https://positions.example'"),
        ("https://positions.example:443", "give 'https://positions.example'"),
        ("http://localhost:80", "give 'http://localhost'"),
        ("positions.example", "is not an origin: give a scheme, https or http, and a host"),
        ("ftp://positions.example", "is not an origin: give a scheme, https or http, and a host"),
        ("https://user@positions.example", "is not an origin: give a scheme, https or http, and a host"),
        ("https://positions.example:99999", "has no port a browser sends"),
    ],
    ids=["wildcard", "trailing-slash", "a-path", "upper-case", "https-default-port", "http-default-port", "no-scheme", "another-scheme", "a-user", "a-port-too-high"],
)
def test_an_origin_written_any_other_way_is_refused_with_how_to_write_it(origin: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        api.Settings.given(allowed_origins=[origin])


def test_the_setting_reads_from_its_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [name for name in os.environ if name.startswith("BASIS_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("BASIS_ALLOWED_ORIGINS", f'["{UI}"]')
    assert api.Settings().allowed_origins == (UI,)
