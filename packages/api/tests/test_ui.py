"""An app that serves a built UI at `/`, its routes under `/api`, on one origin (step 2.14, design.md "One origin")."""

from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from basis import api
from basis.api import openapi


class Hello(BaseModel):
    greeting: str


def hello() -> Hello:
    return Hello(greeting="hello")


def service(ui: str | None) -> api.Service:
    return api.Service(name="greeter", settings=api.Settings, routes=[api.get("/hello", hello, deadline_s=1, response=Hello, errors=[])], ui=ui)


@pytest.fixture
def built(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An app's folder, which the app runs in, holding a built UI in web/dist."""
    dist = tmp_path / "web" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>greeter</title>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log('hi')", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return dist


@pytest.fixture
def client(built: Path) -> Iterator[TestClient]:
    del built
    with TestClient(api.build(service("web/dist"), settings=api.Settings.given()).asgi) as started:
        yield started


def test_the_ui_is_served_at_the_root_and_its_files_as_they_are(client: TestClient) -> None:
    page = client.get("/")
    assert (page.status_code, page.headers["content-type"].split(";")[0], "<title>greeter</title>" in page.text) == (HTTPStatus.OK, "text/html", True)
    assert client.get("/assets/app.js").text == "console.log('hi')"


def test_a_client_side_route_is_the_ui_and_a_missing_file_is_a_404(client: TestClient) -> None:
    assert "<title>greeter</title>" in client.get("/positions/7").text
    assert client.get("/assets/missing.js").status_code == HTTPStatus.NOT_FOUND


def test_the_routes_are_under_api_and_an_unknown_one_is_the_apis_404(client: TestClient) -> None:
    assert client.get("/api/hello").json() == {"greeting": "hello"}
    assert "<title>greeter</title>" in client.get("/hello").text  # without /api, a path is the UI's
    missing = client.get("/api/nothing")
    assert (missing.status_code, missing.headers["content-type"]) == (HTTPStatus.NOT_FOUND, "application/problem+json")


def test_the_document_keeps_the_routes_paths_and_lists_api_as_its_server(client: TestClient) -> None:
    served = client.get("/openapi.json").json()
    assert (sorted(served["paths"]), served["servers"]) == (["/hello"], [{"url": "/api"}])
    assert served == openapi.document(service("web/dist"))
    assert client.get("/health/ready").status_code == HTTPStatus.OK


def test_an_unbuilt_ui_answers_503_saying_how_and_the_routes_still_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with TestClient(api.build(service("web/dist"), settings=api.Settings.given()).asgi) as client:
        page = client.get("/")
        assert (page.status_code, page.json()["type"]) == (HTTPStatus.SERVICE_UNAVAILABLE, "ui-not-built")
        assert client.get("/api/hello").json() == {"greeting": "hello"}


def test_an_app_without_a_ui_is_unchanged() -> None:
    with TestClient(api.build(service(None), settings=api.Settings.given()).asgi) as client:
        assert client.get("/hello").json() == {"greeting": "hello"}
        assert "servers" not in client.get("/openapi.json").json()


@pytest.mark.parametrize("folder", ["", "/srv/ui", "../ui", "web/../../ui", "C:\\\\ui"], ids=["empty", "absolute", "above", "climbs", "windows-absolute"])
def test_a_ui_folder_outside_the_apps_own_is_refused(folder: str) -> None:
    with pytest.raises(api.DeclarationError, match="give the built UI's folder as a relative path inside the app's own folder"):
        service(folder)
