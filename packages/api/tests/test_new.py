"""`basis-api new app NAME` (step 2.7): an API repo in its layers, with the job part beside it. The installed repo,
which `basis check` and `basis check --full` passed on the development host, is left to a run of the command."""

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from basis.api import new
from basis.core import new as core_new

GIT = shutil.which("git") or "git"
ROOT = Path(__file__).parents[3]


def test_an_app_is_the_example_in_its_layers_named_for_it(tmp_path: Path) -> None:
    repo = new.app(tmp_path / "ledger-app", "ledger-app", "positions", index=None, framework_path=ROOT, install=False)
    package = repo / "ledger_app"
    assert sorted(path.name for path in package.glob("*.py")) == ["__init__.py", "compute.py", "dev.py", "domain.py", "errors.py", "handlers.py", "repository.py", "resources.py", "service.py"]
    assert not (package / "openapi.json").exists()  # basis sync writes the repo's at its root
    service = (package / "service.py").read_text(encoding="utf-8")
    assert 'name="ledger-app"' in service
    assert "basis.api.example" not in service
    assert 'fakes="ledger_app.dev:fakes"' in service
    project = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["tool"]["basis"] == {"parts": ["api", "job"], "coverage": 90}
    assert project["project"]["entry-points"]["basis.apis"] == {"ledger-app": "ledger_app.service:service"}
    assert project["tool"]["uv"]["build-backend"]["module-name"] == ["ledger_app", "jobs"]
    assert (repo / "jobs" / "__init__.py").exists()


@pytest.mark.parametrize("name", ["Ledger", "ledger_app", "1ledger"], ids=["upper-case", "underscore", "a-digit-first"])
def test_an_app_name_is_lower_case_letters_digits_and_hyphens(tmp_path: Path, name: str) -> None:
    with pytest.raises(core_new.NewError, match="is no app's name"):
        new.app(tmp_path / "x", name, None, index=None, framework_path=ROOT, install=False)


@pytest.mark.parametrize("name", ["yield", "class", "signal", "calendar", "jobs", "tests", "basis"])
def test_a_name_whose_package_cannot_be_imported_or_would_shadow_another_is_refused_with_nothing_written(tmp_path: Path, name: str) -> None:
    with pytest.raises(core_new.NewError, match=f"^the app's package {name!r} .*: give another name$"):
        new.app(tmp_path / name, name, None, index=None, framework_path=ROOT, install=False)
    assert list(tmp_path.iterdir()) == []


def test_an_app_copies_the_files_it_names_and_no_cache_or_stray_file_of_the_example(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    example = tmp_path / "example"
    shutil.copytree(new.EXAMPLE, example)
    (example / ".ruff_cache").mkdir(exist_ok=True)  # as a checkout's example holds once ruff has run in it
    (example / ".ruff_cache" / "CACHEDIR.TAG").write_text("", encoding="utf-8")
    (example / "notes.txt").write_text("a stray file\n", encoding="utf-8")
    monkeypatch.setattr(new, "EXAMPLE", example)
    repo = new.app(tmp_path / "ledger-app", "ledger-app", None, index=None, framework_path=ROOT, install=False)
    copied = sorted(path.relative_to(repo / "ledger_app").as_posix() for path in (repo / "ledger_app").rglob("*") if path.is_file())
    assert copied == sorted(new.EXAMPLE_FILES)


def test_the_files_an_app_copies_are_every_file_of_the_example_but_its_sample_and_settings() -> None:
    tracked = subprocess.run([GIT, "ls-files"], cwd=new.EXAMPLE, capture_output=True, text=True, check=True).stdout.split()
    assert sorted(tracked) == sorted([*new.EXAMPLE_FILES, "ruff.toml", "__main__.py", "openapi.json", "coupons.arrows", "tests/test_openapi.py"])
