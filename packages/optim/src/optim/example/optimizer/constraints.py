"""The model's constraints, in cvxpy, on the `buy` and `sell` that `problem.build` makes, one function each: the sizes
trades come in, cash, turnover, and caps on weights. Each returns a list, which `build` adds to the problem."""

from collections.abc import Mapping

import cvxpy as cp
import numpy as np
import pandas as pd
import scipy.sparse as sp

from optim.example import model


def sizes(universe: pd.DataFrame, buy: cp.Variable, sell: cp.Variable, *, value: float) -> list[cp.Constraint]:
    """Buys are nothing or whole increments of at least the minimum piece, of bonds that may be bought. A sale is the
    whole holding, which is how an odd lot goes and how a mandatory sale must go, or whole increments of at least the
    minimum piece that leave at least the minimum remaining, of bonds that may be sold. A bond is bought or sold, never
    both: selling it to buy it back is a wash sale, and two trades that dodge the sizes the market trades."""
    held, unit, increment = (universe[column].to_numpy(dtype=float) for column in ("held", "unit_value", "increment"))
    min_piece = universe["min_piece"].fillna(universe["increment"]).to_numpy(dtype=float)
    min_remaining = universe["min_remaining"].fillna(0.0).to_numpy(dtype=float)
    buyable, sellable, must_sell = (universe[column].to_numpy(dtype=bool) for column in ("buyable", "sellable", "must_sell"))

    n = len(universe)
    buy_steps, buying = cp.Variable(n, integer=True), cp.Variable(n, boolean=True)
    sell_steps, part, whole = cp.Variable(n, integer=True), cp.Variable(n, boolean=True), cp.Variable(n, boolean=True)
    partial = cp.multiply(increment, sell_steps)
    return [
        buy == cp.multiply(increment, buy_steps),
        buy_steps >= 0,
        buy >= cp.multiply(min_piece, buying),
        buy <= cp.multiply(np.where(buyable, value / unit, 0.0), buying),
        sell == cp.multiply(held, whole) + partial,
        sell_steps >= 0,
        partial >= cp.multiply(min_piece, part),
        partial <= cp.multiply(np.where(sellable, np.maximum(held - min_remaining, 0.0), 0.0), part),
        whole + part <= sellable.astype(float),
        whole >= must_sell.astype(float),
        buying + whole + part <= 1,
    ]


def cash(portfolio: model.Portfolio, buy: cp.Variable, sell: cp.Variable) -> list[cp.Constraint]:
    """Cash after trading is not negative: what it buys, its cash and its sales pay for."""
    return [portfolio.cash + portfolio.universe["unit_value"].to_numpy(dtype=float) @ (sell - buy) >= 0]


def turnover(universe: pd.DataFrame, buy: cp.Variable, sell: cp.Variable, cap: float, *, value: float) -> list[cp.Constraint]:
    """What it buys and sells together at most `cap` of the portfolio's value."""
    return [(universe["unit_value"].to_numpy(dtype=float) / value) @ (buy + sell) <= cap]


def max_weight(universe: pd.DataFrame, buy: cp.Variable, sell: cp.Variable, per: str, cap: float | Mapping[str, float], *, value: float) -> list[cp.Constraint]:
    """Each group of rows with the same `per`, such as each issuer, at most `cap` of the portfolio's value after
    trading; or, where `cap` is a cap by group, such as `{"Financials": 0.25}` per sector, each group it names. A cap
    leaves the portfolio what it may not sell: it may keep that, however much it weighs."""
    groups = pd.get_dummies(universe[per])
    caps = pd.Series(cap, dtype=float).reindex(groups.columns).dropna() if isinstance(cap, Mapping) else pd.Series(cap, index=groups.columns, dtype=float)
    if caps.empty:
        return []
    member = groups[caps.index].to_numpy(dtype=float).T
    unit, held = (universe[column].to_numpy(dtype=float) for column in ("unit_value", "held"))
    kept = np.where(universe["sellable"].to_numpy(dtype=bool), 0.0, unit * held / value)
    return [sp.csr_array(member) @ cp.multiply(unit / value, held + buy - sell) <= np.maximum(caps.to_numpy(), member @ kept)]
