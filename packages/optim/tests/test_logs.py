"""A run's id and model are on every record the run logs, and a portfolio's id on every record logged while it is
decided, worker processes' records included. Records are basis-core's, which name optim's line where an error surfaced
and write pandas' and numpy's values as JSON."""

import json
from pathlib import Path

import numpy as np
import pytest
from fakes import Rows, loader

from basis.core import logs
from basis.core.storage import Root
from optim import models
from optim.datasets import HOLDINGS, SECURITIES
from optim.runs import run_model
from optim.testing import AS_OF, CASH, frame


def test_workers_log_the_run_the_model_and_the_portfolio(capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BASIS_LOG_FORMAT", "json")
    monkeypatch.setattr(logs, "_bound", {})
    logs.include("optim")
    logs.setup()
    (tmp_path / "params.json").write_text('{"fail": ["P1", "P2"]}')
    source = Rows({HOLDINGS: frame(HOLDINGS, {"portfolio_id": "P1"}, {"portfolio_id": "P2"}), SECURITIES: frame(SECURITIES, CASH)})

    ran = run_model(loader(source), Root.at(tmp_path), models.load("toys.basic"), tmp_path / "params.json", as_of_date=AS_OF, portfolio_ids=["P1", "P2"], workers=2)

    records = [json.loads(line) for line in capfd.readouterr().err.splitlines()]
    failed = [r for r in records if r["message"].startswith("could not decide")]
    assert {r["portfolio_id"] for r in failed} == {"P1", "P2"}  # each logged once, where its traceback is whole
    assert all(r["run_id"] == ran.run_id and r["model"] == "toys.basic" for r in records)


def test_an_error_in_optim_is_located_in_its_code() -> None:
    with pytest.raises(ValueError, match="duplicate") as raised:
        frame(HOLDINGS, {}, {})
    where = logs.describe(raised.value)["where"]

    assert isinstance(where, str)
    assert where.startswith("optim/data/dataset.py:")


def test_frames_series_and_numpy_values_log_as_json() -> None:
    holdings = frame(HOLDINGS, {}, {"security_id": "A"})

    logged = json.loads(logs.to_json({"holdings": holdings, "quantities": holdings["quantity"], "count": np.int64(2), "weights": np.array([0.5, 0.25])}))

    assert logged == {"holdings": {"rows": 2, "columns": ["portfolio_id", "security_id", "quantity"]}, "quantities": [1_000_000.0, 1_000_000.0], "count": 2, "weights": [0.5, 0.25]}
