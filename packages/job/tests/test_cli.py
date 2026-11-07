import json
import os
from pathlib import Path

import pytest

from basis.core import command
from basis.job import cli
from basis.job.example import job

EXAMPLE = Path(job.__file__).parent
RUN = ["run", "basis.job.example.job", "--params", str(EXAMPLE / "params/default.json"), "--data", str(EXAMPLE / "tests/fixture"), "--interval", "2026-11-05..2026-11-06"]


@pytest.fixture(autouse=True)
def snapshot_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in [name for name in os.environ if name.startswith("BASIS_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("BASIS_SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    return tmp_path / "snapshots"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A job repo's root, as `basis` finds it: a pyproject.toml that declares the job part."""
    folder = tmp_path / "repo"
    folder.mkdir()
    (folder / "pyproject.toml").write_text('[project]\nname = "repo"\n\n[tool.basis]\nparts = ["job"]\n', encoding="utf-8")
    return folder


def basis(repo: Path, *argv: str) -> int:
    return command.run(argv, cwd=repo, installed={"job": cli.part})


def error_line(capsys: pytest.CaptureFixture[str]) -> str:
    """The one line a failure prints, after the run's log records."""
    line = capsys.readouterr().err.splitlines()[-1]
    assert line.startswith("ERROR: ")
    return line


def test_basis_finds_the_job_part_through_its_entry_point() -> None:
    assert [verb.name for verb in command.installed()["job"]().verbs] == ["run", "resume", "describe", "ddl"]


def test_a_run_that_passes_every_check_exits_zero(repo: Path) -> None:
    assert basis(repo, *RUN) == 0


def test_a_run_that_cannot_publish_exits_nonzero_with_the_reason_and_the_fix(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo, *RUN, "--publish") == 1
    line = error_line(capsys)
    assert "wrote nothing, because database 'warehouse' is not configured, and the run publishes to it: set BASIS_DATABASES__WAREHOUSE__*" in line
    assert line.endswith("fix it, and run the job again")


def test_a_run_whose_params_do_not_validate_names_the_field_and_the_file(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "bad.json").write_text('{"include_closed_acounts": true}', encoding="utf-8")
    assert basis(repo, *RUN[:3], str(tmp_path / "bad.json"), *RUN[4:]) == 1
    err = capsys.readouterr().err
    assert err.startswith(f"ERROR: while reading basis.job.example.job's params from {tmp_path / 'bad.json'}: Params is not valid: include_closed_acounts: Extra inputs")
    assert err.count("\n") == 1


def test_describe_prints_the_contract_as_json(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo, "describe", "basis.job.example.job") == 0
    described = json.loads(capsys.readouterr().out)
    assert described["outputs"]["positions"] | {"columns": []} == {
        "database": "warehouse",
        "table": "daily_positions",
        "write": "ReplaceWhere",
        "key": ["trade_date", "account_id", "security_id"],
        "columns": [],
    }
    assert described["inputs"]["accounts"]["empty"] == "fail"


def test_the_ledger_ddl_prints_without_a_job(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo, "ddl", "--ledger", "etl.publishes", "--dialect", "mssql") == 0
    assert capsys.readouterr().out.startswith("CREATE TABLE etl.publishes (")


def test_an_unknown_job_exits_nonzero_and_says_what_to_give(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo, "describe", "no_such_job") == 1
    assert error_line(capsys).endswith("or a job's module path, as jobs.NAME.job")


def test_a_replay_of_a_run_on_the_same_code_changes_nothing(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis(repo, *RUN) == 0
    run_id = next(json.loads(line)["run_id"] for line in capsys.readouterr().err.splitlines() if '"run_id"' in line)
    assert basis(repo, "replay", "basis.job.example.job", run_id) == 0
    replayed = json.loads(capsys.readouterr().out)
    assert (replayed["replay_of"], replayed["status"], replayed["differences"]) == (run_id, "checked", [])


def test_a_repo_made_before_basis_has_a_release_fails_with_one_error_line_that_names_the_fix(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.scaffold(["new", "repo", "pricing-etl", "--dir", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("ERROR: basis has no release yet")
    assert captured.err.rstrip("\n").endswith("pass --framework-path with the clone's folder")
    assert captured.err.count("\n") == 1
    assert not (tmp_path / "pricing-etl").exists()
