"""`basis serve`, and the files basis-api writes, as its part gives them to the `basis` command, in a repo whose parts
include `api`."""

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

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
