"""The pytest plugin a model repo gets with optim: the e2e suite's safeguards, which refuse to run anywhere but QA and to
write anywhere but the allowlist, and its smoke case."""

import os
import sys
from pathlib import Path

import pytest

from basis.core.storage import Root
from optim.datasets import HOLDINGS, SECURITIES
from optim.pytest_plugin import QA, qa_problems
from optim.settings import Settings
from optim.testing import CASH, frame


def settings(**values: object) -> Settings:
    return Settings.model_validate({"snapshot_root": "//qa-share/optim/snapshots"} | values)


def mssql(reads: str, writes: str | None = None, ledger: str | None = None) -> dict[str, object]:
    """A SQL Server database's settings, with the connection string of its read login and, if given, its write login
    and its ledger."""
    database: dict[str, object] = {"kind": "mssql", "connection_string": reads, "isolation": "snapshot", "timeout_s": 1, "max_rows": 1}
    return database | ({"write": {"connection_string": writes, "ledger": ledger}} if writes is not None else {})


QA_BOOKS = mssql("Server=sql-qa.firm;Database=Accounts")
REST = {"url": "https://signals.firm", "max_concurrent": 1, "timeout_s": 1, "max_rows": 1}


@pytest.mark.parametrize(
    ("values", "writes", "problems"),
    [
        ({"databases": {"books": QA_BOOKS}}, ["//qa-share/optim/snapshots"], []),
        (
            {"databases": {"books": mssql("Server=sql-prod.firm;Database=Accounts;Password=x")}},
            ["//qa-share/optim/snapshots"],
            ["BASIS_DATABASES__BOOKS__CONNECTION_STRING (sql-prod.firm, database Accounts) contains none"],
        ),
        (
            {"databases": {"books": mssql("Server=sql-qa.firm;Database=Accounts", "Server=sql-prod.firm;Database=Accounts;Password=x")}},
            ["//qa-share/optim/snapshots"],
            ["BASIS_DATABASES__BOOKS__WRITE__CONNECTION_STRING (sql-prod.firm, database Accounts) contains none"],
        ),
        ({"optim": {"rest": REST}}, ["//qa-share/optim/snapshots"], ["BASIS_OPTIM__REST__URL (https://signals.firm/) contains none"]),
        ({}, [], ["BASIS_SNAPSHOT_ROOT (//qa-share/optim/snapshots) is not in optim_e2e_writes"]),
        (
            {
                "databases": {"trading": mssql("Server=sql-qa.firm;Database=Trading", "Server=sql-qa.firm;Database=Trading", "qa.publishes")},
                "optim": {"orders": {"database": "trading", "table": "qa.orders"}},
            },
            ["//qa-share/optim/snapshots", "qa.publishes"],
            ["BASIS_OPTIM__ORDERS__TABLE (qa.orders) is not in optim_e2e_writes"],
        ),
    ],
    ids=["qa", "production-database", "production-write-login", "unmarked-api", "unlisted-snapshot-root", "unlisted-orders-table"],
)
def test_the_e2e_suite_refuses_any_source_or_destination_outside_qa(values: dict[str, object], writes: list[str], problems: list[str]) -> None:
    found = qa_problems(settings(**values), markers=["qa"], writes=writes)

    assert [problem[: len(expected)] for problem, expected in zip(found, problems, strict=False)] == problems
    assert len(found) == len(problems)
    assert all("Password" not in problem for problem in found)  # a connection string's credentials stay out of messages


UNMARKED_LEDGER = {
    "databases": {"trading": mssql("Server=sql-qa.firm;Database=Trading", "Server=sql-qa.firm;Database=Trading", "etl.publishes")},
    "optim": {"orders": {"database": "trading", "table": "qa.orders"}},
}
QA_WRITES = ["//qa-share/optim/snapshots", "qa.orders"]


def test_the_orders_ledger_is_checked_only_for_a_run_that_publishes() -> None:
    environment = settings(**UNMARKED_LEDGER)

    assert qa_problems(environment, markers=["qa"], writes=QA_WRITES) == []
    assert [problem.split(" of", 1)[0] for problem in qa_problems(environment, markers=["qa"], writes=QA_WRITES, publishing=True)] == [
        "BASIS_DATABASES__TRADING__WRITE__LEDGER (etl.publishes) contains none",
        "BASIS_DATABASES__TRADING__WRITE__LEDGER (etl.publishes) is not in optim_e2e_writes ['//qa-share/optim/snapshots', 'qa.orders']",
    ]


def test_a_qa_run_given_publish_is_refused_before_it_runs_where_the_ledger_is_not_qas(tmp_path: Path) -> None:
    """The command does not exist, so a run that started would fail otherwise."""
    qa = QA(settings(**UNMARKED_LEDGER), [str(tmp_path / "no-such-optim")], tmp_path, ["qa"], QA_WRITES)

    with pytest.raises(pytest.fail.Exception, match=r"refusing to publish outside QA:\nBASIS_DATABASES__TRADING__WRITE__LEDGER \(etl.publishes\) contains none"):
        qa.run("toys.basic", Path("params.json"), ["P1"], "--publish")


def test_the_qa_fixture_refuses_to_run_outside_qa(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BASIS_ENV", "qa")
    monkeypatch.setenv("BASIS_SNAPSHOT_ROOT", "//prod-share/optim/snapshots")
    pytester.makepyfile("def test_smoke(optim_qa) -> None:\n    raise AssertionError('ran outside QA')\n")

    result = pytester.runpytest("-p", "no:randomly")

    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*refusing to run the e2e suite outside QA*", "BASIS_SNAPSHOT_ROOT (//prod-share/optim/snapshots) contains none of the QA markers*"])


def test_qa_picks_portfolios_with_a_select_alone(tmp_path: Path) -> None:
    (query := tmp_path / "portfolios.sql").write_text("-- every portfolio\nDELETE FROM positions")

    with pytest.raises(ValueError, match="is not a SELECT"):
        QA(settings(), ["optim"], tmp_path).portfolios(query)


def test_the_smoke_case_runs_replays_and_compares_a_models_trades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Against files standing in for QA: the run's command is this interpreter's optim, as an image's is, in a repo that
    declares optim."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "sma"\n\n[tool.basis]\nparts = ["optim"]\n', encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    frame(HOLDINGS, {"portfolio_id": "P1"}, {"portfolio_id": "P2"}).to_csv(data / "holdings.csv", index=False)
    frame(SECURITIES, CASH, {"security_id": "A"}).to_csv(data / "securities.csv", index=False)
    (params := tmp_path / "params.json").write_text("{}")
    root = tmp_path / "qa-snapshots"
    monkeypatch.setenv("BASIS_ENV", "qa")
    monkeypatch.setenv("BASIS_SNAPSHOT_ROOT", str(root))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(Path(__file__).parent), *sys.path]))
    qa = QA(Settings(), [sys.executable, "-m", "optim"], tmp_path)

    ran = qa.smoke("toys.basic", params, ["P1", "P2"], "--data", str(data), "--as-of", "2026-10-16")

    assert ran.exit_code == 0
    assert len(Root.at(root / "runs" / "optim" / "toys.basic").children()) == 2  # the run and its replay


def test_qa_picks_the_first_portfolios_a_model_lists() -> None:
    """`toys.listed` lists P1, P2 and P3 in a file, which stands in for QA's table."""
    assert QA(settings(), ["optim"], Path()).listed("toys.listed", count=2) == ["P1", "P2"]
