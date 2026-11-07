from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from basis.core import hooks, sync
from basis.core.check import Runner, check
from basis.core.sync import AgentFiles

type Git = Callable[..., str]
"""conftest's `git` fixture."""

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
def repo(tmp_path: Path, git: Git) -> Path:
    """A clone whose files are what basis writes, and which runs its hooks."""
    sync.write(tmp_path, [FILES])
    git(tmp_path, "init", "--quiet")
    hooks.ensure(tmp_path)
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


def test_agent_files_that_differ_from_what_the_parts_write_fail_and_say_to_sync(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "AGENTS.md").write_text("# Agent guide\n", encoding="utf-8")
    (repo / ".agents/skills/job-gone").mkdir(parents=True)
    _, run = runner()
    assert check(repo, [FILES], run=run) == 1
    assert errors(capsys) == ["ERROR: AGENTS.md, .agents/skills/job-gone do not match what the installed basis writes: run `uv run basis sync`, which rewrites or removes each"]


def test_claude_settings_basis_cannot_read_fail_and_every_other_step_still_runs(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / ".claude/settings.json").write_text("{", encoding="utf-8")
    ran, run = runner()
    assert check(repo, [FILES], run=run) == 1
    assert len(ran) == 5
    [line] = errors(capsys)
    assert line.startswith(f"ERROR: {repo / '.claude/settings.json'} is not JSON, so basis cannot add its rules to it: ")


def test_a_clone_that_runs_other_hooks_fails_and_says_how_to_run_the_repos(repo: Path, git: Git, capsys: pytest.CaptureFixture[str]) -> None:
    git(repo, "config", "core.hooksPath", "elsewhere")
    ran, run = runner()
    assert check(repo, [FILES], run=run) == 1
    assert len(ran) == 5
    assert errors(capsys) == ["ERROR: git runs this clone's hooks from elsewhere, not .githooks/, so the repo's commit and push hooks never run: run `git config core.hooksPath .githooks`"]
