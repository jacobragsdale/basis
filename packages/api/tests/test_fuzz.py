"""What `basis check` makes of an API's app in its own process: it builds and starts on the fakes its service
declares, and Schemathesis's requests from its own document find nothing it answers as the document does not say.
Each failure is one line, which names the smallest request that shows it as the `basis call` that makes it again."""

import re
import sys
from datetime import date
from http import HTTPStatus
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import Query
from hypothesis import configuration
from pydantic import BaseModel, ConfigDict

from basis import api
from basis.api import fuzz
from basis.api import service as services

CLOCK = api.clock("clock")
WAREHOUSE = api.database("warehouse", deadline_s=5, max_concurrent=4)


class Halves(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    halves: int


class NoHalves(api.Error):
    status = HTTPStatus.UNPROCESSABLE_CONTENT
    title = "Nothing halves into none"


def halves(count: Annotated[int, Query(ge=0, le=1000)]) -> Halves:
    if count == 0:
        msg = "no count"
        raise NoHalves(msg)
    return Halves(halves=count * 2)


def divides(count: Annotated[int, Query(ge=0, le=1000)]) -> Halves:
    return Halves(halves=1000 // (count - count))  # whatever the count, so the request found is the smallest whatever ran before


def today(clock: Annotated[api.Clock, api.given(CLOCK)]) -> dict[str, date]:
    return {"today": clock.now().date()}


async def stored(warehouse: Annotated[api.Reader, api.given(WAREHOUSE)]) -> dict[str, int]:
    return {"rows": len(await warehouse.fetch(Halves, "SELECT halves FROM halves"))}


class Required(api.Settings):
    divisor: int


@pytest.fixture
def fuzzing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Schemathesis on, as `BASIS_FUZZ=1` turns it on for `basis check`."""
    monkeypatch.setenv(fuzz.FUZZ, "1")


def test_without_basis_fuzz_the_app_is_only_built_on_its_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(fuzz.FUZZ, raising=False)
    monkeypatch.setattr(fuzz, "_fuzz", lambda *_: pytest.fail("Schemathesis ran without BASIS_FUZZ=1"))
    routes = [api.get("/halves", halves, deadline_s=1, response=Halves, errors=[NoHalves])]
    assert fuzz.check(api.Service(name="halving", settings=api.Settings, routes=routes)) == []


@pytest.mark.usefixtures("fuzzing")
def test_an_app_that_answers_as_its_document_says_passes() -> None:
    routes = [api.get("/halves", halves, deadline_s=1, response=Halves, errors=[NoHalves]), api.get("/today", today, deadline_s=1, response=dict[str, date], errors=[])]
    service = api.Service(name="halving", settings=api.Settings, resources=[CLOCK], routes=routes)
    assert fuzz.check(service) == []


@pytest.mark.usefixtures("fuzzing")
def test_a_route_that_answers_500_is_found_with_the_request_that_shows_it() -> None:
    service = api.Service(name="halving", settings=api.Settings, routes=[api.get("/divides", divides, deadline_s=1, response=Halves, errors=[])])
    [(reason, fix)] = fuzz.check(service)
    found = "Schemathesis found GET /divides answers a request its document allows as its document does not say, Server error (ST101); Undocumented status code (ST103)"
    assert re.fullmatch(re.escape(found) + r""" to `uv run basis call divides '\{"query": \{"count": \d+\}\}' --fake`""", reason), reason
    assert fix == "make the request with that command, which answers as the route does, and fix the route, or declare the error it answers with in its errors=[...]"


def test_an_app_that_does_not_build_on_its_fakes_is_one_line() -> None:
    service = api.Service(name="storing", settings=api.Settings, resources=[WAREHOUSE], routes=[api.get("/stored", stored, deadline_s=1, response=dict[str, int], errors=[])])
    [(reason, fix)] = fuzz.check(service)
    assert reason == "service storing's app does not build on the fakes it declares"
    assert fix.startswith("service storing declares no fakes to run on: give its api.Service fakes='module:function'")


def test_settings_basis_cannot_build_from_their_defaults_are_named() -> None:
    service = api.Service(name="halving", settings=Required, routes=[api.get("/halves", halves, deadline_s=1, response=Halves, errors=[NoHalves])])
    [(_, fix)] = fuzz.check(service)
    assert fix == "basis check builds the app from its settings' defaults alone, and divisor has none: give each a default in the service's Settings"


@pytest.mark.usefixtures("fuzzing")
def test_a_service_whose_callers_sign_in_is_built_on_the_development_key_and_called_anonymously() -> None:
    def mine(user: Annotated[api.User | None, api.given(api.User)]) -> dict[str, str]:
        if user is None:
            msg = "sign in to see your own"
            raise api.SignInRequiredError(msg)
        return {"signed": "in"}

    service = api.Service(name="signed", settings=api.Settings, user=api.User, routes=[api.get("/mine", mine, deadline_s=1, response=dict[str, str], errors=[])])
    assert fuzz.check(service) == []


@pytest.mark.usefixtures("fuzzing")
def test_without_schemathesis_the_repo_is_told_to_install_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "schemathesis", None)
    service = api.Service(name="halving", settings=api.Settings, routes=[api.get("/halves", halves, deadline_s=1, response=Halves, errors=[NoHalves])])
    assert fuzz.check(service) == [
        ("schemathesis is not installed, and basis check sends service halving the requests it makes from its own document", "add schemathesis to [dependency-groups] dev, and run `uv lock`")
    ]


def test_a_service_that_does_not_load_is_left_to_openapi_jsons_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(services, "entry_points", lambda **_: [EntryPoint(name="broken", value="nowhere_at_all:service", group=services.GROUP)])
    assert fuzz.problems(tmp_path) == []


SHADOWED = """from pydantic import BaseModel

from basis import api


class Up(BaseModel):
    up: bool


def up() -> Up:
    return Up(up=True)


def fakes() -> dict[object, object]:
    {body}


service = api.Service(name="shadowed", settings=api.Settings, routes=[api.get("/up", up, deadline_s=1, response=Up, errors=[])], fakes="shadowed.service:fakes")
"""


def test_the_repos_own_code_is_checked_not_an_installed_copy_of_it(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """As in the push hook's worktree, whose commit is checked while the clone's own checkout is the one installed."""
    for folder, body in [("installed", "return {}"), ("repo", "raise RuntimeError('the commit under check')")]:
        (tmp_path / folder / "shadowed").mkdir(parents=True)
        (tmp_path / folder / "shadowed" / "__init__.py").write_text("", encoding="utf-8")
        (tmp_path / folder / "shadowed" / "service.py").write_text(SHADOWED.replace("{body}", body), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path / "installed"))
    monkeypatch.setattr(services, "entry_points", lambda **_: [EntryPoint(name="shadowed", value="shadowed.service:service", group=services.GROUP)])
    [(reason, _)] = fuzz.problems(tmp_path / "repo")
    assert reason == "service shadowed's app does not build on the fakes it declares, since it raised RuntimeError: the commit under check"
    assert "shadowed" not in sys.modules


@pytest.mark.usefixtures("fuzzing")
def test_the_example_answers_every_request_as_its_document_says(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "entry_points", lambda **_: [EntryPoint(name="accruals", value="basis.api.example.service:service", group=services.GROUP)])
    assert fuzz.problems(Path(fuzz.__file__).parent / "example") == []  # a folder with no package of its own: the installed example


def test_an_app_whose_fakes_raise_is_one_line(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_service: api.Service) -> dict[object, object]:
        msg = "warehouse.csv"
        raise FileNotFoundError(msg)

    monkeypatch.setattr(fuzz.dev, "fakes", broken)
    service = api.Service(name="storing", settings=api.Settings, resources=[WAREHOUSE], routes=[api.get("/stored", stored, deadline_s=1, response=dict[str, int], errors=[])], fakes="nowhere:fakes")
    assert fuzz.check(service) == [
        ("service storing's app does not build on the fakes it declares, since it raised FileNotFoundError: warehouse.csv", "fix it, as `uv run basis dev` shows it in full")
    ]


@pytest.mark.usefixtures("fuzzing")
def test_hypothesis_keeps_nothing_in_the_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    homes: list[str | Path | None] = []
    keep = configuration.set_hypothesis_home_dir

    def kept(directory: str | Path | None) -> None:
        homes.append(directory)
        keep(directory)

    monkeypatch.setattr(configuration, "set_hypothesis_home_dir", kept)
    service = api.Service(name="halving", settings=api.Settings, routes=[api.get("/halves", halves, deadline_s=1, response=Halves, errors=[NoHalves])])
    assert fuzz.check(service) == []
    [home, after] = homes
    assert (Path(str(home)).name.startswith("basis-fuzz-"), Path(str(home)).exists(), after) == (True, False, None)
