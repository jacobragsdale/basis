"""The model contract: the base classes a model's module subclasses, and the `Decision` its `decide` returns.

A model is a module, its `model.py`, that exports `Params`, `Data` and `decide(portfolio, market, params)`, plus any of
the hooks `prepare_market`, `prepare_portfolio`, `reserve`, `order` and `check`, and optionally `Metrics`.
`optim.models` loads one.
"""

from collections.abc import Mapping
from datetime import date
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator

from optim.data.dataset import Dataset
from optim.datasets import HOLDINGS, SECURITIES
from optim.schema import Model


class Params(Model):
    """Base for a model's parameters, and for any model nested in them. Strict: an unknown key is refused and no value is
    coerced, so a misspelled key or `"0.05"` for a number in a variant an agent wrote fails before any data loads,
    rather than being ignored. Document each field with a docstring under it: `optim describe` prints them."""

    model_config = ConfigDict(strict=True)


class Datasets:
    """Base for a model's `Data`. Each class attribute that is a `Dataset` is one the model reads; on an instance, the
    same attribute is that dataset's frame, so `data.holdings` is a `DataFrame` that ty checks and editors complete. A
    dataset whose rows have a `portfolio_id` is read per portfolio: a portfolio's slice of the data holds its rows alone,
    and every other dataset whole.

    Every model reads the shared holdings and securities, which acceptance needs, so they are declared here. A model
    may swap either for a dataset with the same row model, such as `holdings = HOLDINGS.file("whatif.csv")`.

    A dataset declared as `portfolios` lists the portfolios the model manages, one row each: its query takes only the
    as-of date, a run given no portfolios decides every one it lists, and each portfolio's slice holds its own row."""

    holdings = HOLDINGS
    securities = SECURITIES

    def __init__(self, frames: Mapping[str, pd.DataFrame], *, as_of_date: date, portfolio_id: str | None = None) -> None:
        self.frames = dict(frames)
        """Each dataset's frame, by the dataset's name."""
        self.as_of_date = as_of_date
        self.portfolio_id = portfolio_id
        """The portfolio a slice holds; None for the run's whole data."""


LISTING = "portfolios"
"""The attribute of a model's `Data` that lists its portfolios, if it declares one."""


def declared(data: type[Datasets]) -> dict[str, Dataset]:
    """The datasets a `Data` class reads, by attribute, its base classes' included."""
    found: dict[str, Dataset] = {name: value for klass in reversed(data.__mro__) for name, value in vars(klass).items() if isinstance(value, Dataset)}
    names = [dataset.name for dataset in found.values()]
    if repeated := sorted({name for name in names if names.count(name) > 1}):
        msg = f"{data.__qualname__} reads two datasets named {repeated}: each needs its own name, which names its frame and its snapshot file"
        raise TypeError(msg)
    return found


class Metrics(Model):
    """Base for a model's optional `Metrics`: one float field per number it reports for sweeps, each with a docstring.
    `optim describe` prints them, and a `Decision` takes an instance as its `metrics`."""


type Stop = Literal["optimal", "limit", "imprecise", "infeasible", "unbounded", "failed"]


class SolverStats(Model):
    """How a solver stopped, in its own words and in the core's."""

    solver: str
    status: str
    """The solver's own status, for diagnosis."""
    stop: Stop
    """`optimal`: proved optimal within the gap asked. `limit`: stopped by a limit, usually the time limit, before
    proving it. `imprecise`: the solver doubts its own answer. The rest found nothing to trade on."""
    gap: float | None = None
    """Integer problems with a solution: how far it may be from the best possible, relative, as the solver reports it."""
    nodes: int | None = None
    """Integer problems: branch-and-bound nodes explored."""
    iterations: int | None = None
    seconds: float | None = None
    """Time in the solver itself."""


class Decision(Model):
    """One portfolio's trades, and a report on how they were decided. `optim.cvxpy` builds one from a solved problem; a
    model with no solver builds its own, as `optim.Decision(buy={"A": 1000.0})`. Quantities are positive floats, in
    the units of the holdings, and the core rounds each to its security's trading increment before checking it."""

    buy: dict[str, float] = Field(default_factory=dict)
    """Quantity to buy, by security id. A `pd.Series` indexed by security id is taken as it is."""
    sell: dict[str, float] = Field(default_factory=dict)
    """Quantity to sell, by security id."""
    solved: bool = True
    """Whether the problem was solved: for a solver, it proved its answer optimal within the gap asked. A decision
    that was not solved is rejected, whatever its trades."""
    objective: float | None = None
    """The objective's value at the solution, in the model's own units."""
    solver: SolverStats | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    """Numbers the model reports for sweeps, such as expected income. A `Metrics` instance is taken as its fields."""

    @field_validator("buy", "sell", mode="before")
    @classmethod
    def _series(cls, value: object) -> object:
        return value.to_dict() if isinstance(value, pd.Series) else value

    @field_validator("metrics", mode="before")
    @classmethod
    def _metrics(cls, value: object) -> object:
        return value.model_dump() if isinstance(value, BaseModel) else value
