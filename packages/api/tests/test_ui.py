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


PAGE = {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
"""What a browser's navigation to a page accepts."""


@pytest.mark.parametrize("path", ["/positions/7", "/positions/BRK.B", "/hello"], ids=["a-route", "a-dot-in-its-last-segment", "an-api-path-too"])
def test_a_page_the_browser_navigates_to_is_the_ui(client: TestClient, path: str) -> None:
    page = client.get(path, headers=PAGE)
    assert (page.status_code, "<title>greeter</title>" in page.text) == (HTTPStatus.OK, True)


def test_a_missing_file_is_a_404(client: TestClient) -> None:
    assert client.get("/assets/missing.js").status_code == HTTPStatus.NOT_FOUND
    assert client.get("/favicon.ico", headers={"accept": "image/avif,image/webp,*/*"}).status_code == HTTPStatus.NOT_FOUND


def test_the_routes_are_under_api_and_an_unknown_one_is_the_apis_404(client: TestClient) -> None:
    assert client.get("/api/hello").json() == {"greeting": "hello"}
    missing = client.get("/api/nothing")
    assert (missing.status_code, missing.headers["content-type"]) == (HTTPStatus.NOT_FOUND, "application/problem+json")


def test_a_call_to_a_route_without_api_says_where_the_routes_are(client: TestClient) -> None:
    called = client.get("/hello", headers={"accept": "application/json"})
    assert (called.status_code, called.headers["content-type"]) == (HTTPStatus.NOT_FOUND, "application/problem+json")
    assert called.json()["detail"] == "the API's routes are under /api, and this path is one of them: request /api/hello"


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


def raw(client: TestClient, path: str, **headers: str) -> tuple[dict[str, str], bytes]:
    """The response to a GET of `path` with `headers`, its body as it was sent, never decoded."""
    with client.stream("GET", path, headers=headers) as response:
        return dict(response.headers), b"".join(response.iter_raw())


@pytest.fixture
def compressed(built: Path) -> Path:
    """The UI's script, with the copies its build compresses: stand-ins, whose bytes say which was sent."""
    (built / "assets" / "app.js.br").write_bytes(b"brotli bytes")
    (built / "assets" / "app.js.gz").write_bytes(b"gzip bytes")
    return built


@pytest.mark.parametrize(
    ("accepts", "encoding", "body"),
    [("gzip, deflate, br, zstd", "br", b"brotli bytes"), ("gzip", "gzip", b"gzip bytes"), ("br;q=0, gzip;q=0.8", "gzip", b"gzip bytes"), ("identity", None, b"console.log('hi')")],
)
def test_a_file_is_sent_as_the_compressed_copy_the_request_accepts(compressed: Path, client: TestClient, accepts: str, encoding: str | None, body: bytes) -> None:
    del compressed
    headers, sent = raw(client, "/assets/app.js", **{"accept-encoding": accepts})
    assert (headers.get("content-encoding"), headers["content-type"].split(";")[0], headers["vary"], sent) == (encoding, "text/javascript", "Accept-Encoding", body)


def test_a_hashed_asset_is_immutable_and_every_other_file_is_revalidated(client: TestClient) -> None:
    assert client.get("/assets/app.js").headers["cache-control"] == "public, max-age=31536000, immutable"
    assert client.get("/").headers["cache-control"] == "no-cache"
    assert client.get("/positions/7", headers=PAGE).headers["cache-control"] == "no-cache"  # the UI's index.html, for a client-side route


def test_a_reload_of_an_unchanged_file_is_a_304(compressed: Path, client: TestClient) -> None:
    del compressed
    for path, accepts in [("/", "identity"), ("/assets/app.js", "gzip")]:
        first, _ = raw(client, path, **{"accept-encoding": accepts})
        again = client.get(path, headers={"accept-encoding": accepts, "if-none-match": first["etag"]})
        assert (again.status_code, again.headers["cache-control"], again.content) == (HTTPStatus.NOT_MODIFIED, first["cache-control"], b"")
