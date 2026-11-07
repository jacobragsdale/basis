"""What the model contract accepts and refuses: strict params, typed datasets, decisions, and how a model is found."""

import json
import types
from datetime import date

import numpy as np
import pandas as pd
import pytest
from pydantic import BaseModel, Field, ValidationError

import optim
from optim.contract import declared
from optim.data.dataset import Dataset
from optim.datasets import HOLDINGS, SECURITIES
from optim.models import load, read
from optim.schema import Id, Row


class Signal(Row):
    security_id: Id
    expected_return: float


class Position(Row):
    portfolio_id: Id
    security_id: Id
    quantity: float


SIGNALS = Dataset("signals", Signal, source="file", query="signals.csv", key=("security_id",))
POSITIONS = Dataset("positions", Position, source="file", query="positions.csv", key=("portfolio_id", "security_id"))


class Solve(optim.Params):
    time_limit_s: float = 30.0


class Params(optim.Params):
    max_weight: float = 0.05
    """The most of the portfolio's value in any one security."""
    issuers: list[str] = Field(default_factory=list)
    solve: Solve = Solve()


class Data(optim.Datasets):
    signals = SIGNALS


class More(Data):
    positions = POSITIONS


@pytest.mark.parametrize(
    "values",
    [{"max_wieght": 0.1}, {"max_weight": "0.05"}, {"issuers": "ABC"}, {"solve": {"time_limit_s": "30"}}, {"solve": {"time_limt_s": 30}}],
    ids=["misspelled", "string-for-number", "string-for-list", "nested-coercion", "nested-misspelled"],
)
def test_params_refuse_unknown_keys_and_coercion_nested_ones_included(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Params.model_validate_json(json.dumps(values))


def test_params_take_json_numbers_as_floats_and_document_their_fields() -> None:
    params = Params.model_validate_json('{"max_weight": 1, "solve": {"time_limit_s": 10}}')

    assert (params.max_weight, params.solve.time_limit_s) == (1.0, 10.0)
    assert "most of the portfolio" in Params.model_json_schema()["properties"]["max_weight"]["description"]


def test_a_dataset_is_its_frame_on_an_instance_and_itself_on_the_class() -> None:
    signals = pd.DataFrame({"security_id": ["A"], "expected_return": [0.05]})

    data = More({"signals": signals, "positions": pd.DataFrame()}, as_of_date=date(2026, 10, 16))

    assert data.signals is signals
    assert More.signals is SIGNALS
    assert declared(More) == {"holdings": HOLDINGS, "securities": SECURITIES, "signals": SIGNALS, "positions": POSITIONS}  # every model reads the shared two
    assert (SIGNALS.per_portfolio, POSITIONS.per_portfolio) == (False, True)


def test_two_datasets_with_one_name_are_refused() -> None:
    class Twice(optim.Datasets):
        one = SIGNALS
        other = Dataset("signals", Signal, source="file", query="other.csv", key=("security_id",))

    with pytest.raises(TypeError, match=r"two datasets named \['signals'\]"):
        declared(Twice)


def test_a_decision_takes_series_and_metrics_and_refuses_a_quantity_that_is_not_a_number() -> None:
    class Metrics(optim.Metrics):
        income: float

    decision = optim.Decision(buy=pd.Series({"A": 1000.0}), metrics=Metrics(income=50.0))

    assert (decision.buy, decision.sell, decision.metrics) == ({"A": 1000.0}, {}, {"income": 50.0})
    with pytest.raises(ValidationError):
        optim.Decision(sell={"A": np.nan})  # a solve that failed, read as if it had not


def module(**exports: object) -> types.ModuleType:
    model = types.ModuleType("toy")
    vars(model).update(exports)
    return model


def decide(portfolio: Data, market: Data, params: Params) -> optim.Decision:  # noqa: ARG001
    return optim.Decision()


def test_a_model_is_read_with_whichever_hooks_it_defines() -> None:
    model = read("toy", module(Params=Params, Data=Data, decide=decide, check=lambda *_: []))

    assert (model.params, model.data, model.decide) == (Params, Data, decide)
    assert model.check is not None
    assert model.prepare_market is model.reserve is None


class Lax(BaseModel):
    max_weight: float = 0.05


@pytest.mark.parametrize(
    ("exports", "error"), [({"Params": Lax, "Data": Data, "decide": decide}, "must subclass optim.Params"), ({"Params": Params, "Data": Data}, "has no decide")], ids=["lax-params", "no-decide"]
)
def test_a_model_that_breaks_the_contract_is_refused_when_loaded(exports: dict[str, object], error: str) -> None:
    with pytest.raises(TypeError, match=error):
        read("toy", module(**exports))


def test_an_unknown_model_names_what_is_installed() -> None:
    with pytest.raises(LookupError, match=r"no model 'nowhere\.model'"):
        load("nowhere.model")
