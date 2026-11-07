import json
import os
from pathlib import Path

import pytest

from etl_framework.cli import main
from etl_framework.example import job

EXAMPLE = Path(job.__file__).parent
RUN = ["run", "etl_framework.example.job", "--params", str(EXAMPLE / "params/default.json"), "--data", str(EXAMPLE / "tests/fixture"), "--interval", "2026-11-05..2026-11-06"]


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
    assert main(["describe", "etl_framework.example.job"]) == 0
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
