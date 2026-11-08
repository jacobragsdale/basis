"""`optim new repo` and `basis new model`: a model repo, or another model in one, from basis's templates and optim's,
its example and its agent files, whose own contract tests pass as generated."""

import tomllib
from pathlib import Path

import pytest

import optim
from basis.core.new import UNRELEASED, NewError, NoReleaseError
from optim import models, new, sync
from optim.models import listed
from optim.testing import check_fixture, check_imports, check_params

INDEX = "https://pypi.firm/simple"
CHECKOUT = Path(__file__).parents[3]
"""The checkout of basis these tests run in, which holds packages/optim."""


def test_a_new_repo_holds_one_model_folder_and_everything_around_it(tmp_path: Path) -> None:
    repo = new.repo(tmp_path / "sma-optimizer", "real-income", index=INDEX, framework_path=None, install=False)

    project = tomllib.loads((repo / "pyproject.toml").read_text())
    _, minor, _ = optim.__version__.split(".")
    assert project["project"]["dependencies"] == [f"optim>={optim.__version__},<0.{int(minor) + 1}"]
    assert project["project"]["entry-points"]["optim.models"] == {"real-income": "models.real_income.model"}
    assert project["tool"]["basis"] == {"parts": ["optim"], "coverage": 90}
    assert project["build-system"]["requires"] == ["uv_build>=0.13.6,<0.14"]
    folder = repo / "models" / "real_income"
    for path in ("__init__.py", "main.py", "model.py", "prep.py", "README.md", "datasets/README.md", "datasets/sql/controls.sql", "rules/README.md", "optimizer/problem.py"):
        assert (folder / path).is_file(), path
    for path in ("params/default.json", "tests/test_contract.py", "tests/builders.py", "tests/fixture/fixture.json", "tests/fixture/portfolios.csv"):
        assert (folder / path).is_file(), path
    for path in ("models/__init__.py", "tests/e2e/test_smoke.py", ".dockerignore", ".gitignore", ".env.example", ".python-version", "README.md"):
        assert (repo / path).is_file(), path
    for path in ("Dockerfile", "azure-pipelines.yml", ".githooks/pre-commit", ".githooks/pre-push", ".gitattributes", "AGENTS.md", "CLAUDE.md", ".claude/settings.json"):
        assert (repo / path).is_file(), path  # basis's own files
    for skill in sorted(path.name for path in sync.SKILLS.iterdir() if (path / "SKILL.md").is_file()):
        assert (repo / ".agents" / "skills" / skill / "SKILL.md").is_file(), skill
        assert (repo / ".claude" / "skills" / skill / "SKILL.md").is_file(), skill
    for path in ("src", "models/shared", "models/real_income/ruff.toml", "tests/test_agent_files.py"):
        assert not (repo / path).exists(), path
    code = "".join(path.read_text() for path in (repo / "models").rglob("*.py"))
    assert "optim.example" not in code  # imports point at the repo's own model
    assert not [name for name in ("{repo}", "{model}", "{module}", "{version}", "{source}") if name in (repo / "README.md").read_text() + (repo / "pyproject.toml").read_text()]
    assert sync.stale(repo) == []
    assert not any(path.is_symlink() for path in repo.rglob("*"))  # copies, which Git on Windows checks out as files


def test_a_new_repos_image_runs_basis_with_a_models_command(tmp_path: Path) -> None:
    repo = new.repo(tmp_path / "sma", "income", index=INDEX, framework_path=None, install=False)

    dockerfile = (repo / "Dockerfile").read_text()
    assert f'{sync.IMAGE}ENTRYPOINT ["tini", "--", "basis"]' in dockerfile
    assert "<!-- optim:begin (written by `basis sync`; edit outside the markers) -->\n## The optim contract\n" in (repo / "AGENTS.md").read_text()


def test_a_new_repo_takes_optim_from_the_internal_index_or_a_checkout_of_basis(tmp_path: Path) -> None:
    indexed = tomllib.loads((new.repo(tmp_path / "sma", "income", index=INDEX, framework_path=None, install=False) / "pyproject.toml").read_text())
    checked_out = tomllib.loads((new.repo(tmp_path / "dev" / "sma", "income", index=None, framework_path=CHECKOUT, install=False) / "pyproject.toml").read_text())

    assert indexed["tool"]["uv"]["index"] == [{"name": "internal", "url": INDEX, "default": True}]  # so a name the index lacks never comes from PyPI
    assert "sources" not in indexed["tool"]["uv"]
    assert checked_out["tool"]["uv"]["sources"] == {"optim": {"path": (CHECKOUT / "packages" / "optim").resolve().as_posix(), "editable": True}}


def test_a_new_repo_from_an_unreleased_optim_with_no_source_says_how_to_take_it_from_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """optim installed from the workspace, as here, came from no commit uv recorded, and 0.0.0 has no tag."""
    monkeypatch.setattr(optim, "__version__", UNRELEASED)
    with pytest.raises(NoReleaseError, match=r"uvx --from 'git\+https://github.com/jacobragsdale/basis#subdirectory=packages/optim' optim new repo NAME"):
        new.repo(tmp_path / "sma", "income", index=None, framework_path=None, install=False)

    assert not (tmp_path / "sma").exists()


def test_a_model_name_that_cannot_name_a_module_is_refused_before_the_repo_is_made(tmp_path: Path) -> None:
    with pytest.raises(NewError, match="cannot name a Python package"):
        new.repo(tmp_path / "sma", "2fast", index=INDEX, framework_path=None, install=False)

    assert not (tmp_path / "sma").exists()


def test_the_second_model_brings_the_shared_folder_and_both_pass_their_contract_tests(tmp_path: Path, on_path: list[str]) -> None:
    repo = new.repo(tmp_path / "sma", "income", index=INDEX, framework_path=None, install=False)
    new.model(repo, "ladder", install=False)
    on_path.insert(0, str(repo))

    assert listed(repo) == {"income": "models.income.model", "ladder": "models.ladder.model"}
    assert (repo / "models" / "shared" / "README.md").is_file()
    for module in listed(repo).values():
        model = models.load(module)
        check_params(model, model.directory / "params" / "default.json")
        check_fixture(model, model.directory / "tests" / "fixture", model.directory / "params" / "default.json")
        check_imports(model)
    assert sync.stale(repo) == []


def test_a_new_model_is_listed_in_the_repos_readme_whatever_columns_its_table_has(tmp_path: Path) -> None:
    repo = new.repo(tmp_path / "sma", "income", index=INDEX, framework_path=None, install=False)
    readme = repo / "README.md"
    readme.write_text(readme.read_text().replace("| Model | Folder |\n|---|---|", "| Model | Folder | What it does |\n|---|---|---|"))

    new.model(repo, "model-portfolio", install=False)

    assert "| `income` | `models/income/` |\n| `model-portfolio` | `models/model_portfolio/` | |\n" in readme.read_text()


@pytest.mark.parametrize(("name", "problem"), [("income", "already has a model named income"), ("2fast", "cannot name a Python package")], ids=["taken", "no-module"])
def test_a_model_name_that_is_taken_or_cannot_name_a_module_is_refused(tmp_path: Path, name: str, problem: str) -> None:
    repo = new.repo(tmp_path / "sma", "income", index=INDEX, framework_path=None, install=False)

    with pytest.raises(NewError, match=problem):
        new.model(repo, name, install=False)


def test_a_model_is_added_only_to_a_model_repo(tmp_path: Path) -> None:
    with pytest.raises(NewError, match=r"is not a model repo: .*`uvx optim new repo NAME MODEL`"):
        new.model(tmp_path, "ladder", install=False)


def test_a_new_repo_from_a_pre_release_takes_its_minor_versions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(optim, "__version__", "0.3.0rc1")
    repo = new.repo(tmp_path / "sma", "income", index=INDEX, framework_path=None, install=False)

    assert tomllib.loads((repo / "pyproject.toml").read_text())["project"]["dependencies"] == ["optim>=0.3.0rc1,<0.4"]
