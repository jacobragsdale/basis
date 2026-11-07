"""optim's part of the `basis` command, which its `basis.verbs` entry point names; `optim`, which is `basis` under its
earlier name; and a repo with both jobs and models, whose shared verbs reach the part that runs each name."""

import json
import os
import sys
from pathlib import Path

import pytest
from fakes import EXAMPLE, Basis

from basis.core import command
from basis.job.example import job
from optim import cli

JOB = Path(job.__file__).parent


@pytest.fixture(autouse=True)
def no_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [name for name in os.environ if name.startswith(("BASIS_", "OPTIM_"))]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("BASIS_SNAPSHOT_ROOT", str(tmp_path / "snapshots"))


def error_line(capsys: pytest.CaptureFixture[str]) -> str:
    """The one line a failure prints, after the logs."""
    line = capsys.readouterr().err.splitlines()[-1]
    assert line.startswith("ERROR: ")
    return line


def test_basis_finds_the_optim_part_through_its_entry_point() -> None:
    part = command.installed()["optim"]()

    assert [verb.name for verb in part.verbs] == ["sweep", "warm"]
    assert part.runs is not None
    assert [name for name in ("run", "describe", "resume", "replay", "regress", "ddl") if getattr(part.runs, name) is not None] == ["run", "describe", "replay", "regress"]
    assert (part.runs.noun, list(part.new), part.docs) == ("model", ["model"], cli.DOCS)


def test_optim_is_basis_under_its_earlier_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "sma"\n\n[tool.basis]\nparts = ["optim"]\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["optim", "--help"])
    assert cli.main() == 0
    optim = capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["basis", "--help"])
    assert command.main() == 0

    assert optim == capsys.readouterr().out
    assert "\nFor its models:\n" in optim
    assert "\nFrom the optim part:\n  sweep  " in optim


def test_the_variables_optim_0_2_0_read_are_refused_before_any_verb_runs(basis: Basis, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPTIM_WORKERS", "2")
    assert basis("describe", "optim.example.model") == 1
    assert "OPTIM_WORKERS is set, and optim reads only BASIS_ variables now: rename OPTIM_WORKERS to BASIS_OPTIM__WORKERS" in error_line(capsys)


def test_a_failure_of_a_verb_of_optims_is_one_line_with_its_fix(basis: Basis, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis("describe", "models.nothing.model") == 1
    assert error_line(capsys).startswith("ERROR: no model 'models.nothing.model'")
    assert basis("replay", "R1") == 1
    assert error_line(capsys).startswith("ERROR: no run R1 in ")


def test_a_replay_names_the_model_whose_run_it_is(basis: Basis, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis("run", "optim.example.model", "--params", str(EXAMPLE / "params" / "default.json"), "--data", str(EXAMPLE / "tests" / "fixture")) == 0
    run_id = capsys.readouterr().out.splitlines()[0].removeprefix("run ")

    assert basis("replay", "optim.example.model", run_id, "--portfolios", "P1") == 0
    assert [line.split()[0] for line in capsys.readouterr().out.splitlines()[2:]] == ["P1"]  # after the run's id and the table's header
    assert basis("replay", "toys.basic", run_id) == 1
    assert error_line(capsys) == f"ERROR: run {run_id} is a run of optim.example.model, not toys.basic: give optim.example.model, or leave the model out"


def test_a_replay_takes_the_name_a_models_module_is_listed_under_now(basis: Basis, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """The run was saved under the model's module path; the repo lists that module as `earnings` now."""
    assert basis("run", "optim.example.model", "--params", str(EXAMPLE / "params" / "default.json"), "--data", str(EXAMPLE / "tests" / "fixture")) == 0
    run_id = capsys.readouterr().out.splitlines()[0].removeprefix("run ")
    monkeypatch.setattr("optim.models.installed", lambda: {"earnings": "optim.example.model"})

    assert basis("replay", "earnings", run_id) == 0


def test_regress_without_a_model_prints_json_and_with_one_a_table(basis: Basis, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("optim.models.installed", lambda: {"income": "optim.example.model"})
    assert basis("run", "income", "--params", str(EXAMPLE / "params" / "default.json"), "--data", str(EXAMPLE / "tests" / "fixture"), "--label", "purpose=test") == 0
    capsys.readouterr()

    assert basis("regress") == 0
    assert json.loads(capsys.readouterr().out) == []  # the run replays as it ran
    assert basis("regress", "income") == 0
    assert capsys.readouterr().out == "run_id,portfolio_id,status_before,status_after,limit,security_id,side,before,after\n"


@pytest.mark.parametrize(
    ("argv", "problem"),
    [
        (["new", "repo", "Bad Name", "income"], "'Bad Name' is no repo's name"),
        (["new", "repo", "sma", "2fast", "--index", "https://pypi.firm/simple"], "'2fast' cannot name a Python package or module"),
    ],
    ids=["a-bad-repo-name", "a-bad-model-name"],
)
def test_optim_new_repo_runs_outside_any_repo_and_fails_as_basis_does(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str], problem: str) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["optim", *argv])
    assert cli.main() == 1
    assert problem in error_line(capsys)
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def jobs_and_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo that declares both parts, with a job named positions and a model named income: the examples."""
    monkeypatch.setattr("basis.job.job.listed", lambda: {"positions": "basis.job.example.job"})
    monkeypatch.setattr("optim.models.installed", lambda: {"income": "optim.example.model"})
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "desk"\n\n[tool.basis]\nparts = ["job", "optim"]\n', encoding="utf-8")
    return repo


def basis_in(repo: Path, *argv: str) -> int:
    return command.run(argv, cwd=repo, installed=command.installed())


def test_a_repo_with_jobs_and_models_runs_each_by_its_name(jobs_and_models: Path, capsys: pytest.CaptureFixture[str]) -> None:
    job = ["run", "positions", "--params", str(JOB / "params/default.json"), "--data", str(JOB / "tests/fixture"), "--interval", "2026-11-05..2026-11-06"]
    model = ["run", "income", "--params", str(EXAMPLE / "params" / "default.json"), "--data", str(EXAMPLE / "tests" / "fixture"), "--portfolios", "P1"]

    assert basis_in(jobs_and_models, *job) == 0
    assert basis_in(jobs_and_models, *model) == 0
    assert capsys.readouterr().out.splitlines()[0].startswith("run ")
    assert basis_in(jobs_and_models, "describe") == 0
    assert json.loads(capsys.readouterr().out) == {"positions": "basis.job.example.job", "income": "optim.example.model"}
    assert basis_in(jobs_and_models, "describe", "income") == 0
    assert json.loads(capsys.readouterr().out)["model"] == "income"
    assert basis_in(jobs_and_models, "regress") == 0
    assert json.loads(capsys.readouterr().out) == {"job": [], "optim": []}  # one JSON document, each part's result by part


def test_in_a_repo_with_jobs_and_models_an_option_of_the_other_part_is_refused(jobs_and_models: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis_in(jobs_and_models, "run", "income", "--params", str(EXAMPLE / "params" / "default.json"), "--interval", "2026-11-05..2026-11-06") == 2
    assert error_line(capsys) == "ERROR: basis run: unrecognized arguments: --interval 2026-11-05..2026-11-06: `basis run --help` says what it takes"


def test_in_a_repo_with_jobs_and_models_a_name_both_run_is_refused_with_the_fix(jobs_and_models: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("optim.models.installed", lambda: {"positions": "optim.example.model"})
    assert basis_in(jobs_and_models, "describe", "positions") == 1
    assert error_line(capsys) == "ERROR: the job and optim parts both run something named 'positions': rename one of them"


def test_help_in_a_repo_with_jobs_and_models_lists_the_shared_verbs_once(jobs_and_models: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis_in(jobs_and_models, "--help") == 0
    out = capsys.readouterr().out
    assert "\nFor its jobs and models:\n" in out
    assert [line.split()[0] for line in out.splitlines() if line.startswith("  ")].count("run") == 1
