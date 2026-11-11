import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from basis.core import gates, hooks, sync
from basis.core.check import Runner, check, run_command
from basis.core.errors import BasisError
from basis.core.sync import AgentFiles, stamped

type Git = Callable[..., str]
"""conftest's `git` fixture."""

FILES = AgentFiles("basis-alpha", "the alpha block\n", {})
PYPROJECT = '[tool.basis]\nparts = ["alpha"]\ncoverage = 90\n'


def runner(*failing: str, covered: float = 100.0, sockets: Mapping[str, Sequence[str]] | None = None, failed: Sequence[str] = ()) -> tuple[list[tuple[str, ...]], Runner]:
    """A runner that fails the steps whose command holds a word of `failing`, and the commands it was given; its
    pytest writes what pytest-cov and basis's plugin write, `covered` percent of the code, each test in `sockets` with
    what it reached, and each test in `failed`."""
    ran: list[tuple[str, ...]] = []

    def run(argv: Sequence[str], _folder: Path) -> int:
        ran.append(tuple(argv))
        if "pytest" in argv:
            written(argv, covered=covered, sockets=sockets or {}, failed=failed)
        return 1 if set(argv) & set(failing) or ("pytest" in argv and failed) else 0

    return ran, run


def written(argv: Sequence[str], *, covered: float, sockets: Mapping[str, Sequence[str]], failed: Sequence[str]) -> None:
    """Write what a pytest run that `basis check` makes writes for its gates."""
    for argument in argv:
        if argument.startswith("--cov-report=json:"):
            Path(argument.removeprefix("--cov-report=json:")).write_text(json.dumps({"totals": {"percent_covered": covered}}), encoding="utf-8")
        elif argument.startswith("--basis-gates="):
            found = {"exit_status": 1 if failed else 0, "failed": list(failed), "sockets": {test: list(reached) for test, reached in sockets.items()}}
            Path(argument.removeprefix("--basis-gates=")).write_text(json.dumps(found), encoding="utf-8")


def errors(capsys: pytest.CaptureFixture[str]) -> list[str]:
    lines = capsys.readouterr().err.splitlines()
    assert all(line.startswith("ERROR: ") for line in lines)
    return lines


@pytest.fixture
def repo(tmp_path: Path, git: Git) -> Path:
    """A clone whose files are what basis writes, and which runs its hooks, with a coverage floor."""
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
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
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
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


def test_pytest_measures_coverage_and_says_where_basiss_plugin_writes(repo: Path) -> None:
    ran, run = runner()
    assert check(repo, [FILES], run=run) == 0
    pytest_argv = ran[-1]
    assert pytest_argv[:6] == ("uv", "run", "--locked", "--quiet", "pytest", "-q")
    assert [argument.partition("=")[0] for argument in pytest_argv[6:]] == ["--basis-gates", "--cov", "--cov-config", "--cov-report", "--cov-report"]


def test_coverage_under_the_floor_fails_with_its_line_alone(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, run = runner(covered=89.99)
    assert check(repo, [FILES], run=run) == 1
    reason = "the unit tests cover 89.9% of the code's lines and branches, under the floor of 90% that [tool.basis] coverage gives"
    fix = "test what the coverage report above lists as missing; a repository module counts for nothing here, since the integration tier covers it"
    assert errors(capsys) == [f"ERROR: {reason}: {fix}"]


def test_a_repo_without_a_floor_fails_and_its_tests_still_run_unmeasured(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "pyproject.toml").write_text('[tool.basis]\nparts = ["alpha"]\n', encoding="utf-8")
    ran, run = runner()
    assert check(repo, [FILES], run=run) == 1
    assert [argument.partition("=")[0] for argument in ran[-1][6:]] == ["--basis-gates"]
    fix = "add `coverage = 90` to it, the percentage of the code's lines and branches its unit tests must cover, as every generator writes it"
    assert errors(capsys) == [f"ERROR: pyproject.toml's [tool.basis] gives no coverage floor: {fix}"]


def test_a_repo_without_pytest_cov_is_told_to_add_it(repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gates, "find_spec", lambda _: None)
    _, run = runner()
    assert check(repo, [FILES], run=run) == 1
    reason = "pytest-cov is not installed, and basis check measures the unit tests' coverage with it"
    assert errors(capsys) == [f"ERROR: {reason}: add pytest-cov to [dependency-groups] dev, and run `uv lock`"]


def test_a_unit_test_that_opened_a_socket_is_its_line_and_pytests_own_is_left_out(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    test = "tests/test_prices.py::test_reaches_qa"
    _, run = runner(sockets={test: ["connect 10.0.0.5:1433"]}, failed=[test])
    assert check(repo, [FILES], run=run) == 1
    [line] = errors(capsys)
    assert line.startswith(f"ERROR: {test} opened a socket (connect 10.0.0.5:1433), and a unit test reaches nothing outside its process: give it a fake")


def test_a_test_that_mocks_fails_with_the_fix(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "tests").mkdir()
    (repo / "tests" / "test_prices.py").write_text("from unittest import mock\n", encoding="utf-8")
    _, run = runner()
    assert check(repo, [FILES], run=run) == 1
    [line] = errors(capsys)
    assert line.startswith("ERROR: tests/test_prices.py:1 uses unittest.mock, and a test's only double is a fake: ")


def test_each_parts_checks_are_lines_of_their_own_and_a_defect_in_one_leaves_the_rest_running(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def broken(_repo: Path) -> list[tuple[str, str]]:
        msg = "no graph"
        raise RuntimeError(msg)

    def refusing(_repo: Path) -> list[tuple[str, str]]:
        msg = "api/service.py does not load: fix it"
        raise BasisError(msg)

    checks = [("alpha", lambda found: [(f"{found.name}'s layers are upside down", "turn them over")]), ("beta", broken), ("gamma", refusing)]
    ran, run = runner()
    assert check(repo, [FILES], run=run, checks=checks) == 1
    assert len(ran) == 5
    lines = [line for line in capsys.readouterr().err.splitlines() if line.startswith("ERROR")]
    assert lines == [
        f"ERROR: {repo.name}'s layers are upside down: turn them over",
        "ERROR: the beta part's checks could not run, RuntimeError: no graph: a defect; the log record above holds its traceback",
        "ERROR: api/service.py does not load: fix it",
    ]


def test_a_command_basis_runs_never_sees_the_callers_coverage_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]) -> None:
    """coverage.py's variables outrank the settings basis measures by, so `basis check` run under coverage wrote its
    repo's data into the caller's file."""
    monkeypatch.setenv("COVERAGE_FILE", str(tmp_path / ".coverage.caller"))
    monkeypatch.setenv("COVERAGE_PROCESS_START", str(tmp_path / ".coveragerc"))
    monkeypatch.setenv("BASIS_KEPT", "yes")
    shown = "import os; print(sorted(name for name in os.environ if name.startswith(('COVERAGE_', 'BASIS_KEPT'))))"
    assert run_command([sys.executable, "-c", shown], tmp_path) == 0
    assert capfd.readouterr().out == "['BASIS_KEPT']\n"
