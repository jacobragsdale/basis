from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from basis.core import hooks, sync
from basis.core.check import Runner, check
from basis.core.sync import AgentFiles, stamped

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
    (repo / ".agents/skills/report-gone").mkdir(parents=True)
    (repo / ".agents/skills/report-gone/SKILL.md").write_text(stamped("---\nname: report-gone\ndescription: Gone.\n---\n"), encoding="utf-8")
    _, run = runner()
    assert check(repo, [FILES], run=run) == 1
    assert errors(capsys) == ["ERROR: AGENTS.md, .agents/skills/report-gone do not match what the installed basis writes: run `uv run basis sync`, which rewrites or removes each"]


def test_a_file_a_part_makes_that_is_out_of_date_fails_and_says_to_sync(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def document(version: str) -> str:
        return f'{{\n  "x-basis": "{sync.MARKER}",\n  "openapi": "{version}"\n}}\n'

    sync.write(repo, [AgentFiles("basis-alpha", "the alpha block\n", {}, made={"openapi.json": lambda: document("3.1.0")})])
    _, run = runner()
    assert check(repo, [AgentFiles("basis-alpha", "the alpha block\n", {}, made={"openapi.json": lambda: document("3.2.0")})], run=run) == 1
    assert errors(capsys) == ["ERROR: openapi.json does not match what the installed basis writes: run `uv run basis sync`, which rewrites or removes each"]


def test_a_file_a_part_cannot_make_fails_and_every_other_step_still_runs(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def fails() -> str:
        msg = "service accruals does not import"
        raise sync.SyncError(msg, "fix it")

    ran, run = runner()
    assert check(repo, [AgentFiles("basis-alpha", "the alpha block\n", {}, made={"openapi.json": fails})], run=run) == 1
    assert len(ran) == 5
    assert errors(capsys) == ["ERROR: service accruals does not import: fix it"]


def test_a_file_whose_maker_raises_an_import_error_fails_and_every_other_step_still_runs(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def fails() -> str:
        msg = "No module named 'accruals'"
        raise ImportError(msg)

    ran, run = runner()
    assert check(repo, [AgentFiles("basis-alpha", "the alpha block\n", {}, made={"openapi.json": fails})], run=run) == 1
    assert len(ran) == 5
    lines = capsys.readouterr().err.splitlines()  # the traceback's record first, where logging is set up
    assert [line for line in lines if line.startswith("ERROR")] == [
        "ERROR: openapi.json could not be made, ImportError: No module named 'accruals': a defect; the log record above holds its traceback"
    ]


def test_claude_settings_basis_cannot_read_fail_and_every_other_step_still_runs(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / ".claude/settings.json").write_text("{", encoding="utf-8")
    ran, run = runner()
    assert check(repo, [FILES], run=run) == 1
    assert len(ran) == 5
    [line] = errors(capsys)
    assert line.startswith(f"ERROR: {repo / '.claude/settings.json'} is not JSON, so basis cannot add its rules to it (")
    assert line.endswith("): fix it, or remove it, and run `uv run basis sync`")


def test_claude_settings_basis_cannot_read_hide_no_other_drift(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / ".claude/settings.json").write_text("{", encoding="utf-8")
    (repo / ".githooks/pre-push").write_text("#!/bin/sh\n", encoding="utf-8")
    _, run = runner()
    assert check(repo, [FILES], run=run) == 1
    assert [line.split(" ")[1] for line in errors(capsys)] == [f"{repo / '.claude/settings.json'}", ".githooks/pre-push"]


def test_files_of_the_repos_own_at_paths_basis_writes_fail_and_say_to_adopt_them(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "Dockerfile").write_text("FROM python:3.14-slim\n", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("Read AGENTS.md.\n", encoding="utf-8")
    _, run = runner()
    assert check(repo, [FILES], run=run) == 1
    adopt = "run `uv run basis sync --adopt`, which saves each beside itself as NAME.before-basis, for you to compare and delete, and writes basis's"
    assert errors(capsys) == [f"ERROR: Dockerfile, CLAUDE.md are the repo's own, at paths basis writes, so basis leaves them alone: {adopt}"]


def test_staged_checks_the_files_staged_for_a_commit_first(repo: Path, git: Git, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "deploy.toml").write_text("[broken\n", encoding="utf-8")
    git(repo, "add", "deploy.toml")
    ran, run = runner()
    assert check(repo, [FILES], run=run) == 0
    assert check(repo, [FILES], run=run, staged=True) == 1
    assert len(ran) == 10
    [line] = errors(capsys)
    assert line.startswith("ERROR: deploy.toml is not TOML (")


def test_check_points_a_clone_that_names_no_hooks_folder_at_the_repos_hooks(tmp_path: Path, git: Git, capsys: pytest.CaptureFixture[str]) -> None:
    sync.write(tmp_path, [FILES])
    git(tmp_path, "init", "--quiet")
    _, run = runner()
    assert check(tmp_path, [FILES], run=run) == 0
    assert git(tmp_path, "config", "core.hooksPath") == ".githooks\n"
    assert not [line for line in capsys.readouterr().err.splitlines() if line.startswith("ERROR")]  # its log record may be there


def test_a_clone_that_runs_other_hooks_fails_and_says_how_to_run_the_repos(repo: Path, git: Git, capsys: pytest.CaptureFixture[str]) -> None:
    git(repo, "config", "core.hooksPath", "elsewhere")
    ran, run = runner()
    assert check(repo, [FILES], run=run) == 1
    assert len(ran) == 5
    assert errors(capsys) == ["ERROR: git runs this clone's hooks from elsewhere, not .githooks/, so the repo's commit and push hooks never run: run `git config core.hooksPath .githooks`"]
