import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from basis.core import hooks, sync

type Git = Callable[..., str]
"""conftest's `git` fixture."""
GIT = shutil.which("git") or "git"
"""git, for a command that is meant to fail; each test that runs one takes the `git` fixture, which skips it without git."""

UV = """#!/bin/sh
# A stand-in for uv: it records each command, "formats" each Python file it is given, and passes `basis check` unless
# CHECK fails it.
printf '%s\\n' "$*" >> "$UV_LOG"
case "$*" in
    *"ruff format"*) for file in "$@"; do case "$file" in *.py) printf '# formatted\\n' >> "$file" ;; esac; done ;;
    *"basis check"*) exit "${CHECK:-0}" ;;
esac
"""


@pytest.fixture
def clone(tmp_path: Path, git: Git) -> Path:
    """A clone of a repo with basis's files, not yet pointed at its hooks."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sync.write(repo, [])
    git(repo, "init", "--quiet", "--initial-branch=main")
    return repo


def test_ensure_points_a_clone_that_names_no_hooks_folder_at_the_repos(clone: Path, git: Git) -> None:
    assert hooks.problem(clone) == ("git runs this clone's hooks from .git/hooks, not .githooks/, so the repo's commit and push hooks never run", "run `git config core.hooksPath .githooks`")
    hooks.ensure(clone)
    assert git(clone, "config", "core.hooksPath") == ".githooks\n"
    assert hooks.problem(clone) is None


def test_ensure_leaves_a_hooks_folder_a_setting_already_names(clone: Path, git: Git) -> None:
    git(clone, "config", "core.hooksPath", "elsewhere")
    hooks.ensure(clone)
    assert git(clone, "config", "core.hooksPath") == "elsewhere\n"
    assert hooks.problem(clone) == ("git runs this clone's hooks from elsewhere, not .githooks/, so the repo's commit and push hooks never run", "run `git config core.hooksPath .githooks`")


def test_a_repo_in_a_folder_of_its_clone_points_git_at_its_own_hooks(tmp_path: Path, git: Git) -> None:
    git(tmp_path, "init", "--quiet")
    repo = tmp_path / "services" / "pricing"
    repo.mkdir(parents=True)
    sync.write(repo, [])
    hooks.ensure(repo)
    assert git(tmp_path, "config", "core.hooksPath") == "services/pricing/.githooks\n"
    assert hooks.problem(repo) is None


def test_ensure_waits_for_the_hooks_to_be_written(tmp_path: Path, git: Git) -> None:
    git(tmp_path, "init", "--quiet")
    hooks.ensure(tmp_path)
    assert git(tmp_path, "config", "--default", "unset", "core.hooksPath") == "unset\n"


def test_ensure_writes_the_setting_of_the_clone_the_repo_is_in_not_one_a_hook_exports(clone: Path, tmp_path: Path, git: Git, monkeypatch: pytest.MonkeyPatch) -> None:
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "--quiet")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    hooks.ensure(clone)
    monkeypatch.delenv("GIT_DIR")
    assert git(clone, "config", "core.hooksPath") == ".githooks\n"
    assert git(other, "config", "--default", "unset", "core.hooksPath") == "unset\n"


def test_outside_a_clone_ensure_does_nothing_and_problem_says_to_make_one(tmp_path: Path) -> None:
    sync.write(tmp_path, [])
    hooks.ensure(tmp_path)
    assert hooks.problem(tmp_path) == (f"{tmp_path} is in no git clone, so its commit and push hooks never run", "run `git init` in it, or work in a clone of the repo")


def test_without_git_ensure_does_nothing_and_problem_says_to_install_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sync.write(tmp_path, [])
    monkeypatch.setenv("PATH", str(tmp_path / "no-tools"))
    hooks.ensure(tmp_path)
    assert hooks.problem(tmp_path) == ("git is not on the PATH, so this repo's commit and push hooks cannot run", "install git, and put it on the PATH")


@pytest.fixture
def hooked(clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`clone`, pointed at its hooks, with the stand-in uv first on the PATH; it logs to `uv.log` beside the clone."""
    if os.name == "nt":
        pytest.skip("the hooks run in Git for Windows' sh, which these tests do not set up")
    hooks.ensure(clone)
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "uv").write_text(UV, encoding="utf-8")
    (tools / "uv").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("UV_LOG", str(tmp_path / "uv.log"))
    return clone


def committed(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([GIT, "commit", "--quiet", "--message", "a change"], cwd=repo, capture_output=True, text=True, check=False)


def uv_ran(repo: Path) -> list[str]:
    log = repo.parent / "uv.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_the_commit_hook_fixes_and_formats_the_staged_python_files_stages_them_again_and_runs_basis_check(hooked: Path, git: Git) -> None:
    (hooked / "jobs").mkdir()
    (hooked / "jobs/prices.py").write_text("x = 1\n", encoding="utf-8")
    (hooked / "notes.md").write_text("# Notes\n", encoding="utf-8")
    git(hooked, "add", "--all")
    assert committed(hooked).returncode == 0
    assert uv_ran(hooked) == [
        "run --frozen --quiet ruff check --fix --exit-zero --silent -- jobs/prices.py",
        "run --frozen --quiet ruff format --silent -- jobs/prices.py",
        "run --frozen --quiet basis check",
    ]
    assert git(hooked, "show", "HEAD:jobs/prices.py") == "x = 1\n# formatted\n"
    assert git(hooked, "status", "--porcelain") == ""


def test_a_commit_of_no_python_files_runs_only_basis_check(hooked: Path, git: Git) -> None:
    git(hooked, "add", "--all")
    assert committed(hooked).returncode == 0
    assert uv_ran(hooked) == ["run --frozen --quiet basis check"]


def test_a_failing_basis_check_stops_the_commit(hooked: Path, git: Git, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHECK", "1")
    git(hooked, "add", "--all")
    assert committed(hooked).returncode == 1
    assert git(hooked, "rev-list", "--all") == ""


def test_a_python_file_changed_since_it_was_staged_stops_the_commit_before_ruff_touches_it(hooked: Path, git: Git) -> None:
    (hooked / "prices.py").write_text("x = 1\n", encoding="utf-8")
    git(hooked, "add", "--all")
    (hooked / "prices.py").write_text("x = 2\n", encoding="utf-8")
    found = committed(hooked)
    assert found.returncode == 1
    assert found.stderr.splitlines() == [
        "ERROR: prices.py also changed since it was staged, and the hook would stage that with ruff's fixes: stage all of it, or stash the rest with `git stash push --keep-index`, and commit again"
    ]
    assert uv_ran(hooked) == []


def test_staged_whitespace_errors_stop_the_commit(hooked: Path, git: Git) -> None:
    (hooked / "notes.md").write_text("# Notes   \n", encoding="utf-8")
    git(hooked, "add", "--all")
    found = committed(hooked)
    assert found.returncode == 1
    assert found.stderr.splitlines()[0] == "notes.md:1: trailing whitespace."
    assert found.stderr.splitlines()[-1].startswith("ERROR: the staged changes add the whitespace errors or conflict markers listed above")


@pytest.mark.parametrize(("check", "pushed"), [("0", True), ("1", False)])
def test_the_push_hook_runs_basis_check_and_a_failure_stops_the_push(hooked: Path, git: Git, monkeypatch: pytest.MonkeyPatch, check: str, *, pushed: bool) -> None:
    git(hooked, "add", "--all")
    assert committed(hooked).returncode == 0
    remote = hooked.parent / "remote.git"
    git(hooked.parent, "init", "--quiet", "--bare", remote.name)
    monkeypatch.setenv("CHECK", check)
    push = subprocess.run([GIT, "push", "--quiet", str(remote), "main"], cwd=hooked, capture_output=True, check=False)
    assert (push.returncode == 0) is pushed
    assert uv_ran(hooked)[-1] == "run --frozen --quiet basis check"
    assert (git(remote, "rev-list", "--all") != "") is pushed
