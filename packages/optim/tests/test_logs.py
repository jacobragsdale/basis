"""A run's id and model are on every record the run logs, and a portfolio's id on every record logged while it is
decided, worker processes' records included."""

import json
from pathlib import Path

import pytest
from builders import Rows, loader

from optim import logs, models
from optim.datasets import HOLDINGS, SECURITIES
from optim.runs import run_model
from optim.storage import Root
from optim.testing import AS_OF, CASH, frame


def test_workers_log_the_run_the_model_and_the_portfolio(capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPTIM_LOG_FORMAT", "json")
    monkeypatch.setattr(logs, "_bound", {})
    logs.setup()
    (tmp_path / "params.json").write_text('{"fail": ["P1", "P2"]}')
    source = Rows({HOLDINGS: frame(HOLDINGS, {"portfolio_id": "P1"}, {"portfolio_id": "P2"}), SECURITIES: frame(SECURITIES, CASH)})

    ran = run_model(loader(source), Root.at(tmp_path), models.load("toys.basic"), tmp_path / "params.json", as_of_date=AS_OF, portfolio_ids=["P1", "P2"], workers=2)

    records = [json.loads(line) for line in capfd.readouterr().err.splitlines()]
    failed = [r for r in records if r["message"].startswith("could not decide")]
    assert {r["portfolio_id"] for r in failed} == {"P1", "P2"}  # each logged once, where its traceback is whole
    assert all(r["run_id"] == ran.run_id and r["model"] == "toys.basic" for r in records)
