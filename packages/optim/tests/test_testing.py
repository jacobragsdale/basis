"""What a model repo's tests use from optim: each model's contract tests, which its `tests/test_contract.py` subclasses
from `optim.testing.ContractTests`, and the check of the repo's agent files. They run in throwaway model repos, as any
of the repo's own tests do."""

import json
from pathlib import Path

import pytest

from optim import sync
from optim.datasets import HOLDINGS, SECURITIES
from optim.testing import CASH, frame

TOY = (Path(__file__).parent / "toys" / "basic.py").read_text()
CONTRACT = "from optim.testing import ContractTests\n\nfrom models.toy import model\n\n\nclass TestContract(ContractTests):\n    model = model\n"
AGENT_FILES = "from pathlib import Path\n\nfrom optim.testing import check_agent_files\n\n\ndef test_agent_files() -> None:\n    check_agent_files(Path(__file__).parents[1])\n"


def model_repo(pytester: pytest.Pytester, *, model: str = TOY, params: dict[str, object] | None = None, synced: bool = True) -> Path:
    """A model repo with one model, `toy`, laid out as `optim new` lays it out, with its default params, its fixture and
    its contract tests: P1 and P2 hold cash, and A is bought."""
    repo = pytester.path
    (repo / "pyproject.toml").write_text('[project]\nname = "toyrepo"\nversion = "0"\n\n[project.entry-points."optim.models"]\ntoy = "models.toy.model"\n')
    folder = repo / "models" / "toy"
    (folder / "params").mkdir(parents=True)
    (folder / "tests" / "fixture").mkdir(parents=True)
    for package in (repo / "models", folder, folder / "tests"):
        (package / "__init__.py").write_text("")
    (folder / "model.py").write_text(model)
    (folder / "params" / "default.json").write_text(json.dumps(params or {}))
    (folder / "tests" / "test_contract.py").write_text(CONTRACT)
    fixture = folder / "tests" / "fixture"
    frame(HOLDINGS, {"portfolio_id": "P1"}, {"portfolio_id": "P2"}).to_csv(fixture / "holdings.csv", index=False)
    frame(SECURITIES, CASH, {"security_id": "A"}).to_csv(fixture / "securities.csv", index=False)
    (fixture / "fixture.json").write_text('{"as_of_date": "2026-10-16"}')
    (repo / "tests").mkdir()
    (repo / "tests" / "test_agent_files.py").write_text(AGENT_FILES)
    if synced:
        sync.sync(repo)
    pytester.syspathinsert()
    return repo


def test_a_model_that_keeps_the_contract_passes_its_contract_tests(pytester: pytest.Pytester) -> None:
    model_repo(pytester)

    result = pytester.runpytest("-p", "no:randomly")

    result.assert_outcomes(passed=4)  # params, fixture, imports and agent files
    result.stdout.no_fnmatch_line("*FAILED*")


@pytest.mark.parametrize(
    ("change", "failure"),
    [
        ({"model": TOY + "\nimport httpx  # noqa: E402, F401\n"}, ["*can reach I/O through its imports*", "*models.toy.model -> httpx*"]),
        ({"model": TOY + "\nfrom optim import api  # noqa: E402, F401\n"}, ["*can reach I/O through its imports*", "*models.toy.model -> optim.api*"]),
        ({"params": {"unsolved": ["P2"]}}, ["*rejects portfolios of its fixture*", "*P2: not solved: the model says so*"]),
        ({"synced": False}, ["*run `uv run optim sync`*"]),
    ],
    ids=["io-import", "api-import", "fixture-rejected", "stale-agent-files"],
)
def test_a_model_that_breaks_the_contract_fails_saying_what_to_do(pytester: pytest.Pytester, change: dict[str, object], failure: list[str]) -> None:
    model_repo(pytester, **change)  # ty: ignore[invalid-argument-type] -- each case's keyword

    result = pytester.runpytest("-p", "no:randomly")

    result.assert_outcomes(passed=3, failed=1)
    result.stdout.fnmatch_lines(failure)


def test_a_models_main_and_tests_are_not_model_code(pytester: pytest.Pytester) -> None:
    """`main.py` runs the model through `optim.run`, which reaches every source; workers import `model.py` alone."""
    repo = model_repo(pytester)
    (repo / "models" / "toy" / "main.py").write_text('from optim import run\n\nfrom models.toy import model\n\nif __name__ == "__main__":\n    run(model)\n')

    pytester.runpytest("-p", "no:randomly").assert_outcomes(passed=4)


def test_misspelled_default_params_fail_the_params_and_the_fixture_tests(pytester: pytest.Pytester) -> None:
    model_repo(pytester, params={"by": "A"})

    result = pytester.runpytest("-p", "no:randomly")

    result.assert_outcomes(passed=2, failed=2)
    result.stdout.fnmatch_lines(["*by*", "*Extra inputs are not permitted*"])
