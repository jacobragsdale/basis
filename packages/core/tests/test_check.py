from collections.abc import Sequence
from pathlib import Path

import pytest

from basis.core import sync
from basis.core.check import Runner, check
from basis.core.sync import AgentFiles

FILES = AgentFiles("basis-alpha", "the alpha block\n", {})


def runner(*failing: str) -> tuple[list[tuple[str, ...]], Runner]:
    """A runner that fails the steps whose command holds a word of `failing`, and the commands it was given."""
    ran: list[tuple[str, ...]] = []

    def run(argv: Sequence[str], _folder: Path) -> int:
        ran.append(tuple(argv))
        return 1 if set(argv) & set(failing) else 0

    return ran, run


def errors(capsys: pytest.CaptureFixture[str]) -> list[str]:
    lines = capsys.readouterr().err.splitlines()
    assert all(line.startswith("ERROR: ") for line in lines)
    return lines


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    sync.write(tmp_path, FILES)
    return tmp_path


def test_a_repo_that_passes_every_step_passes(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ran, run = runner()
    assert check(repo, [FILES], run=run) == 0
    assert [argv[argv.index("--quiet") + 1] if "run" in argv else argv[1] for argv in ran] == ["lock", "ruff", "ruff", "ty", "pytest"]
    assert errors(capsys) == []


def test_every_failing_step_prints_its_line_and_the_rest_still_run(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ran, run = runner("format", "pytest")
    assert check(repo, [FILES], run=run) == 1
    assert len(ran) == 5
    assert errors(capsys) == [
        "ERROR: ruff format would reformat the files listed above: run `uv run ruff format .`",
        "ERROR: pytest failed, as above: fix the code, or the test, that each failure names",
    ]


def test_a_stale_lockfile_stops_the_check_before_the_tools_run(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ran, run = runner("lock")
    assert check(repo, [FILES], run=run) == 1
    assert len(ran) == 1
    assert errors(capsys) == ["ERROR: uv.lock does not match pyproject.toml: run `uv lock`, and commit uv.lock"]


def test_agent_files_that_differ_from_the_installed_part_fail_and_say_to_sync(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "AGENTS.md").write_text("# Agent guide\n", encoding="utf-8")
    _, run = runner()
    assert check(repo, [FILES], run=run) == 1
    assert errors(capsys) == ["ERROR: AGENTS.md does not match the installed basis-alpha: run `uv run basis sync`, which rewrites them"]
