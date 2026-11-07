import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

type Git = Callable[..., str]
"""Runs `git ARGV...` in a folder and returns what it prints; fails the test when git does."""


@pytest.fixture(autouse=True)
def fresh_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """git as a fresh machine has it, whatever runs the tests: no clone that a hook or `git rebase --exec` exports, so
    no test writes to the clone it runs in, no user or system settings, and an author for commits."""
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.com")


@pytest.fixture
def git() -> Git:
    found = shutil.which("git")
    if found is None:
        pytest.skip("git is not installed")

    def run(folder: Path, *argv: str) -> str:
        return subprocess.run([found, *argv], cwd=folder, check=True, capture_output=True, text=True).stdout

    return run
