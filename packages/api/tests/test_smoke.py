"""`basis smoke --env ENV`, basis-api's side (step 2.10): the API as an environment serves it, through a transport that
stands in for the deploy, checked against the repo's committed document, its readiness and each route's example."""

import json
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path

import httpx2
import pytest
from pydantic import BaseModel

from basis import api
from basis.api import openapi, smoke
from basis.core.sync import MARKER


class Quote(BaseModel):
    security: str
    price: float


def quote(security: str) -> Quote:
    return Quote(security=security, price=99.5)


SERVICE = api.Service(name="quotes", settings=api.Settings, routes=[api.get("/quotes/{security}", quote, deadline_s=1, response=Quote, errors=[], smoke={"path": {"security": "UST10Y"}})])
DOCUMENT = openapi.document(SERVICE)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo whose committed document is the service's, and a user whose qa.env names where QA serves it."""
    folder = tmp_path / "repo"
    folder.mkdir()
    (folder / "openapi.json").write_text(json.dumps({"x-basis": MARKER, **DOCUMENT}), encoding="utf-8")
    config = tmp_path / "config"
    (config / "basis").mkdir(parents=True)
    (config / "basis" / "qa.env").write_text("BASIS_ENV=qa\nBASIS_SMOKE__URL=https://qa.example.com/api\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("APPDATA", str(config))  # where Windows keeps the same folder
    for name in [name for name in __import__("os").environ if name.startswith("BASIS_")]:
        monkeypatch.delenv(name)
    return folder


def deployed(document: object = DOCUMENT, *, ready: int = 200, body: object = None, commit: str | None = "3f1c2a9e") -> httpx2.MockTransport:
    """QA's deploy: its document, its readiness, the commit it runs and its answer to the example."""
    answers: dict[str, Callable[[], httpx2.Response]] = {
        "/api/openapi.json": lambda: httpx2.Response(200, json=document),
        "/api/health/ready": lambda: httpx2.Response(ready, json={"status": "ready", "commit": commit}),
        "/api/quotes/UST10Y": lambda: httpx2.Response(200, json=body if body is not None else {"security": "UST10Y", "price": 99.5}),
    }
    return httpx2.MockTransport(lambda request: answers.get(request.url.path, lambda: httpx2.Response(HTTPStatus.NOT_FOUND))())


def test_a_deploy_of_the_committed_document_that_is_ready_and_answers_its_examples_passes(repo: Path) -> None:
    assert smoke.smoke(repo, "qa", service=SERVICE, transport=deployed()) == []
    assert smoke.smoke(repo, "qa", commit="3f1c2a9e", service=SERVICE, transport=deployed()) == []


@pytest.mark.parametrize(
    ("served", "reason"),
    [("0aa1b2c3", "https://qa.example.com/api serves commit 0aa1b2c3, not 3f1c2a9e"), (None, "https://qa.example.com/api serves no commit (its image sets no BASIS_GIT_SHA), not 3f1c2a9e")],
)
def test_a_deploy_of_another_commit_fails_the_smoke_that_names_its_commit(repo: Path, served: str | None, reason: str) -> None:
    assert smoke.smoke(repo, "qa", commit="3f1c2a9e", service=SERVICE, transport=deployed(commit=served)) == [
        (reason, "wait until the deploy of 3f1c2a9e finishes, and check that its release reached the environment")
    ]


def test_a_deploy_of_another_document_fails_saying_to_deploy_this_commit(repo: Path) -> None:
    [(reason, fix)] = smoke.smoke(repo, "qa", service=SERVICE, transport=deployed({**DOCUMENT, "info": {"title": "other", "version": "1"}}))
    assert (reason, fix) == ("the document https://qa.example.com/api serves is not this repo's openapi.json", "deploy this commit, or check out the commit that is deployed")


@pytest.mark.parametrize(("path", "reason"), [
    ("/api/openapi.json", "https://qa.example.com/api/openapi.json answered 200 with text/html, not JSON"),
    ("/api/health/ready", "https://qa.example.com/api/health/ready answered 200 with text/html, not JSON"),
], ids=["document", "readiness"])  # fmt: skip
def test_a_200_that_is_not_json_fails_as_one_line(repo: Path, path: str, reason: str) -> None:
    """As a UI's own page answers a path the ingress routes to it, not to the API."""
    served = deployed()

    def answer(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == path:
            return httpx2.Response(200, text="<!doctype html><title>positions</title>", headers={"content-type": "text/html"})
        return served.handle_request(request)

    found = smoke.smoke(repo, "qa", service=SERVICE, transport=httpx2.MockTransport(answer))
    assert (reason, "check that BASIS_SMOKE__URL is the base the API's paths are under, where the ingress routes them to the API") in found


def test_a_float_that_answers_null_for_nan_passes(repo: Path) -> None:
    assert smoke.smoke(repo, "qa", service=SERVICE, transport=deployed(body={"security": "UST10Y", "price": None})) == []


def test_an_api_that_is_not_ready_fails(repo: Path) -> None:
    [(reason, _)] = smoke.smoke(repo, "qa", service=SERVICE, transport=deployed(ready=503))
    assert reason == "https://qa.example.com/api/health/ready answered 503, so the API is not ready"


def test_an_example_whose_answer_its_response_does_not_hold_fails(repo: Path) -> None:
    [(reason, _)] = smoke.smoke(repo, "qa", service=SERVICE, transport=deployed(body={"security": "UST10Y", "price": "99.5"}))
    assert reason.startswith("quote's example, GET https://qa.example.com/api/quotes/UST10Y, answered a body its response does not hold:")


def test_an_environment_that_names_no_url_says_how_to_name_it(repo: Path, tmp_path: Path) -> None:
    (tmp_path / "config" / "basis" / "qa.env").write_text("BASIS_ENV=qa\n", encoding="utf-8")
    [(reason, fix)] = smoke.smoke(repo, "qa", service=SERVICE, transport=deployed())
    assert reason == "basis smoke has nowhere to reach service quotes in qa"
    assert fix.startswith("set BASIS_SMOKE__URL in qa's settings")


def changes() -> Quote:
    return Quote(security="X", price=1.0)


def test_only_a_get_takes_an_example() -> None:
    with pytest.raises(api.DeclarationError, match="basis smoke sends read-only requests alone"):
        api.post("/quotes", changes, deadline_s=1, response=Quote, errors=[], smoke={"query": {}}, anonymous=True)
