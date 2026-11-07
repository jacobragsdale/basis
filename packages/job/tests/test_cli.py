import json
import os
from pathlib import Path

import pytest

from basis.job.cli import main
from basis.job.example import job

EXAMPLE = Path(job.__file__).parent
RUN = ["run", "basis.job.example.job", "--params", str(EXAMPLE / "params/default.json"), "--data", str(EXAMPLE / "tests/fixture"), "--interval", "2026-11-05..2026-11-06"]


@pytest.fixture(autouse=True)
def snapshot_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in [name for name in os.environ if name.startswith("ETL_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("ETL_SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    return tmp_path / "snapshots"


def test_a_run_that_passes_every_check_exits_zero() -> None:
    assert main(RUN) == 0


def test_a_run_that_cannot_publish_exits_nonzero() -> None:
    assert main([*RUN, "--publish"]) == 1


def test_a_run_whose_params_do_not_validate_exits_nonzero(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text('{"include_closed_acounts": true}', encoding="utf-8")
    assert main([*RUN[:3], str(tmp_path / "bad.json"), *RUN[4:]]) == 1


def test_describe_prints_the_contract_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["describe", "basis.job.example.job"]) == 0
    described = json.loads(capsys.readouterr().out)
    assert described["outputs"]["positions"] | {"columns": []} == {
        "database": "warehouse",
        "table": "daily_positions",
        "write": "ReplaceWhere",
        "key": ["trade_date", "account_id", "security_id"],
        "columns": [],
    }
    assert described["inputs"]["accounts"]["empty"] == "fail"


def test_the_ledger_ddl_prints_without_a_job(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["ddl", "--ledger", "etl.publishes", "--dialect", "mssql"]) == 0
    assert capsys.readouterr().out.startswith("CREATE TABLE etl.publishes (")


def test_an_unknown_job_exits_nonzero() -> None:
    assert main(["describe", "no_such_job"]) == 1


def test_a_repo_made_before_basis_has_a_release_fails_with_one_error_line_that_names_the_fix(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["new", "repo", "pricing-etl", "prices", "--dir", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("ERROR: basis has no release yet")
    assert captured.err.rstrip("\n").endswith("pass --framework-path with the clone's folder")
    assert captured.err.count("\n") == 1
    assert not (tmp_path / "pricing-etl").exists()
