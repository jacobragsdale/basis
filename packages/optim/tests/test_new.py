"""`optim new`: a model repo, or another model in one, from the installed example and templates, whose own contract
tests pass as generated."""

import tomllib
from pathlib import Path

import pytest

from optim import models, sync
from optim.models import listed
from optim.new import OPTIM, new_model, new_repo
from optim.testing import check_agent_files, check_fixture, check_imports, check_params

INDEX = "https://pypi.firm/simple"
CHECKOUT = Path(__file__).parents[1]


def test_a_new_repo_holds_one_model_folder_and_everything_around_it(tmp_path: Path) -> None:
    repo = new_repo("sma-optimizer", "real-income", directory=tmp_path, index=INDEX, uv_sync=False)

    project = tomllib.loads((repo / "pyproject.toml").read_text())
    assert project["project"]["dependencies"] == [OPTIM]  # the version range from the index
    assert project["project"]["entry-points"]["optim.models"] == {"real-income": "models.real_income.model"}
    folder = repo / "models" / "real_income"
    for path in ("__init__.py", "main.py", "model.py", "prep.py", "README.md", "datasets/README.md", "datasets/sql/controls.sql", "rules/README.md", "optimizer/problem.py"):
        assert (folder / path).is_file(), path
    for path in ("params/default.json", "tests/test_contract.py", "tests/builders.py", "tests/fixture/fixture.json", "tests/fixture/portfolios.csv"):
        assert (folder / path).is_file(), path
    for path in ("models/__init__.py", "tests/test_agent_files.py", "tests/e2e/test_smoke.py", "Dockerfile", ".dockerignore", "azure-pipelines.yml", ".gitignore", ".env.example", "AGENTS.md", ".git"):
        assert (repo / path).exists(), path
    for path in ("src", "models/shared", "CLAUDE.md", ".claude", "models/real_income/ruff.toml"):
        assert not (repo / path).exists(), path
    code = "".join(path.read_text() for path in (repo / "models").rglob("*.py"))
    assert "optim.example" not in code  # imports point at the repo's own model
    assert "{{" not in (repo / "README.md").read_text() + (repo / "pyproject.toml").read_text()
    assert sync.stale(repo) == []
    assert not any(path.is_symlink() for path in repo.rglob("*"))  # copies, which Git on Windows checks out as files


def test_a_new_repo_takes_optim_from_the_internal_index_or_a_checkout(tmp_path: Path) -> None:
    indexed = tomllib.loads((new_repo("sma", "income", directory=tmp_path, index=INDEX, uv_sync=False) / "pyproject.toml").read_text())
    checked_out = tomllib.loads((new_repo("sma", "income", directory=tmp_path / "dev", optim_path=CHECKOUT, uv_sync=False) / "pyproject.toml").read_text())

    assert indexed["tool"]["uv"]["index"] == [{"name": "internal", "url": INDEX}]
    assert indexed["tool"]["uv"]["sources"] == {"optim": {"index": "internal"}}
    assert checked_out["tool"]["uv"]["sources"] == {"optim": {"path": CHECKOUT.resolve().as_posix(), "editable": True}}


@pytest.mark.parametrize("sources", [{}, {"index": INDEX, "optim_path": CHECKOUT}], ids=["neither", "both"])
def test_a_new_repo_that_does_not_say_where_optim_comes_from_is_refused(tmp_path: Path, sources: dict[str, object]) -> None:
    """PyPI has an unrelated package named `optim`, which a repo with no source for optim would install."""
    with pytest.raises(ValueError, match="PyPI"):
        new_repo("sma", "income", directory=tmp_path, uv_sync=False, **sources)  # ty: ignore[invalid-argument-type] -- each case's keywords

    assert not (tmp_path / "sma").exists()


def test_the_second_model_brings_the_shared_folder_and_both_pass_their_contract_tests(tmp_path: Path, on_path: list[str]) -> None:
    repo = new_repo("sma", "income", directory=tmp_path, index=INDEX, uv_sync=False)
    new_model("ladder", repo=repo, uv_sync=False)
    on_path.insert(0, str(repo))

    assert listed(repo) == {"income": "models.income.model", "ladder": "models.ladder.model"}
    assert (repo / "models" / "shared" / "README.md").is_file()
    for module in listed(repo).values():
        model = models.load(module)
        check_params(model, model.directory / "params" / "default.json")
        check_fixture(model, model.directory / "tests" / "fixture", model.directory / "params" / "default.json")
        check_imports(model)
    check_agent_files(repo)


def test_a_new_model_is_listed_in_the_repos_readme_whatever_columns_its_table_has(tmp_path: Path) -> None:
    repo = new_repo("sma", "income", directory=tmp_path, index=INDEX, uv_sync=False)
    readme = repo / "README.md"
    readme.write_text(readme.read_text().replace("| Model | Folder |\n|---|---|", "| Model | Folder | What it does |\n|---|---|---|"))

    new_model("model-portfolio", repo=repo, uv_sync=False)

    assert "| `income` | `models/income/` |\n| `model-portfolio` | `models/model_portfolio/` | |\n" in readme.read_text()


def test_a_model_name_that_is_taken_or_cannot_name_a_module_is_refused(tmp_path: Path) -> None:
    repo = new_repo("sma", "income", directory=tmp_path, index=INDEX, uv_sync=False)

    with pytest.raises(FileExistsError, match="already has a model named income"):
        new_model("income", repo=repo, uv_sync=False)
    with pytest.raises(ValueError, match="cannot name a Python package"):
        new_model("2fast", repo=repo, uv_sync=False)
