"""Builders for this model's tests: a portfolio as `prepare_portfolio` starts it, from a few bonds, and the params from
`default.json` with changes, so a test states only what it is about.

    found = portfolio({"security_id": "A", "sector": "Energy"}, {"security_id": "B", "held": 5000.0})
    after = screens.exclude_sectors(found, MARKET, params(exclude_sectors=["Energy"]))
"""

import json
from collections.abc import Mapping
from pathlib import Path

import pandas as pd
from optim.datasets import HOLDINGS, SECURITIES
from optim.testing import CASH, data, frame

from optim.example import model, prep
from optim.example.datasets.signals import SIGNALS

PARAMS = Path(__file__).parents[1] / "params" / "default.json"


def params(**changes: object) -> model.Params:
    """The default params, with `changes`, validated as a params file is."""
    return model.Params.model_validate_json(json.dumps(json.loads(PARAMS.read_text(encoding="utf-8")) | changes))


def portfolio(*bonds: Mapping[str, object], cash: float = 1_000_000.0, **datasets: pd.DataFrame) -> model.Portfolio:
    """Portfolio P1, prepared with the default params, before any rule: `cash`, and each bond, optim's default bond
    with the fields given, of which P1 holds `held`, 0 unless given, with an `expected_return`, 0.05 unless given, or
    no signal where it is None. `datasets` are the model's other datasets, such as `controls=frame(CONTROLS, ...)`."""
    fields = [{key: value for key, value in bond.items() if key not in ("held", "expected_return")} for bond in bonds]
    ids = [str(bond.get("security_id", "A")) for bond in bonds]
    signals = [{"security_id": i, "expected_return": bond.get("expected_return", 0.05)} for i, bond in zip(ids, bonds, strict=True) if bond.get("expected_return", 0.05) is not None]
    holdings = [{"quantity": cash}, *({"security_id": i, "quantity": bond["held"]} for i, bond in zip(ids, bonds, strict=True) if bond.get("held"))]
    found = data(model.Data, {"securities": frame(SECURITIES, CASH, *fields), "signals": frame(SIGNALS, *signals), "holdings": frame(HOLDINGS, *holdings), **datasets}, portfolio_id="P1")
    return prep.portfolio(found, prep.market(found), params())


MARKET = prep.market(data(model.Data))
"""A market of no securities, for the rules, which leave it unused."""
