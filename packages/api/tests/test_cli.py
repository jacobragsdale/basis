"""`basis serve`, `basis call` and `basis dev`, and the files basis-api writes, as its part gives them to the `basis`
command, in a repo whose parts include `api`. `basis call` runs the example service, on the fakes it declares."""

import io
import json
import os
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest
from fastapi.responses import PlainTextResponse
from pydantic import JsonValue, TypeAdapter

from basis import api
from basis.api import dev, fakes, openapi, service, tokens
from basis.api import sync as api_sync
from basis.api.example import resources as declared
from basis.api.example import service as example
from basis.core import command
from basis.core import sync as core_sync

type Git = Callable[..., str]
"""conftest's `git` fixture."""

PYPROJECT = '[project]\nname = "pricing-api"\nversion = "0.1.0"\n\n[tool.basis]\nparts = ["api"]\n'


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An API repo, whose verbs act in dev, from the process's environment, as a pod's do: the user has no file of
    settings."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("BASIS_ENV", "dev")
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    return tmp_path


@pytest.fixture
def listed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The example, as the one service the installed packages list, as an API repo's own package lists its."""
    monkeypatch.setattr(service, "entry_points", lambda **_: [EntryPoint(name="accruals", value="basis.api.example.service:service", group=service.GROUP)])


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


@pytest.mark.usefixtures("listed")
def test_sync_writes_the_apis_image_command_agent_block_and_document(repo: Path) -> None:
    assert basis(repo, "sync") == 0
    assert "#   an API: serve, and relay on a schedule where its databases declare an outbox\nENTRYPOINT" in (repo / "Dockerfile").read_text(encoding="utf-8")
    assert "## basis-api\n" in (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert (repo / "openapi.json").read_text(encoding="utf-8") == openapi.text(example.service)
    assert core_sync.plan(repo, [api_sync.agent_files()]).stale() == []


@pytest.mark.usefixtures("listed")
def test_a_document_that_differs_from_the_services_is_stale_until_synced(repo: Path) -> None:
    assert basis(repo, "sync") == 0
    document = repo / "openapi.json"
    document.write_text(document.read_text(encoding="utf-8").replace('"operationId": "bond"', '"operationId": "get_bond"'), encoding="utf-8")
    assert core_sync.plan(repo, [api_sync.agent_files()]).stale() == [document]
    assert basis(repo, "sync") == 0
    assert document.read_text(encoding="utf-8") == openapi.text(example.service)


def test_a_repo_that_lists_no_service_has_no_document_and_every_other_file_is_written(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo, "sync") == 1
    assert capsys.readouterr().err.splitlines()[-1].startswith("ERROR: basis cannot make openapi.json, since the installed packages list no services in the basis.apis entry points")
    assert (repo / "Dockerfile").exists()
    assert not (repo / "openapi.json").exists()


def test_a_service_that_does_not_load_is_one_error_line(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "broken.py").write_text("import nowhere_at_all\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(repo))
    monkeypatch.setattr(service, "entry_points", lambda **_: [EntryPoint(name="broken", value="broken:service", group=service.GROUP)])
    assert basis(repo, "sync") == 1
    line = capsys.readouterr().err.splitlines()[-1]
    assert line == (
        "ERROR: basis cannot make openapi.json, since the service does not load: ModuleNotFoundError: No module named 'nowhere_at_all': fix it, then run `uv run basis sync`, which writes openapi.json"
    )


@pytest.mark.usefixtures("listed")
def test_serve_never_touches_git(repo: Path, git: Git) -> None:
    """`basis sync` points a clone at its hooks, and `basis serve`, which a container runs, leaves git alone."""
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
    assert body == {"security_id": ISIN, "coupon_rate": 0.045, "convention": "30/360", "frequency": 2, "issued": "2024-11-15", "maturity": "2034-11-15"}


@pytest.mark.usefixtures("clean")
def test_call_runs_compute_in_the_pool(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("BASIS_COMPUTE__WORKERS", "1")
    assert basis(repo, "call", "bond_yield", json.dumps({"path": {"security": ISIN}, "query": {"settle": "2026-11-06"}}), "--fake", *EXAMPLE) == 0
    body, _ = _called(capsys)
    assert isinstance(body, dict)
    assert body["annual_yield"] == pytest.approx(0.0472658580, rel=1e-9)


@pytest.mark.usefixtures("clean")
def test_call_reads_its_request_from_stdin(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    request = {"query": {"face": 1000000, "rate": 0.045, "start": "2026-05-15", "end": "2026-11-15", "convention": "30/360"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request)))
    assert basis(repo, "call", "accrued_interest", "-", "--fake", *EXAMPLE) == 0
    body, _ = _called(capsys)
    assert isinstance(body, dict)
    assert body["interest"] == pytest.approx(22500.0)


@pytest.mark.usefixtures("clean")
@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (("bond", json.dumps({"path": {"security": "US0000000000"}})), "ERROR: bond answered 404 unknown-bond, No bond has that ISIN: the warehouse has no bond US0000000000"),
        (
            ("accrued_interest", json.dumps({"query": {"settle": "2026-11-06"}})),
            "ERROR: accrued_interest answered 422 invalid-request, The request is not valid: parameter settle: Unknown query parameter: this route takes face, rate, start, end, convention",
        ),
        (("bonds", "{}"), "ERROR: service accruals has no route named bonds: give one of accrued_interest, conventions, bond, dirty_price, bond_yield, coupons, record_settlement"),
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
def test_call_on_real_resources_names_the_settings_they_need(repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASIS_GIT_SHA", "abc123")  # some setting of dev's, so the call reaches the service's own checks
    assert basis(repo, "call", "bond", json.dumps({"path": {"security": ISIN}}), "--real", *EXAMPLE) == 1
    _, last = _called(capsys)
    assert last.startswith("ERROR: service accruals's resources are not all configured, so the API cannot start: set BASIS_DATABASES__WAREHOUSE__KIND")


@pytest.mark.parametrize("argv", [["serve", *EXAMPLE[1:]], ["call", "bond", "{}", "--real", *EXAMPLE], ["relay", *EXAMPLE[1:]]], ids=["serve", "call-real", "relay"])
def test_a_verb_that_reaches_the_firm_s_systems_names_its_environment(repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    monkeypatch.delenv("BASIS_ENV")
    assert basis(repo, *argv) == 1
    assert capsys.readouterr().err.splitlines()[-1].endswith("names no environment, which basis never assumes: give --env dev|qa|prod, or set BASIS_ENV")


def test_in_prod_a_verb_with_write_logins_is_refused_unless_the_deployment_allows_writes(repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASIS_ENV", "prod")
    for name, value in {"KIND": "mssql", "CONNECTION_STRING": "Server=sql01;Database=Trading", "ISOLATION": "read_committed", "TIMEOUT_S": "5", "MAX_ROWS": "5"}.items():
        monkeypatch.setenv(f"BASIS_DATABASES__WAREHOUSE__{name}", value)
    monkeypatch.setenv("BASIS_DATABASES__WAREHOUSE__WRITE__CONNECTION_STRING", "Server=sql01;UID=api")
    assert basis(repo, "serve", *EXAMPLE[1:]) == 1
    assert "basis serve accruals, with write logins to warehouse, would write to prod, which basis refuses unless the process sets BASIS_PROD_WRITES=1" in capsys.readouterr().err


def test_call_on_its_fakes_reaches_nothing_and_needs_no_environment(repo: Path, monkeypatch: pytest.MonkeyPatch, clean: None) -> None:
    del clean
    monkeypatch.setenv("XDG_CONFIG_HOME", str(repo / "config"))
    assert basis(repo, "call", "bond", json.dumps({"path": {"security": ISIN}}), "--fake", *EXAMPLE) == 0


@pytest.mark.usefixtures("clean")
def test_call_on_its_fakes_takes_a_token_the_development_key_signs_for_it(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Where the settings name no identity: the app is on its fakes alone, in this process, so no one else signs in."""
    settlement = {"body": {"security_id": ISIN, "face": 100.0, "settle": "2026-11-06"}}
    assert basis(repo, "call", "record_settlement", json.dumps(settlement), "--fake", *EXAMPLE) == 1
    assert _called(capsys)[1].startswith("ERROR: record_settlement answered 401 sign-in-required, Sign-in is required")
    identity = dev.settings(example.service).identity
    assert identity is not None
    now = datetime.now(UTC)
    token = tokens.sign(identity, "jsmith", issued=now, expires=now + timedelta(minutes=5))
    assert basis(repo, "call", "record_settlement", json.dumps(settlement | {"headers": {"authorization": f"Bearer {token}"}}), "--fake", *EXAMPLE) == 0


@pytest.mark.usefixtures("clean")
def test_call_on_its_fakes_signs_in_as_the_user_it_is_given(repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """A request `basis check --full` found, signed in, carries the user's claims, not a token, which would expire."""
    settlement = {"body": {"security_id": ISIN, "face": 100.0, "settle": "2026-11-06"}, "user": {"sub": "jsmith"}}
    assert basis(repo, "call", "record_settlement", json.dumps(settlement), "--fake", *EXAMPLE) == 0
    capsys.readouterr()
    monkeypatch.setenv("BASIS_GIT_SHA", "abc123")  # some setting of dev's, so the environment loads
    assert basis(repo, "call", "record_settlement", json.dumps(settlement), "--real", *EXAMPLE) == 1
    fix = "basis call signs in as a user on the fakes alone, with the development key: give --fake, or a token of the environment's in its headers"
    assert _called(capsys)[1] == f'ERROR: the request to record_settlement gives "user": {fix}'


@pytest.mark.usefixtures("clean")
def test_call_reads_its_request_from_a_file_as_it_was_sent(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The form `basis check --full` writes a request Schemathesis found in: its path and query exactly as the URL
    carried them, and its body's bytes, which JSON may not hold."""
    sent = tmp_path / "request.json"
    sent.write_text(json.dumps({"target": f"/bonds/{ISIN}?as_of=x&as_of=y", "headers": {"content-type": "application/json"}, "content_base64": "wNLo7A=="}), encoding="utf-8")
    assert basis(repo, "call", "bond", f"@{sent}", "--fake", *EXAMPLE) == 1
    assert "422" in _called(capsys)[1]
    sent.write_text(json.dumps({"target": "/conventions"}), encoding="utf-8")
    assert basis(repo, "call", "bond", f"@{sent}", "--fake", *EXAMPLE) == 1
    assert _called(capsys)[1] == "ERROR: the request to bond is sent to /conventions, which is not bond's path /bonds/{security}: give its target a path the route answers"
    sent.write_text(json.dumps({"target": "/bonds/X", "path": {"security": "X"}}), encoding="utf-8")
    assert basis(repo, "call", "bond", f"@{sent}", "--fake", *EXAMPLE) == 1
    assert _called(capsys)[1].startswith('ERROR: the request to bond is not valid: the request: Value error, a "target" carries the path and the query as sent, so give no "path" or "query" beside it')
    assert basis(repo, "call", "bond", f"@{tmp_path / 'nowhere.json'}", "--fake", *EXAMPLE) == 1
    assert _called(capsys)[1].startswith(f"ERROR: the request to bond is the file {tmp_path / 'nowhere.json'}, which cannot be read")


def test_call_chooses_its_resources_out_loud(repo: Path) -> None:
    assert basis(repo, "call", "bond", "{}", *EXAMPLE) == 2  # a command line that does not parse


def _with_fakes(reference: str | None) -> api.Service:
    return api.Service(name="accruals", settings=declared.Settings, resources=example.service.resources, routes=example.service.routes, user=api.User, fakes=reference)


def without_pricing() -> dict[fakes.Faked, fakes.Fake]:
    return {declared.WAREHOUSE: fakes.Database()}


@pytest.mark.parametrize(
    ("reference", "message"),
    [
        (None, "declares no fakes to run on: give its api.Service fakes='module:function'"),
        ("basis.api.example.nowhere:fakes", "no module basis.api.example.nowhere exists"),
        ("basis.api.example.dev:nothing", "basis.api.example.dev has no nothing"),
        ("basis.api.example.dev:BONDS", "a dict: name a function that takes nothing and returns them"),
        (f"{__name__}:without_pricing", "fakes none of pricing, ledger, and service accruals runs on fakes alone here"),
    ],
    ids=["none", "no-module", "no-function", "not-a-function", "a-service-unfaked"],
)
def test_dev_and_call_run_on_the_fakes_a_service_declares_and_no_others(reference: str | None, message: str) -> None:
    with pytest.raises(api.DeclarationError, match=re.escape(message)):
        dev.fakes(_with_fakes(reference))


def plain() -> dict[str, str]:
    return PlainTextResponse("a price of 98.50")  # ty: ignore[invalid-return-type] -- a reply that is not JSON, on purpose


PLAIN = api.Service(name="plain", settings=api.Settings, routes=[api.get("/plain", plain, deadline_s=1, response=dict[str, str], errors=[])])


@pytest.mark.usefixtures("clean")
def test_a_reply_that_is_not_json_is_one_error_line_with_its_status_and_type(repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASIS_GIT_SHA", "abc123")  # some setting of dev's, so the call reaches the route
    assert basis(repo, "call", "plain", "{}", "--real", "--service", f"{__name__}:PLAIN") == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.splitlines()[-1] == "ERROR: plain answered 200 with text/plain; charset=utf-8, not JSON: basis call gives JSON alone, so serve the API with basis dev, and request the route there"
