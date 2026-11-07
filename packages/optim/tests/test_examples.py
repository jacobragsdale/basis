"""The example `optim new` copies, run end to end on its fixture, as a model repo's contract tests run it, and decided
on a few bonds where its integer program must choose well."""

from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest
from builders import EXAMPLE, Rows, loader

from optim import Decision, models
from optim.cli import main
from optim.example import model, prep
from optim.example.datasets.controls import CONTROLS
from optim.example.tests.builders import MARKET, params, portfolio
from optim.runs import replay_run, run_model
from optim.snapshots import read_info, read_results
from optim.storage import Root
from optim.testing import AS_OF, frame

EXAMPLE_MODEL = models.load("optim.example.model")


def run_example(tmp_path: Path) -> tuple[Root, str]:
    root = Root.at(tmp_path)
    ran = run_model(loader(Rows({})), root, EXAMPLE_MODEL, EXAMPLE / "params" / "default.json", as_of_date=AS_OF, data_dir=EXAMPLE / "tests" / "fixture")
    return root, ran.run_id


def test_the_example_rebalances_within_its_limits_controls_and_mandates(tmp_path: Path) -> None:
    """P1 holds 9% in B, over its 5% cap, and a wash-sale control on C; D is rated below the floor. P2 holds cash, its
    own floor of AA- rules out B, and its mandate caps Financials, B and C, at 4%."""
    root, run_id = run_example(tmp_path)

    results = read_results(root / run_id)
    trades = results["trades"].set_index(["portfolio_id", "security_id"])["quantity"]

    assert results["decisions"]["status"].tolist() == ["accepted", "accepted"]
    assert trades.to_dict() == {("P1", "A"): 54_000.0, ("P1", "E"): 55_000.0, ("P1", "B"): 46_000.0, ("P2", "A"): 24_000.0, ("P2", "C"): 19_000.0, ("P2", "E"): 25_000.0}
    assert set(results["metrics"]["metric"]) == {"income", "cost"}


def test_the_example_replays_to_the_same_trades(tmp_path: Path) -> None:
    root, run_id = run_example(tmp_path)

    replayed = replay_run(root / run_id, root)

    assert read_results(root / replayed.run_id)["trades"].equals(read_results(root / run_id)["trades"])


def decide(*bonds: dict[str, object], **datasets: pd.DataFrame) -> Decision:
    """P1, with $100,000 of cash and `bonds`, decided as a run decides it, with the default params: bonds at par, each
    of its own issuer and with a signal, trade in increments of 1,000, at least 5,000 at a time, and a partial sale
    must leave 5,000."""
    sized = ({"issuer": f"Issuer {bond['security_id']}", "min_piece": 5000.0, "min_remaining": 5000.0} | bond for bond in bonds)
    data = portfolio(*sized, cash=100_000.0, **datasets).data
    market = prep.market(data)
    return model.decide(model.prepare_portfolio(data, market, params()), market, params())


def test_the_example_never_buys_back_what_it_sells() -> None:
    """7,000 of A is 6.5% of P1, over its 5% cap, and no partial sale leaves 5,000. Selling all of A and buying 5,000
    back would keep A's income, but it is a wash sale, and two trades that dodge the market's sizes."""
    decision = decide({"security_id": "A", "held": 7000.0}, {"security_id": "B"}, {"security_id": "C"})

    assert decision.solved
    assert decision.sell == {"A": 7000.0}
    assert "A" not in decision.buy


def test_the_example_keeps_an_issuer_it_may_not_sell_over_the_issuer_cap() -> None:
    """15,000 of B is 13% of P1, over the 10% issuer cap, and a control keeps P1 from selling it."""
    controls = frame(CONTROLS, {"portfolio_id": "P1", "control_id": "R1", "security_id": "B", "action": "no_sell", "reason": "client_restriction"})

    decision = decide({"security_id": "A"}, {"security_id": "B", "held": 15_000.0}, controls=controls)

    assert decision.solved
    assert "B" not in decision.buy
    assert "B" not in decision.sell


def test_a_portfolio_with_nothing_it_may_hold_or_buy_decides_nothing() -> None:
    found = portfolio({"security_id": "A"})

    assert model.decide(replace(found, universe=found.universe.iloc[0:0]), MARKET, params()) == Decision()


def test_optim_run_on_the_fixture_decides_as_of_its_date(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """As a run from Python does: the fixture's rows are of the date in its `fixture.json`."""
    monkeypatch.setenv("OPTIM_SNAPSHOT_ROOT", str(tmp_path))

    assert main(["run", "optim.example.model", "--params", str(EXAMPLE / "params" / "default.json"), "--data", str(EXAMPLE / "tests" / "fixture")]) == 0

    run_id = capsys.readouterr().out.splitlines()[0].removeprefix("run ")
    assert read_info(Root.at(tmp_path) / run_id).as_of_date == AS_OF
