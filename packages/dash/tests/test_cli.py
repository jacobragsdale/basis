"""`basis serve` and `basis dev`, as basis-dash's part gives them to the `basis` command, in a repo whose parts include
`dash`; and the one line that refuses a repo that also declares `api`, since a repo serves one thing."""

import re
from pathlib import Path

import pytest

from basis.core import command
from basis.core.settings import SettingsError
from basis.dash import server


def _repo(tmp_path: Path, parts: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname = "positions"\nversion = "0.1.0"\n\n[tool.basis]\nparts = {parts}\n', encoding="utf-8")
    return tmp_path


def basis(repo: Path, *argv: str) -> int:
    return command.run(argv, cwd=repo, installed=command.installed())  # the installed entry points, basis-dash's named dash


def test_basis_lists_serve_and_dev_in_a_repo_with_a_dash_app(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(_repo(tmp_path, '["dash", "job"]'), "--help") == 0
    out = capsys.readouterr().out
    assert "From the dash part:" in out
    assert re.search(r"^  serve +serve the Dash app with uvicorn until SIGTERM", out, re.MULTILINE)
    assert re.search(r"^  dev +serve the Dash app on this machine alone", out, re.MULTILINE)


def test_a_repo_that_declares_an_api_and_a_dash_app_is_refused_in_one_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(_repo(tmp_path, '["api", "dash"]'), "--help") == 1
    errors = [line for line in capsys.readouterr().err.splitlines() if line.startswith("ERROR")]
    assert errors == ["ERROR: the api and dash parts both add `basis serve`, and a repo cannot have both: declare one of them, and keep the other in a repo of its own"]


@pytest.mark.parametrize("name", ["DASH_DEBUG", "DASH_SHARED_STORAGE", "_DASH_FASTAPI_CONFIG"])
def test_serving_refuses_a_variable_that_changes_what_dash_does(name: str) -> None:
    with pytest.raises(SettingsError, match=f"the process sets {name}, which would change what Dash does"):
        server.refuse_dash_variables({name: "1", "BASIS_SERVE__PORT": "8000"})


def test_serve_names_the_app_it_cannot_find(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASIS_ENV", "dev")
    assert basis(_repo(tmp_path, '["dash"]'), "serve", "nowhere.app") == 1
    assert capsys.readouterr().err.splitlines()[-1].startswith("ERROR: no app named 'nowhere.app'")


def test_basis_check_runs_the_parts_checks_of_the_repos_app(tmp_path: Path) -> None:
    from basis.dash import checks, cli  # noqa: PLC0415 -- the part as basis check calls it

    part = cli.part()
    assert part.checks is not None
    assert part.checks(_repo(tmp_path, '["dash"]')) == checks.problems(tmp_path) == []  # a repo with no package has no app to check


def test_basis_lists_smoke_in_a_repo_with_a_dash_app(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(_repo(tmp_path, '["dash"]'), "--help") == 0
    assert re.search(r"^  smoke ", capsys.readouterr().out, re.MULTILINE)
