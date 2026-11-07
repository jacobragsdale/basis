"""The pytest plugin a model repo gets with optim: the e2e suite's safeguards, which refuse to run anywhere but QA and to
write anywhere but the allowlist, and its smoke case."""

import os
import sys
from pathlib import Path

import pytest

from optim.datasets import HOLDINGS, SECURITIES
from optim.pytest_plugin import QA, qa_problems
from optim.settings import Settings
from optim.storage import Root
from optim.testing import CASH, frame


def settings(**values: object) -> Settings:
    return Settings.model_validate({"snapshot_root": "//qa-share/optim/snapshots"} | values)


@pytest.mark.parametrize(
    ("values", "writes", "problems"),
    [
        ({"mssql": {"connection_string": "Server=sql-qa.firm;Database=Accounts", "max_concurrent": 1, "timeout_s": 1, "max_rows": 1}}, ["//qa-share/optim/snapshots"], []),
        (
            {"mssql": {"connection_string": "Server=sql-prod.firm;Database=Accounts;Password=x", "max_concurrent": 1, "timeout_s": 1, "max_rows": 1}},
            ["//qa-share/optim/snapshots"],
            ["OPTIM_MSSQL__CONNECTION_STRING (sql-prod.firm, database Accounts) contains none"],
        ),
        ({"rest": {"url": "https://signals.firm", "max_concurrent": 1, "timeout_s": 1, "max_rows": 1}}, ["//qa-share/optim/snapshots"], ["OPTIM_REST__URL (https://signals.firm/) contains none"]),
        ({}, [], ["OPTIM_SNAPSHOT_ROOT (//qa-share/optim/snapshots) is not in optim_e2e_writes"]),
        (
            {"orders": {"connection_string": "Server=sql-qa.firm;Database=Trading", "table": "qa.orders"}},
            ["//qa-share/optim/snapshots"],
            ["OPTIM_ORDERS__TABLE (qa.orders) is not in optim_e2e_writes"],
        ),
    ],
    ids=["qa", "production-database", "unmarked-api", "unlisted-snapshot-root", "unlisted-orders-table"],
)
def test_the_e2e_suite_refuses_any_source_or_destination_outside_qa(values: dict[str, object], writes: list[str], problems: list[str]) -> None:
    found = qa_problems(settings(**values), markers=["qa"], writes=writes)

    assert [problem[: len(expected)] for problem, expected in zip(found, problems, strict=False)] == problems
    assert len(found) == len(problems)
    assert all("Password" not in problem for problem in found)  # a connection string's credentials stay out of messages


def test_the_qa_fixture_refuses_to_run_outside_qa(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPTIM_SNAPSHOT_ROOT", "//prod-share/optim/snapshots")
    pytester.makepyfile("def test_smoke(optim_qa) -> None:\n    raise AssertionError('ran outside QA')\n")

    result = pytester.runpytest("-p", "no:randomly")

    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*refusing to run the e2e suite outside QA*", "OPTIM_SNAPSHOT_ROOT (//prod-share/optim/snapshots) contains none of the QA markers*"])


def test_qa_picks_portfolios_with_a_select_alone(tmp_path: Path) -> None:
    (query := tmp_path / "portfolios.sql").write_text("-- every portfolio\nDELETE FROM positions")

    with pytest.raises(ValueError, match="is not a SELECT"):
        QA(settings(), ["optim"], tmp_path).portfolios(query)


def test_the_smoke_case_runs_replays_and_compares_a_models_trades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Against files standing in for QA: the run's command is this interpreter's optim, as an image's is."""
    data = tmp_path / "data"
    data.mkdir()
    frame(HOLDINGS, {"portfolio_id": "P1"}, {"portfolio_id": "P2"}).to_csv(data / "holdings.csv", index=False)
    frame(SECURITIES, CASH, {"security_id": "A"}).to_csv(data / "securities.csv", index=False)
    (params := tmp_path / "params.json").write_text("{}")
    root = tmp_path / "qa-snapshots"
    monkeypatch.setenv("OPTIM_SNAPSHOT_ROOT", str(root))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(Path(__file__).parent), *sys.path]))
    qa = QA(Settings(), [sys.executable, "-m", "optim"], tmp_path)

    ran = qa.smoke("toys.basic", params, ["P1", "P2"], "--data", str(data), "--as-of", "2026-10-16")

    assert ran.exit_code == 0
    assert len(Root.at(root).children()) == 2  # the run and its replay


def test_qa_picks_the_first_portfolios_a_model_lists() -> None:
    """`toys.listed` lists P1, P2 and P3 in a file, which stands in for QA's table."""
    assert QA(settings(), ["optim"], Path()).listed("toys.listed", count=2) == ["P1", "P2"]
