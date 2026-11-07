"""Data prep: joining datasets into what the rules and the optimizer read. `market` builds what every portfolio shares,
once per run; `portfolio` builds one portfolio's universe, and its sector caps from the params, before any rule changes
them. Change this file to join a new dataset in, or to add a column every rule can read."""

import pandas as pd
from optim.datasets import unit_value

from optim.example import model


def market(data: model.Data) -> model.Market:
    """Every security with its `unit_value`, and its `expected_return` from the signals, 0 without one. A bond without a
    signal cannot be bought: there is no view on it."""
    joined = data.securities.merge(data.signals, on="security_id", how="left", validate="one_to_one")
    return model.Market(joined.assign(unit_value=unit_value(joined), has_signal=joined["expected_return"].notna(), expected_return=joined["expected_return"].fillna(0.0)))


def portfolio(data: model.Data, market: model.Market, params: model.Params) -> model.Portfolio:
    """The portfolio as the rules first see it: a row per security it holds, cash aside, or may buy, which is every bond
    with a signal; its cash and value; and the params' sector caps."""
    holdings, securities = data.holdings, market.securities
    if unknown := sorted(set(holdings["security_id"]) - set(securities["security_id"])):
        msg = f"holds securities that are not loaded: {unknown[:5]}"
        raise LookupError(msg)
    held = holdings.groupby("security_id")["quantity"].sum().rename("held").reset_index()
    candidates = securities[(securities["asset_class"] == "bond") & securities["has_signal"]][["security_id"]]
    ids = pd.concat([held[["security_id"]], candidates], ignore_index=True).drop_duplicates()
    rows = ids.merge(securities, on="security_id", how="left", validate="one_to_one").merge(held, on="security_id", how="left", validate="one_to_one")
    rows = rows.assign(held=rows["held"].fillna(0.0))
    cash = float((rows.loc[rows["asset_class"] == "cash", "held"] * rows.loc[rows["asset_class"] == "cash", "unit_value"]).sum())
    universe = rows[rows["asset_class"] != "cash"].reset_index(drop=True)
    if (short := universe["held"] < 0).any():
        msg = f"holds negative quantities of securities that are not cash, e.g. {universe.loc[short, 'security_id'].head(3).tolist()}: short positions are not modelled"
        raise ValueError(msg)
    universe = universe.assign(buyable=(universe["asset_class"] == "bond") & universe["has_signal"], sellable=universe["held"] > 0, must_sell=False)
    value = cash + float((universe["held"] * universe["unit_value"]).sum())
    return model.Portfolio(data=data, universe=universe, sector_caps=dict(params.sector_caps), cash=cash, value=value)
