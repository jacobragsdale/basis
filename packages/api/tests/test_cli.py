"""`basis serve`, as basis-api's part gives it to the `basis` command, in a repo whose parts include `api`."""

from pathlib import Path

import pytest

from basis.core import command

PYPROJECT = '[project]\nname = "pricing-api"\nversion = "0.1.0"\n\n[tool.basis]\nparts = ["api"]\n'


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    return tmp_path


def test_basis_lists_serve_in_a_repo_with_an_api(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert command.run(["--help"], cwd=repo, installed=command.installed()) == 0  # the installed entry point, named api
    out = capsys.readouterr().out
    assert "From the api part:" in out
    assert "serve  serve the API with uvicorn until SIGTERM" in out


def test_a_service_basis_cannot_find_is_one_error_line(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert command.run(["serve", "nowhere"], cwd=repo, installed=command.installed()) == 1
    assert capsys.readouterr().err.splitlines()[-1].startswith("ERROR: no service named 'nowhere': ")


def test_settings_basis_cannot_read_are_one_error_line(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("BASIS_SERVE__PORT", "eighty")
    assert command.run(["serve", "basis.api.example.service"], cwd=repo, installed=command.installed()) == 1
    assert (
        capsys.readouterr().err.splitlines()[-1]
        == "ERROR: the settings are not valid, so the API cannot start: set BASIS_SERVE__PORT: Input should be a valid integer, unable to parse string as an integer"
    )
