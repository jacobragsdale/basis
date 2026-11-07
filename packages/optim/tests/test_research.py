"""Research on saved runs, from the snapshot alone: sweeps of params variants, regressions of recent runs against
today's code, one portfolio decided from Python, and a model described for agents."""

import json
from datetime import date
from pathlib import Path

import cvxpy as cp
import pandas as pd
import pytest
from builders import EXAMPLE, Clock, Rows, loader
from toys.scarce import Supply

from optim import models, research
from optim.cli import main
from optim.datasets import HOLDINGS, SECURITIES
from optim.runs import open_snapshot, run_model
from optim.storage import Root
from optim.testing import AS_OF, CASH, frame

SOURCE = Rows(
    {
        HOLDINGS: frame(HOLDINGS, {"portfolio_id": "P1"}, {"portfolio_id": "P2", "quantity": 60_000.0}, {"portfolio_id": "P3", "quantity": 80_000.0}),
        SECURITIES: frame(SECURITIES, CASH, {"security_id": "A"}, {"security_id": "NEW"}),
    }
)


def saved(tmp_path: Path, model: str, *, labels: dict[str, str] | None = None, **values: object) -> Root:
    root = Root.at(tmp_path / "snapshots")
    (tmp_path / "params.json").write_text(json.dumps(values))
    ran = run_model(loader(SOURCE), root, models.load(model), tmp_path / "params.json", as_of_date=AS_OF, portfolio_ids=["P1", "P2", "P3"], labels=labels or {}, clock=Clock())
    return root / ran.run_id


def variants(tmp_path: Path, **files: dict[str, object]) -> list[Path]:
    (directory := tmp_path / "variants").mkdir()
    for name, values in files.items():
        (directory / f"{name}.json").write_text(json.dumps(values))
    return sorted(directory.glob("*.json"))


def test_a_sweep_decides_the_snapshot_once_per_variant_into_one_table(tmp_path: Path) -> None:
    snapshot = open_snapshot(saved(tmp_path, "toys.basic"))

    table = research.sweep(snapshot, variants(tmp_path, a={"buy": "A"}, bad={"buy": "BAD"}, unsolved={"unsolved": ["P2"]}))

    assert table.groupby("variant")["accepted"].sum().to_dict() == {"a": 3, "bad": 0, "unsolved": 2}
    assert {"objective", "turnover", "trades", "cash", "solve_seconds", "holdings", "reasons"} <= set(table.columns)  # `holdings` is the toy's metric


def test_a_sweep_refuses_a_misspelled_variant_before_any_decides(tmp_path: Path) -> None:
    snapshot = open_snapshot(saved(tmp_path, "toys.basic"))

    with pytest.raises(ValueError, match=r"bad\.json"):
        research.sweep(snapshot, variants(tmp_path, a={"buy": "A"}, bad={"bye": "A"}))


def test_regress_lists_every_trade_and_status_that_today_s_code_changes(tmp_path: Path) -> None:
    """The saved run is edited as an older version of the model would have decided it: P1 bought more of A."""
    snapshot = saved(tmp_path, "toys.basic", labels={"purpose": "production"})
    saved(tmp_path, "toys.basic", labels={"purpose": "research"})
    trades = pd.read_parquet(Path(snapshot.path) / "trades.parquet")
    trades.loc[trades["portfolio_id"] == "P1", "quantity"] = 2000.0
    trades.to_parquet(Path(snapshot.path) / "trades.parquet", index=False)

    changes = research.regress(models.load("toys.basic"), snapshot.parent, labels={"purpose": "production"})

    assert changes[["portfolio_id", "security_id", "side", "before", "after"]].to_dict("records") == [{"portfolio_id": "P1", "security_id": "A", "side": "buy", "before": 2000.0, "after": 1000.0}]
    assert research.regress(models.load("toys.basic"), snapshot.parent, labels={"purpose": "research"}).empty


def test_one_portfolio_decided_from_python_hands_back_what_it_saw_and_the_problem_it_solved(tmp_path: Path) -> None:
    ran = run_model(loader(Rows({})), Root.at(tmp_path), models.load("optim.example.model"), EXAMPLE / "params" / "default.json", as_of_date=date(2026, 10, 16), data_dir=EXAMPLE / "tests" / "fixture")

    trial = research.trial(open_snapshot(Root.at(tmp_path) / ran.run_id), "P1")

    assert isinstance(trial.problem, cp.Problem)
    assert trial.problem.status == "optimal"
    assert set(trial.portfolio.universe["security_id"]) == {"A", "B", "C", "D", "E"}  # ty: ignore[unresolved-attribute] -- the example's Portfolio
    assert trial.outcome.accepted
    assert trial.outcome.decision is not None
    assert trial.outcome.decision.sell == {"B": 46_000.0}


def test_one_portfolio_of_a_model_that_reserves_decides_after_those_before_it(tmp_path: Path) -> None:
    """Largest cash first: P1 takes 1,000,000 of the 1,100,000, P3 80,000, and P2 the 20,000 left."""
    trial = research.trial(open_snapshot(saved(tmp_path, "toys.scarce", supply=1_100_000.0)), "P2")

    assert trial.market == Supply(left=20_000.0)
    assert trial.outcome.decision is not None
    assert trial.outcome.decision.buy == {"NEW": 20_000.0}


def test_describe_tells_an_agent_what_a_model_takes_reads_and_reports(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["describe", "optim.example.model"]) == 0

    described = json.loads(capsys.readouterr().out)
    assert "Buy only bonds rated this or better" in described["params"]["properties"]["min_rating"]["description"]
    assert {name: (d["source"], d["per_portfolio"]) for name, d in described["datasets"].items()} == {
        "holdings": ("mssql", True),
        "securities": ("oracle", False),
        "portfolios": ("mssql", False),
        "signals": ("rest", False),
        "controls": ("mssql", True),
        "mandates": ("mssql", True),
    }
    assert set(described["metrics"]["properties"]) == {"income", "cost"}
    assert described["hooks"] == ["prepare_market", "prepare_portfolio"]


def test_the_sweep_command_writes_its_table(tmp_path: Path) -> None:
    snapshot = saved(tmp_path, "toys.basic")
    variants(tmp_path, a={"buy": "A"})

    assert main(["sweep", snapshot.path, str(tmp_path / "variants"), "--out", str(tmp_path / "sweep.csv")]) == 0
    assert pd.read_csv(tmp_path / "sweep.csv")["variant"].tolist() == ["a", "a", "a"]
