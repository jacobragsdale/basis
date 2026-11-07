from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from basis.core import upgrade

type Git = Callable[..., str]
"""conftest's `git` fixture."""

PACKAGES = {"basis-core", "basis-job", "basis-api", "basis-report"}  # the packages of basis-core and the installed parts
LOCK = """version = 1

[[package]]
name = "basis-core"
version = "{version}"
source = {{ registry = "https://pkgs.example.com/simple" }}

[[package]]
name = "basis-job"
version = "{version}"
source = {{ registry = "https://pkgs.example.com/simple" }}

[[package]]
name = "polars"
version = "2.1.0"
source = {{ registry = "https://pkgs.example.com/simple" }}
"""


PUSH = "basis upgrade: commit the upgrade and push it: the push hook runs the QA tier on it, and records the check for `uv run basis review`"


class Desktop:
    """uv and git as `upgrade` meets them: `uv lock` relocks to `newest`, and each other command exits with its code."""

    def __init__(self, repo: Path, *, newest: str, codes: dict[str, int] | None = None, dirty: Sequence[str] = ()) -> None:
        self.repo, self.newest, self.codes, self.dirty = repo, newest, codes or {}, list(dirty)
        self.ran: list[str] = []
        self.changed: list[str] = []

    def run(self, argv: Sequence[str], _folder: Path) -> int:
        command = " ".join(argv)
        self.ran.append(command)
        if argv[:2] == ["uv", "lock"] and (self.repo / "uv.lock").read_text(encoding="utf-8") != (text := LOCK.format(version=self.newest)):
            (self.repo / "uv.lock").write_text(text, encoding="utf-8")
            self.changed.append("uv.lock")
        if command.endswith("basis sync"):
            self.changed.append("AGENTS.md")
        return next((code for word, code in self.codes.items() if word in command), 0)

    def files(self, _repo: Path) -> list[str]:
        return self.dirty or self.changed


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "uv.lock").write_text(LOCK.format(version="0.1.0"), encoding="utf-8")
    return tmp_path


def upgraded(repo: Path, found: Desktop) -> tuple[int, list[str]]:
    said: list[str] = []
    return upgrade.upgrade(repo, PACKAGES, run=found.run, changed=found.files, write=said.append), said


def test_upgrade_relocks_basis_syncs_checks_in_full_and_says_what_changed(repo: Path) -> None:
    found = Desktop(repo, newest="0.1.1")
    code, said = upgraded(repo, found)
    assert code == 0
    assert found.ran == ["uv lock --quiet --upgrade-package basis-core --upgrade-package basis-job", "uv run --locked --quiet basis sync", "uv run --locked --quiet basis check --full --no-qa"]
    registry = "(registry https://pkgs.example.com/simple)"
    assert said == [f"basis upgrade: basis-core 0.1.0 {registry} -> 0.1.1 {registry}; basis-job 0.1.0 {registry} -> 0.1.1 {registry}", "basis upgrade: changed uv.lock, AGENTS.md", PUSH]


def test_upgrade_with_nothing_newer_says_so_and_still_syncs_and_checks(repo: Path) -> None:
    found = Desktop(repo, newest="0.1.0")
    code, said = upgraded(repo, found)
    assert (code, len(found.ran)) == (0, 3)
    assert said[0].startswith("basis upgrade: the lock already holds the newest basis pyproject.toml allows: basis-core 0.1.0")


def test_upgrade_refuses_a_folder_with_changes_no_commit_holds(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    found = Desktop(repo, newest="0.1.1", dirty=["jobs/prices/job.py"])
    assert upgraded(repo, found)[0] == 1
    assert found.ran == []
    assert capsys.readouterr().err == (
        "ERROR: the repo has changes no commit holds (jobs/prices/job.py), and basis upgrade's changes are a diff of their own: commit or stash them, and run `uv run basis upgrade` again\n"
    )


def test_a_repo_that_fails_its_full_check_after_the_upgrade_fails_and_says_how_to_set_it_aside(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, said = upgraded(repo, Desktop(repo, newest="0.1.1", codes={"check": 1}))
    assert code == 1
    assert said[-2:] == ["basis upgrade: changed uv.lock, AGENTS.md", PUSH]
    assert capsys.readouterr().err.endswith("fix what it says, or set the upgrade aside with `git stash --include-untracked`\n")


def test_the_files_an_upgrade_changed_are_named_from_the_repos_folder_as_git_names_them(tmp_path: Path, git: Git) -> None:
    git(tmp_path, "init", "--quiet")
    repo = tmp_path / "services" / "pricing"
    repo.mkdir(parents=True)
    (repo / "old.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "elsewhere.txt").write_text("b\n", encoding="utf-8")
    git(tmp_path, "add", "--all")
    git(tmp_path, "commit", "--quiet", "--no-verify", "--message", "First commit")
    assert upgrade.changed_files(repo) == []
    git(repo, "mv", "old.txt", "new name é.txt")
    (repo / 'quote"d.txt').write_text("c\n", encoding="utf-8")
    (tmp_path / "elsewhere.txt").write_text("changed\n", encoding="utf-8")
    assert sorted(upgrade.changed_files(repo) or []) == ["new name é.txt", 'quote"d.txt']
