"""Mutation testing in `basis check --full`: the domain functions a change touches, against the commit it branched
from, and mutmut's run on them in a copy of the repo, which says which mutants survived and leaves the repo as it was.
The runs of mutmut itself are Linux's alone, as they are a repo's."""

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from basis.core import clone, mutation

type Git = Callable[..., str]
"""conftest's `git` fixture."""

PYPROJECT = """[project]
name = "rates"
version = "0.1.0"

[tool.basis]
parts = ["alpha"]
coverage = 90

[tool.pytest.ini_options]
addopts = "-ra --import-mode=importlib -p no:randomly"
testpaths = ["rates"]

[tool.mutmut]
source_paths = ["elsewhere"]
"""
DOMAIN = """def add(a: int, b: int) -> int:
    return a + b


def clamp(x: int, low: int, high: int) -> int:
    if x < low:
        return low
    return x


class Rate:
    def doubled(self, n: int) -> int:
        return n * 2
"""
TESTS = """from rates import domain


def test_add() -> None:
    assert domain.add(2, 3) == 5


def test_clamp() -> None:
    assert domain.clamp(5, 0, 10) == 5
    assert domain.clamp(-1, 0, 10) == 0
"""
linux = pytest.mark.skipif(sys.platform != "linux", reason="mutmut runs in WSL or on Linux only")


def write(repo: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def commit(repo: Path, git: Git, message: str) -> None:
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "--no-verify", "--message", message)


@pytest.fixture
def repo(tmp_path: Path, git: Git) -> Path:
    """A clone whose first commit holds a domain module and its tests."""
    folder = tmp_path / "rates-repo"
    write(folder, {"pyproject.toml": PYPROJECT, "rates/__init__.py": "", "rates/domain.py": DOMAIN, "rates/tests/__init__.py": "", "rates/tests/test_domain.py": TESTS})
    git(folder, "init", "--quiet", "--initial-branch", "main")
    commit(folder, git, "First commit")
    return folder


def touched(repo: Path) -> list[str]:
    found = clone.of(repo)
    assert found is not None
    return [function.qualified for function in mutation.touched(repo, found, mutation._base(found))]  # noqa: SLF001 -- the base mutate measures from


def test_a_first_commit_touches_every_domain_function(repo: Path) -> None:
    assert touched(repo) == ["rates.domain.add", "rates.domain.clamp", "rates.domain.Rate.doubled"]


def test_a_commit_touches_the_functions_whose_lines_it_changes(repo: Path, git: Git) -> None:
    write(repo, {"rates/domain.py": DOMAIN.replace("return n * 2", "return n + n").replace("if x < low:", "if x <= low:")})
    commit(repo, git, "Change two")
    assert touched(repo) == ["rates.domain.clamp", "rates.domain.Rate.doubled"]


def test_a_change_not_yet_committed_counts(repo: Path, git: Git) -> None:
    write(repo, {"README.md": "Rates.\n"})
    commit(repo, git, "Say what it is")
    write(repo, {"rates/domain.py": DOMAIN.replace("return a + b", "return b + a")})
    assert touched(repo) == ["rates.domain.add"]


def test_a_removed_line_touches_its_function(repo: Path, git: Git) -> None:
    write(repo, {"rates/domain.py": DOMAIN.replace("    if x < low:\n        return low\n", "")})
    commit(repo, git, "Remove the floor")
    assert touched(repo) == ["rates.domain.clamp"]


def test_a_deleted_function_touches_none_of_its_neighbours(repo: Path, git: Git) -> None:
    write(repo, {"rates/domain.py": DOMAIN.replace("\n\nclass Rate:\n    def doubled(self, n: int) -> int:\n        return n * 2\n", "")})
    commit(repo, git, "Remove Rate")
    assert touched(repo) == []  # git puts the deletion right after clamp's last line, which it leaves alone


def test_a_function_s_deleted_tail_touches_it(repo: Path, git: Git) -> None:
    write(repo, {"rates/domain.py": DOMAIN.replace("        return low\n    return x\n", "        return low\n")})
    commit(repo, git, "Fall through")
    assert touched(repo) == ["rates.domain.clamp"]


def test_the_change_is_everything_since_the_branch_left_the_remotes_default(repo: Path, git: Git) -> None:
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    write(repo, {"rates/domain.py": DOMAIN.replace("return a + b", "return b + a")})
    commit(repo, git, "Change add")
    write(repo, {"rates/domain.py": DOMAIN.replace("return a + b", "return b + a").replace("return n * 2", "return n + n")})
    commit(repo, git, "Change doubled")
    assert touched(repo) == ["rates.domain.add", "rates.domain.Rate.doubled"]


def test_a_new_domain_module_is_touched_whole(repo: Path, git: Git) -> None:
    write(repo, {"rates/bonds/__init__.py": "", "rates/bonds/domain/__init__.py": "def coupon(rate: float) -> float:\n    return rate / 2\n"})
    commit(repo, git, "Add bonds")
    write(repo, {"rates/swaps/__init__.py": "", "rates/swaps/domain.py": "def leg(rate: float) -> float:\n    return -rate\n"})  # untracked
    assert touched(repo) == ["rates.bonds.domain.coupon", "rates.swaps.domain.leg"]


def test_a_change_elsewhere_touches_no_domain_function(repo: Path, git: Git) -> None:
    write(repo, {"rates/handlers.py": "def nothing() -> None:\n    pass\n", "rates/tests/test_domain.py": TESTS + "\n"})
    commit(repo, git, "Add a handler")
    assert touched(repo) == []


def test_the_names_of_a_functions_mutants_are_mutmuts() -> None:
    assert mutation.Function("rates.domain", "rates/domain.py", "clamp", (5, 8)).mutants == "rates.domain.x_clamp__mutmut_*"
    assert mutation.Function("rates.domain", "rates/domain.py", "Rate.doubled", (12, 13)).mutants == "rates.domain.xǁRateǁdoubled__mutmut_*"


def test_mutation_testing_is_skipped_where_mutmut_cannot_fork(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mutation.sys, "platform", "win32")
    assert mutation.mutate(repo).skipped == "mutmut runs in WSL or on Linux only, since it forks a process for each mutant: push from WSL to have it run"


@linux
def test_mutation_testing_is_skipped_when_the_change_touches_no_domain_function(repo: Path, git: Git) -> None:
    write(repo, {"rates/handlers.py": "def nothing() -> None:\n    pass\n"})
    commit(repo, git, "Add a handler")
    assert mutation.mutate(repo).skipped == "the change touches no domain function"


@linux
def test_mutation_testing_says_how_to_install_mutmut(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mutation, "find_spec", lambda _: None)
    found = mutation.mutate(repo)
    assert found.skipped == "mutmut is not installed: add `mutmut>=3.8; sys_platform == 'linux'` to [dependency-groups] dev, and run `uv lock`"
    assert found.functions == ["rates.domain.add", "rates.domain.clamp", "rates.domain.Rate.doubled"]


@linux
@pytest.mark.heavy
def test_mutmut_says_which_mutants_survived_and_leaves_the_repo_as_it_was(repo: Path, git: Git) -> None:
    found = mutation.mutate(repo)
    assert (found.skipped, found.failure, found.base) == (None, None, None)
    assert found.mutants == found.killed + found.survived + sum(found.unsettled.values())
    assert found.unsettled == {"no tests": 2}  # `doubled`, which no test calls
    assert found.survived == 1
    [survivor] = found.shown
    assert (survivor.name, survivor.function, survivor.change) == ("rates.domain.x_clamp__mutmut_1", "rates.domain.clamp", "if x < low: -> if x <= low:")
    assert git(repo, "status", "--porcelain", "--untracked-files=all") == ""


def test_what_each_exit_code_means_is_mutmut_s_own() -> None:
    from mutmut.stats import status_by_exit_code  # noqa: PLC0415 -- mutmut is this workspace's development dependency, never basis-core's

    theirs = {code: status for code, status in status_by_exit_code.items() if code != mutation.SURVIVED and code not in mutation.KILLED and status != "suspicious"}
    assert theirs | {2: "interrupted"} == mutation.STATUSES  # mutmut's "check was interrupted by user", in a word
