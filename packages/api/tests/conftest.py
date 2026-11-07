import io
import json
import logging
import os
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from basis.core.logs import JsonFormatter

type Records = Callable[[], list[dict[str, object]]]
type Git = Callable[..., str]
"""Runs `git ARGV...` in a folder and returns what it prints; fails the test when git does."""


@pytest.fixture
def logged() -> Iterator[Records]:
    """The records logged during the test, each as the JSON line basis-core writes, with basis's loggers at INFO."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root, framework = logging.getLogger(), logging.getLogger("basis")
    level = framework.level
    root.addHandler(handler)
    framework.setLevel(logging.INFO)
    try:
        yield lambda: [_record(line) for line in stream.getvalue().splitlines()]
    finally:
        root.removeHandler(handler)
        framework.setLevel(level)


def _record(line: str) -> dict[str, object]:
    record: object = json.loads(line)
    assert isinstance(record, dict)
    return {str(key): value for key, value in record.items()}


@pytest.fixture
def logging_restored() -> Iterator[None]:
    """The process's logging as it was, once the test ends: `basis` run in the test's process configures logging to
    write to the test's captured stderr, which closes with the test."""
    root, framework = logging.getLogger(), logging.getLogger("basis")
    handlers, level, framework_level = root.handlers[:], root.level, framework.level
    try:
        yield
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)
        framework.setLevel(framework_level)


@pytest.fixture
def git(monkeypatch: pytest.MonkeyPatch) -> Git:
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
