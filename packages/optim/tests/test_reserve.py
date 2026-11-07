"""Portfolios that compete for a scarce bond decide one at a time, in the model's order, each seeing the supply the
earlier ones left; only an accepted decision reserves, and a replay of one portfolio decides those before it again."""

import json
from pathlib import Path

import pytest
from builders import Clock, Rows, loader

from optim import models
from optim.datasets import HOLDINGS, SECURITIES
from optim.runs import replay_run, run_model
from optim.snapshots import read_info
from optim.storage import Root
from optim.testing import AS_OF, CASH, frame

SCARCE = models.load("toys.scarce")
CASH_HELD = {"P1": 100_000.0, "P2": 60_000.0, "P3": 80_000.0}  # largest first: P1, then P3, then P2
SOURCE = Rows({HOLDINGS: frame(HOLDINGS, *({"portfolio_id": p, "quantity": q} for p, q in CASH_HELD.items())), SECURITIES: frame(SECURITIES, CASH, {"security_id": "NEW"})})


def bought(root: Root, tmp_path: Path, *, workers: int = 1, **values: object) -> tuple[str, dict[str, float]]:
    (tmp_path / "params.json").write_text(json.dumps(values))
    ran = run_model(loader(SOURCE), root, SCARCE, tmp_path / "params.json", as_of_date=AS_OF, portfolio_ids=list(CASH_HELD), workers=workers, clock=Clock())
    return ran.run_id, {o.portfolio_id: o.decision.buy.get("NEW", 0.0) if o.decision and o.accepted else 0.0 for o in ran.outcomes}


@pytest.mark.parametrize("workers", [1, 2], ids=["in-process", "prepared-in-workers"])
def test_portfolios_decide_in_turn_in_the_models_order_each_from_what_the_earlier_left(tmp_path: Path, workers: int) -> None:
    root = Root.at(tmp_path / "snapshots")

    run_id, buys = bought(root, tmp_path, workers=workers)

    assert buys == {"P1": 100_000.0, "P2": 0.0, "P3": 50_000.0}  # P1 first, then P3 takes the 50,000 left
    assert read_info(root / run_id).order == ["P1", "P3", "P2"]


def test_a_rejected_decision_reserves_nothing_so_its_supply_goes_to_the_next(tmp_path: Path) -> None:
    _, buys = bought(Root.at(tmp_path), tmp_path, unsolved=["P1"])

    assert buys == {"P1": 0.0, "P2": 60_000.0, "P3": 80_000.0}


def test_replaying_one_portfolio_decides_the_portfolios_before_it_again(tmp_path: Path) -> None:
    root = Root.at(tmp_path / "snapshots")
    run_id, _ = bought(root, tmp_path, order=["P1", "P2", "P3"])

    replayed = replay_run(root / run_id, root, portfolio_ids=["P3"])

    assert [(o.portfolio_id, o.decision and o.decision.buy) for o in replayed.outcomes] == [("P3", {})]  # P1 and P2 took it all
    assert read_info(root / replayed.run_id).order == ["P1", "P2", "P3"]


def test_an_order_that_drops_a_portfolio_fails_the_run(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must return every prepared portfolio once"):
        bought(Root.at(tmp_path), tmp_path, order=["P1", "P3"])
