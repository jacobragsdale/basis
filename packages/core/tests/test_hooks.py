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
# CHECK fails it, recording the folder and the commit it checked.
printf '%s\\n' "$*" >> "$UV_LOG"
case "$*" in
    *"ruff format"*) for file in "$@"; do case "$file" in *.py) sed -i 's/[[:space:]]*$//' "$file"; printf '# formatted\\n' >> "$file" ;; esac; done ;;
    *"basis check"*) printf '%s %s\\n' "$(pwd -W 2>/dev/null || pwd)" "$(git rev-parse --verify --quiet HEAD || echo none)" >> "$UV_LOG.checks"; exit "${CHECK:-0}" ;;
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
    return stand_in_uv(clone, tmp_path, monkeypatch)


def stand_in_uv(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    hooks.ensure(repo)
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "uv").write_text(UV, encoding="utf-8")
    (tools / "uv").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("UV_LOG", str(tmp_path / "uv.log"))
    return repo


def committed(repo: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([GIT, "commit", "--quiet", "--message", "a change", *argv], cwd=repo, capture_output=True, text=True, check=False)


def uv_ran(tmp_path: Path) -> list[str]:
    log = tmp_path / "uv.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def checks(tmp_path: Path) -> list[tuple[Path, str]]:
    """Each `basis check` the hooks ran: the folder it ran in, and the commit checked out there."""
    log = tmp_path / "uv.log.checks"
    return [(Path(folder), commit) for folder, commit in (line.rsplit(" ", 1) for line in log.read_text(encoding="utf-8").splitlines())] if log.exists() else []


RUFF = ["run --frozen --quiet ruff check --fix --force-exclude --exit-zero --silent -- jobs/prices.py", "run --frozen --quiet ruff format --force-exclude --silent -- jobs/prices.py"]


def test_the_commit_hook_fixes_and_formats_the_staged_python_files_stages_them_again_and_runs_basis_check(hooked: Path, tmp_path: Path, git: Git) -> None:
    (hooked / "jobs").mkdir()
    (hooked / "jobs/prices.py").write_text("x = 1\n", encoding="utf-8")
    (hooked / "notes.md").write_text("# Notes\n", encoding="utf-8")
    git(hooked, "add", "--all")
    assert committed(hooked).returncode == 0
    assert uv_ran(tmp_path) == [*RUFF, "run --frozen --quiet basis check --staged"]
    assert git(hooked, "show", "HEAD:jobs/prices.py") == "x = 1\n# formatted\n"
    assert git(hooked, "status", "--porcelain") == ""


def test_the_commit_hook_of_a_repo_in_a_folder_of_its_clone_fixes_its_staged_files(tmp_path: Path, git: Git, monkeypatch: pytest.MonkeyPatch) -> None:
    top = tmp_path / "clone"
    repo = top / "services" / "prices"
    repo.mkdir(parents=True)
    git(top, "init", "--quiet")
    sync.write(repo, [])
    stand_in_uv(repo, tmp_path, monkeypatch)
    (repo / "jobs").mkdir()
    (repo / "jobs/prices.py").write_text("x = 1\n", encoding="utf-8")
    git(top, "add", "--all")
    assert committed(top).returncode == 0
    assert uv_ran(tmp_path)[:2] == RUFF
    assert git(top, "show", "HEAD:services/prices/jobs/prices.py") == "x = 1\n# formatted\n"


def test_a_commit_of_no_python_files_runs_only_basis_check(hooked: Path, tmp_path: Path, git: Git) -> None:
    git(hooked, "add", "--all")
    assert committed(hooked).returncode == 0
    assert uv_ran(tmp_path) == ["run --frozen --quiet basis check --staged"]


def test_a_failing_basis_check_stops_the_commit(hooked: Path, git: Git, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHECK", "1")
    git(hooked, "add", "--all")
    assert committed(hooked).returncode == 1
    assert git(hooked, "rev-list", "--all") == ""


def test_a_python_file_changed_since_it_was_staged_stops_the_commit_before_ruff_touches_it(hooked: Path, tmp_path: Path, git: Git) -> None:
    (hooked / "prices.py").write_text("x = 1\n", encoding="utf-8", newline="")  # LF, as .gitattributes keeps it, on Windows too
    git(hooked, "add", "--all")
    (hooked / "prices.py").write_text("x = 2\n", encoding="utf-8", newline="")
    found = committed(hooked)
    assert found.returncode == 1
    assert found.stderr.splitlines() == [
        "ERROR: prices.py also changed since it was staged, and the hook would stage that with ruff's fixes: stage all of it, or stash the rest with `git stash push --keep-index`, and commit again"
    ]
    assert uv_ran(tmp_path) == []


def test_staged_whitespace_errors_stop_the_commit(hooked: Path, git: Git) -> None:
    (hooked / "notes.md").write_text("# Notes   \n", encoding="utf-8")
    git(hooked, "add", "--all")
    found = committed(hooked)
    assert found.returncode == 1
    assert found.stderr.splitlines()[0] == "notes.md:1: trailing whitespace."
    assert found.stderr.splitlines()[-1].startswith("ERROR: the staged changes add the whitespace errors or conflict markers listed above")


@pytest.fixture
def pushing(hooked: Path, tmp_path: Path, git: Git) -> Path:
    """`hooked` with two commits on main, a branch `older` at the first, and a bare remote beside it."""
    git(hooked, "add", "--all")
    assert committed(hooked, "--no-verify").returncode == 0
    git(hooked, "branch", "older")
    (hooked / "notes.md").write_text("# Notes\n", encoding="utf-8")
    git(hooked, "add", "--all")
    assert committed(hooked, "--no-verify").returncode == 0
    git(tmp_path, "init", "--quiet", "--bare", "remote.git")
    return hooked


def pushed(repo: Path, *refs: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([GIT, "push", "--quiet", str(repo.parent / "remote.git"), *refs], cwd=repo, capture_output=True, text=True, check=False)


def worktrees(repo: Path, git: Git) -> list[str]:
    """The clone's worktrees, and anything the push hook left beside it."""
    return [line for line in git(repo, "worktree", "list", "--porcelain").splitlines() if line.startswith("worktree ")] + [path.name for path in repo.parent.glob(f".{repo.name}.push.*")]


def test_the_push_hook_checks_head_in_full_in_place_when_the_tree_is_clean(pushing: Path, tmp_path: Path, git: Git) -> None:
    assert pushed(pushing, "main").returncode == 0
    assert checks(tmp_path) == [(pushing, git(pushing, "rev-parse", "HEAD").strip())]
    assert uv_ran(tmp_path) == ["run --frozen --quiet basis check --full"]


@pytest.mark.parametrize("dirty", ["a change", "a new file"])
def test_the_push_hook_checks_head_in_a_worktree_of_its_own_when_the_tree_has_changes(pushing: Path, tmp_path: Path, git: Git, dirty: str) -> None:
    (pushing / ("notes.md" if dirty == "a change" else "draft.md")).write_text("# Not yet\n", encoding="utf-8")
    assert pushed(pushing, "main").returncode == 0
    [(folder, commit)] = checks(tmp_path)
    assert folder.parent == pushing.parent
    assert folder.name.startswith(f".{pushing.name}.push.")
    assert commit == git(pushing, "rev-parse", "HEAD").strip()
    assert uv_ran(tmp_path) == [f"run --frozen --quiet basis check --full --settings-from {pushing.as_posix()}/"]  # its untracked files, such as .env.qa, are there
    assert worktrees(pushing, git) == [f"worktree {pushing.as_posix()}"]


def test_the_push_hook_checks_each_pushed_commit_once_and_skips_a_deletion(pushing: Path, tmp_path: Path, git: Git) -> None:
    assert pushed(pushing, "main", "older", "main:copy").returncode == 0
    found = checks(tmp_path)
    assert [commit for _, commit in found] == [git(pushing, "rev-parse", ref).strip() for ref in ("main", "older")]
    assert found[0][0] == pushing
    assert pushed(pushing, ":older", ":copy").returncode == 0
    assert len(checks(tmp_path)) == 2
    assert worktrees(pushing, git) == [f"worktree {pushing.as_posix()}"]


@pytest.mark.parametrize("ref", ["main", "older"])
def test_a_failing_check_stops_the_push_and_leaves_no_worktree(pushing: Path, tmp_path: Path, git: Git, monkeypatch: pytest.MonkeyPatch, ref: str) -> None:
    monkeypatch.setenv("CHECK", "1")
    found = pushed(pushing, ref)
    assert found.returncode == 1
    assert "ERROR: a commit being pushed fails basis check --full, as above: fix it, commit, and push again" in found.stderr.splitlines()  # before git's own line
    assert git(tmp_path / "remote.git", "rev-list", "--all") == ""
    assert worktrees(pushing, git) == [f"worktree {pushing.as_posix()}"]


def test_the_push_hook_of_a_repo_in_a_folder_of_its_clone_checks_that_folder_of_the_worktree(tmp_path: Path, git: Git, monkeypatch: pytest.MonkeyPatch) -> None:
    top = tmp_path / "clone"
    repo = top / "services" / "prices"
    repo.mkdir(parents=True)
    git(top, "init", "--quiet", "--initial-branch=main")
    sync.write(repo, [])
    stand_in_uv(repo, tmp_path, monkeypatch)
    git(top, "add", "--all")
    assert committed(top, "--no-verify").returncode == 0
    (repo / "draft.md").write_text("# Not yet\n", encoding="utf-8")
    git(tmp_path, "init", "--quiet", "--bare", "remote.git")
    assert subprocess.run([GIT, "push", "--quiet", str(tmp_path / "remote.git"), "main"], cwd=top, capture_output=True, check=False).returncode == 0
    [(folder, _)] = checks(tmp_path)
    assert folder.relative_to(folder.parents[1]) == Path("services/prices")
    assert folder.parents[1].name.startswith(".clone.push.")
    assert worktrees(top, git) == [f"worktree {top.as_posix()}"]


def test_whitespace_in_a_python_file_the_formatter_fixes_does_not_stop_the_commit(hooked: Path, git: Git) -> None:
    (hooked / "jobs").mkdir()
    (hooked / "jobs/prices.py").write_text("x = 1   \n", encoding="utf-8")
    git(hooked, "add", "--all")
    assert committed(hooked).returncode == 0
    assert git(hooked, "show", "HEAD:jobs/prices.py") == "x = 1\n# formatted\n"


def test_a_hook_staged_without_its_executable_bit_is_committed_with_it(hooked: Path, git: Git) -> None:
    """As Git for Windows stages a hook, whose file system keeps no executable bit: git in WSL then skips it."""
    git(hooked, "add", "--all")
    git(hooked, "update-index", "--chmod=-x", "--", ".githooks/pre-commit")
    assert committed(hooked).returncode == 0
    assert git(hooked, "ls-files", "-s", "--", ".githooks/pre-commit").split(" ")[0] == "100755"


def test_a_tag_of_a_commit_already_checked_is_not_checked_again(pushing: Path, tmp_path: Path, git: Git) -> None:
    head = git(pushing, "rev-parse", "HEAD").strip()
    record = Path(git(pushing, "rev-parse", "--git-common-dir").strip())
    record = (pushing / record if not record.is_absolute() else record) / "basis" / "checks" / f"{head}.json"
    record.parent.mkdir(parents=True)
    record.write_text('{\n  "passed": true\n}', encoding="utf-8")  # as basis check --full records a pass
    git(pushing, "tag", "--annotate", "--message", "a release", "v1.0.0")
    assert pushed(pushing, "v1.0.0").returncode == 0
    assert checks(tmp_path) == []


def test_a_hook_the_index_holds_without_its_executable_bit_is_named_with_the_fix(hooked: Path, git: Git) -> None:
    git(hooked, "add", "--all")
    assert hooks.unexecutable(hooked) == []
    git(hooked, "update-index", "--chmod=-x", "--", ".githooks/pre-push")
    assert hooks.unexecutable(hooked) == [
        (
            ".githooks/pre-push is committed without its executable bit, so git in WSL or on Linux skips it",
            "run `git update-index --chmod=+x -- .githooks/pre-push`, and commit, as the commit hook now does",
        )
    ]
