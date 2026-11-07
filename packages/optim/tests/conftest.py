"""The core's tests run pytest itself, in throwaway model repos, to test the plugin (`test_plugin.py`), and run the
`basis` command in a repo that declares optim."""

import sys
from collections.abc import Generator
from pathlib import Path

import cvxpy  # noqa: F401 -- numpy's extensions load once per process: loaded here, before a pytester run unloads what it imports
import pytest
from fakes import Basis

from basis.core import command
from optim import cli

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


@pytest.fixture
def basis(tmp_path: Path) -> Basis:
    """`basis` in a model repo whose `pyproject.toml` declares optim alone, as `optim new repo` makes it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "repo"\n\n[tool.basis]\nparts = ["optim"]\n', encoding="utf-8")

    def run(*argv: str) -> int:
        return command.run(argv, cwd=repo, installed={"optim": cli.part})

    return run
