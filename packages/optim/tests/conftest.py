"""The core's tests run pytest itself, in throwaway model repos, to test the plugin (`test_plugin.py`)."""

import sys
from collections.abc import Generator
from pathlib import Path

import cvxpy  # noqa: F401 -- numpy's extensions load once per process: loaded here, before a pytester run unloads what it imports
import pytest

pytest_plugins = ("pytester",)


@pytest.fixture
def on_path(monkeypatch: pytest.MonkeyPatch) -> Generator[list[str]]:
    """`sys.path`, restored after the test, with the modules imported from the paths the test added dropped: a generated
    repo's own, since another test's repo of the same name is another package. A library it imported stays, as numpy's
    extensions must, which load once per process."""
    before = list(sys.path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    yield sys.path
    added = [Path(path) for path in sys.path if path not in before]
    for name, module in list(sys.modules.items()):
        if (file := getattr(module, "__file__", None)) and any(Path(file).is_relative_to(path) for path in added):
            del sys.modules[name]
