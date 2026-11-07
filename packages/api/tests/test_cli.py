"""`basis serve`, `basis call` and `basis dev`, and the files basis-api writes, as its part gives them to the `basis`
command, in a repo whose parts include `api`. `basis call` runs the example service, on the fakes it declares."""

import io
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from basis import api
from basis.api import dev, fakes
from basis.api.example import service as example
from basis.core import command

type Git = Callable[..., str]
"""Runs `git ARGV...` in a folder and returns what it prints; fails the test when git does."""

PYPROJECT = '[project]\nname = "pricing-api"\nversion = "0.1.0"\n\n[tool.basis]\nparts = ["api"]\n'


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    return tmp_path


def basis(repo: Path, *argv: str) -> int:
    return command.run(argv, cwd=repo, installed=command.installed())  # the installed entry points, basis-api's named api


def test_basis_lists_serve_in_a_repo_with_an_api(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo, "--help") == 0
    out = capsys.readouterr().out
    assert "From the api part:" in out
    assert re.search(r"^  serve +serve the API with uvicorn until SIGTERM", out, re.MULTILINE)  # whatever the column's width


def test_basis_lists_dev_and_call_in_a_repo_with_an_api(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo, "--help") == 0
    out = capsys.readouterr().out
    assert re.search(r"^  dev +serve the API on its declared fakes, on this machine alone", out, re.MULTILINE)
    assert re.search(r"^  call +run one route in this process", out, re.MULTILINE)


def test_sync_writes_the_apis_image_command_and_agent_block(repo: Path) -> None:
    assert basis(repo, "sync") == 0
    assert "#   an API: serve\nENTRYPOINT" in (repo / "Dockerfile").read_text(encoding="utf-8")
    assert "## basis-api\n" in (repo / "AGENTS.md").read_text(encoding="utf-8")


def test_serve_never_touches_git(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`basis sync` points a clone at its hooks, and `basis serve`, which a container runs, leaves git alone."""
    git = _git(monkeypatch)
    git(repo, "init", "--quiet")
    assert basis(repo, "sync") == 0
    assert git(repo, "config", "--get", "core.hooksPath") == ".githooks\n"
    git(repo, "config", "--unset", "core.hooksPath")
    assert basis(repo, "serve", "nowhere") == 1
    with pytest.raises(subprocess.CalledProcessError):  # git config exits 1 for a setting no clone has
        git(repo, "config", "--get", "core.hooksPath")


def test_a_service_basis_cannot_find_is_one_error_line(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo, "serve", "nowhere") == 1
    assert capsys.readouterr().err.splitlines()[-1].startswith("ERROR: no service named 'nowhere': ")


def test_settings_basis_cannot_read_are_one_error_line(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("BASIS_SERVE__PORT", "eighty")
    assert basis(repo, "serve", "basis.api.example.service") == 1
    expected = "ERROR: the settings are not valid, so the API cannot start: set BASIS_SERVE__PORT: Input should be a valid integer, unable to parse string as an integer"
    assert capsys.readouterr().err.splitlines()[-1] == expected


def _git(monkeypatch: pytest.MonkeyPatch) -> Git:
    """git as a fresh machine has it: no clone a hook or `git rebase --exec` exports, so the test never writes the clone
    it runs in, and no user or system settings."""
    found = shutil.which("git")
    if found is None:
        pytest.skip("git is not installed")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    def run(folder: Path, *argv: str) -> str:
        return subprocess.run([found, *argv], cwd=folder, check=True, capture_output=True, text=True).stdout

    return run


ISIN = "US912810TM64"
EXAMPLE = ("--service", "basis.api.example.service")


@pytest.fixture
def clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """No BASIS_ variable of the developer's, so `basis call` reads only what a test sets."""
    for name in [name for name in os.environ if name.startswith("BASIS_")]:
        monkeypatch.delenv(name)


_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def _called(capsys: pytest.CaptureFixture[str]) -> tuple[JsonValue, str]:
    out, err = capsys.readouterr()
    return _JSON.validate_json(out) if out else None, err.splitlines()[-1] if err else ""


@pytest.mark.usefixtures("clean")
def test_call_prints_a_routes_response_as_json(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo, "call", "bond", json.dumps({"path": {"security": ISIN}}), "--fake", *EXAMPLE) == 0
    body, _ = _called(capsys)
    assert body == {"security_id": ISIN, "coupon_rate": "0.045", "convention": "30/360", "frequency": 2, "issued": "2024-11-15", "maturity": "2034-11-15"}


@pytest.mark.usefixtures("clean")
def test_call_runs_compute_in_the_pool(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("BASIS_COMPUTE__WORKERS", "1")
    assert basis(repo, "call", "bond_yield", json.dumps({"path": {"security": ISIN}, "query": {"settle": "2026-11-06"}}), "--fake", *EXAMPLE) == 0
    body, _ = _called(capsys)
    assert isinstance(body, dict)
    assert body["annual_yield"] == "0.0472658580"


@pytest.mark.usefixtures("clean")
def test_call_reads_its_request_from_stdin(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    request = {"query": {"face": 1000000, "rate": "0.045", "start": "2026-05-15", "end": "2026-11-15", "convention": "30/360"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request)))
    assert basis(repo, "call", "accrued_interest", "-", "--fake", *EXAMPLE) == 0
    body, _ = _called(capsys)
    assert isinstance(body, dict)
    assert body["interest"] == "22500.00"


@pytest.mark.usefixtures("clean")
@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (("bond", json.dumps({"path": {"security": "US0000000000"}})), "ERROR: bond answered 404 unknown-bond, No bond has that ISIN: the warehouse has no bond US0000000000"),
        (
            ("accrued_interest", json.dumps({"query": {"settle": "2026-11-06"}})),
            "ERROR: accrued_interest answered 422 invalid-request, The request is not valid: query /settle: Unknown query parameter: this route takes face, rate, start, end, convention",
        ),
        (("bonds", "{}"), "ERROR: service accruals has no route named bonds: give one of accrued_interest, conventions, bond, dirty_price, bond_yield, record_settlement"),
        (("bond", "{}"), "ERROR: bond's path /bonds/{security} needs security: give each in the request's path"),
        (("bond", '{"paths": {}}'), 'ERROR: the request to bond is not valid: paths: Extra inputs are not permitted: give one JSON object of "path", "query", "headers" and "body"'),
    ],
    ids=["a-problem", "an-invalid-request", "no-such-route", "no-path", "an-unknown-key"],
)
def test_a_call_that_fails_is_one_error_line(repo: Path, capsys: pytest.CaptureFixture[str], argv: tuple[str, ...], error: str) -> None:
    assert basis(repo, "call", *argv, "--fake", *EXAMPLE) == 1
    _, last = _called(capsys)
    assert last.startswith(error)


@pytest.mark.usefixtures("clean")
def test_call_on_real_resources_names_the_settings_they_need(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo, "call", "bond", json.dumps({"path": {"security": ISIN}}), "--real", *EXAMPLE) == 1
    _, last = _called(capsys)
    assert last.startswith("ERROR: service accruals's resources are not all configured, so the API cannot start: set BASIS_DATABASES__WAREHOUSE__KIND")


def test_call_chooses_its_resources_out_loud(repo: Path) -> None:
    assert basis(repo, "call", "bond", "{}", *EXAMPLE) == 2  # a command line that does not parse


def _with_fakes(reference: str | None) -> api.Service:
    return api.Service(name="accruals", settings=example.Settings, resources=example.service.resources, routes=example.service.routes, fakes=reference)


def without_pricing() -> dict[fakes.Faked, fakes.Fake]:
    return {example.WAREHOUSE: fakes.Database()}


@pytest.mark.parametrize(
    ("reference", "message"),
    [
        (None, "declares no fakes to run on: give its api.Service fakes='module:function'"),
        ("basis.api.example.nowhere:fakes", "no module basis.api.example.nowhere exists"),
        ("basis.api.example.dev:nothing", "basis.api.example.dev has no nothing"),
        ("basis.api.example.dev:BONDS", "a dict: name a function that takes nothing and returns them"),
        (f"{__name__}:without_pricing", "fakes none of pricing, and service accruals runs on fakes alone here"),
    ],
    ids=["none", "no-module", "no-function", "not-a-function", "a-service-unfaked"],
)
def test_dev_and_call_run_on_the_fakes_a_service_declares_and_no_others(reference: str | None, message: str) -> None:
    with pytest.raises(api.ServiceError, match=re.escape(message)):
        dev.fakes(_with_fakes(reference))
