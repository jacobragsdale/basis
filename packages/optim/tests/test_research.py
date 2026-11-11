"""Research on saved runs, from the snapshot alone: sweeps of params variants, regressions of recent runs against
today's code, one portfolio decided from Python, and a model described for agents."""

import json
from datetime import date
from pathlib import Path

import cvxpy as cp
import pandas as pd
import pyarrow as pa
import pytest
from fakes import EXAMPLE, Basis, Clock, Rows, loader, saved
from toys.scarce import Supply

from basis.core.errors import BasisError
from basis.core.storage import Root
from basis.core.verbs import Regress
from optim import cli, models, research
from optim.datasets import HOLDINGS, SECURITIES
from optim.runs import open_snapshot, read_snapshot, run_model
from optim.snapshots import Manifest, Saved
from optim.testing import AS_OF, CASH, frame

SOURCE = Rows(
    {
        HOLDINGS: frame(HOLDINGS, {"portfolio_id": "P1"}, {"portfolio_id": "P2", "quantity": 60_000.0}, {"portfolio_id": "P3", "quantity": 80_000.0}),
        SECURITIES: frame(SECURITIES, CASH, {"security_id": "A"}, {"security_id": "NEW"}),
    }
)


def toy_run(tmp_path: Path, model: str, *, labels: dict[str, str] | None = None, **values: object) -> Saved:
    root = Root.at(tmp_path / "snapshots")
    (tmp_path / "params.json").write_text(json.dumps(values))
    ran = run_model(loader(SOURCE), root, models.load(model), tmp_path / "params.json", as_of_date=AS_OF, portfolio_ids=["P1", "P2", "P3"], labels=labels or {}, clock=Clock())
    return saved(root, ran.run_id)


def variants(tmp_path: Path, **files: dict[str, object]) -> list[Path]:
    (directory := tmp_path / "variants").mkdir()
    for name, values in files.items():
        (directory / f"{name}.json").write_text(json.dumps(values))
    return sorted(directory.glob("*.json"))


def test_a_sweep_decides_the_snapshot_once_per_variant_into_one_table(tmp_path: Path) -> None:
    snapshot = read_snapshot(toy_run(tmp_path, "toys.basic"))

    table = research.sweep(snapshot, variants(tmp_path, a={"buy": "A"}, bad={"buy": "BAD"}, unsolved={"unsolved": ["P2"]}))

    assert table.groupby("variant")["accepted"].sum().to_dict() == {"a": 3, "bad": 0, "unsolved": 2}
    assert {"objective", "turnover", "trades", "cash", "solve_seconds", "holdings", "reasons"} <= set(table.columns)  # `holdings` is the toy's metric


def test_a_sweep_refuses_a_misspelled_variant_before_any_decides(tmp_path: Path) -> None:
    snapshot = read_snapshot(toy_run(tmp_path, "toys.basic"))

    with pytest.raises(ValueError, match=r"bad\.json"):
        research.sweep(snapshot, variants(tmp_path, a={"buy": "A"}, bad={"bye": "A"}))


def test_regress_lists_every_trade_and_status_that_today_s_code_changes(tmp_path: Path) -> None:
    """The saved run is edited as an older version of the model would have decided it: P1 bought more of A."""
    snapshot = toy_run(tmp_path, "toys.basic", labels={"purpose": "production"})
    toy_run(tmp_path, "toys.basic", labels={"purpose": "research"})
    trades = snapshot.results()["trades"]
    trades.loc[trades["portfolio_id"] == "P1", "quantity"] = 2000.0
    edited = snapshot.info.datasets | {"results/trades": snapshot.store.put(pa.Table.from_pandas(trades, preserve_index=False))}
    snapshot.path.write_bytes(Manifest.model_validate(snapshot.info.model_dump() | {"datasets": edited}).model_dump_json().encode())

    changes = research.regress(models.load("toys.basic"), snapshot.store.root, labels={"purpose": "production"})

    assert changes[["portfolio_id", "security_id", "side", "before", "after"]].to_dict("records") == [{"portfolio_id": "P1", "security_id": "A", "side": "buy", "before": 2000.0, "after": 1000.0}]
    assert research.regress(models.load("toys.basic"), snapshot.store.root, labels={"purpose": "research"}).empty

    regressions = runs.regressions if (runs := cli.part().runs) is not None else None
    assert regressions is not None, "basis check --full replays the model's runs as it does a job's"
    found = regressions(Regress("toys.basic", 20, {"purpose": "production"}, str(tmp_path / "snapshots")))
    assert found.failures == ()
    assert [(entry.name, entry.run_id, entry.changed) for entry in found.replayed] == [("toys.basic", snapshot.info.run_id, True)]
    assert [(diff.output, diff.summary) for diff in found.replayed[0].differences] == [("orders", "P1: buy A 2,000.00 is now 1,000.00")]


def test_check_full_sees_a_portfolio_whose_status_changed_and_a_model_that_fails_to_replay(tmp_path: Path) -> None:
    """The saved run is edited as an older version would have decided it: P2 rejected."""
    snapshot = toy_run(tmp_path, "toys.basic")
    decisions = snapshot.results()["decisions"]
    decisions.loc[decisions["portfolio_id"] == "P2", "status"] = "rejected"
    edited = snapshot.info.datasets | {"results/decisions": snapshot.store.put(pa.Table.from_pandas(decisions, preserve_index=False))}
    snapshot.path.write_bytes(Manifest.model_validate(snapshot.info.model_dump() | {"datasets": edited}).model_dump_json().encode())
    regressions = runs.regressions if (runs := cli.part().runs) is not None else None
    assert regressions is not None

    found = regressions(Regress("toys.basic", 20, {}, str(tmp_path / "snapshots")))
    missing = regressions(Regress("toys.nowhere", 20, {}, str(tmp_path / "snapshots")))

    assert [(diff.output, diff.summary) for diff in found.replayed[0].differences] == [("decisions", "P2: rejected is now accepted")]
    assert (missing.replayed, [type(error) for error in missing.failures]) == ((), [BasisError])  # one ERROR line, the model not found


def test_one_portfolio_decided_from_python_hands_back_what_it_saw_and_the_problem_it_solved(tmp_path: Path) -> None:
    ran = run_model(loader(Rows({})), Root.at(tmp_path), models.load("optim.example.model"), EXAMPLE / "params" / "default.json", as_of_date=date(2026, 10, 16), data_dir=EXAMPLE / "tests" / "fixture")

    trial = research.trial(open_snapshot(ran.run_id, snapshots=tmp_path), "P1")  # by its id, as a quant opens one

    assert isinstance(trial.problem, cp.Problem)
    assert trial.problem.status == "optimal"
    assert set(trial.portfolio.universe["security_id"]) == {"A", "B", "C", "D", "E"}  # ty: ignore[unresolved-attribute] -- the example's Portfolio
    assert trial.outcome.accepted
    assert trial.outcome.decision is not None
    assert trial.outcome.decision.sell == {"B": 46_000.0}


def test_one_portfolio_of_a_model_that_reserves_decides_after_those_before_it(tmp_path: Path) -> None:
    """Largest cash first: P1 takes 1,000,000 of the 1,100,000, P3 80,000, and P2 the 20,000 left."""
    trial = research.trial(read_snapshot(toy_run(tmp_path, "toys.scarce", supply=1_100_000.0)), "P2")

    assert trial.market == Supply(left=20_000.0)
    assert trial.outcome.decision is not None
    assert trial.outcome.decision.buy == {"NEW": 20_000.0}


def test_describe_tells_an_agent_what_a_model_takes_reads_and_reports(basis: Basis, capsys: pytest.CaptureFixture[str]) -> None:
    assert basis("describe", "optim.example.model") == 0

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


def test_the_sweep_command_writes_its_table(tmp_path: Path, basis: Basis) -> None:
    snapshot = toy_run(tmp_path, "toys.basic")
    variants(tmp_path, a={"buy": "A"})

    assert basis("sweep", snapshot.path.path, str(tmp_path / "variants"), "--out", str(tmp_path / "sweep.csv")) == 0
    assert pd.read_csv(tmp_path / "sweep.csv")["variant"].tolist() == ["a", "a", "a"]


def test_the_sweep_command_finds_a_run_by_its_id_in_the_environment_it_names(tmp_path: Path, basis: Basis, monkeypatch: pytest.MonkeyPatch) -> None:
    """qa's file names the store that holds the run; the process's environment, dev's, names none."""
    snapshot = toy_run(tmp_path, "toys.basic")
    variants(tmp_path, a={"buy": "A"})
    (folder := tmp_path / "config" / "basis").mkdir(parents=True)
    (folder / "qa.env").write_text(f"BASIS_ENV=qa\nBASIS_SNAPSHOT_ROOT={tmp_path / 'snapshots'}\n")
    monkeypatch.delenv("BASIS_ENV")

    assert basis("sweep", snapshot.info.run_id, str(tmp_path / "variants"), "--env", "qa", "--out", str(tmp_path / "sweep.csv")) == 0
    assert pd.read_csv(tmp_path / "sweep.csv")["variant"].tolist() == ["a", "a", "a"]


def test_the_warm_command_reads_the_cache_from_the_environment_it_names(tmp_path: Path, basis: Basis, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    (folder := tmp_path / "config" / "basis").mkdir(parents=True)
    (folder / "qa.env").write_text(f"BASIS_ENV=qa\nBASIS_SNAPSHOT_ROOT={tmp_path / 'snapshots'}\n")
    monkeypatch.setenv("BASIS_OPTIM__CACHE_ROOT", str(tmp_path / "cache"))  # the process's, which qa's file does not read
    monkeypatch.delenv("BASIS_ENV")

    assert basis("warm", "toys.basic", "--env", "qa") == 1
    assert capsys.readouterr().err.splitlines()[-1] == "ERROR: warm fills the cache, and no cache is configured: set BASIS_OPTIM__CACHE_ROOT, as .env.example shows"
