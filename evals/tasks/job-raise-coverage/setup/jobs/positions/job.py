"""Daily positions: each account's net quantity and gross notional in each security, per trade date, from the day's
trades, and each account's latest activity. A run is for an interval of trade dates; rerunning one replaces its
positions and leaves every other day's alone.

This file is the job: its params, what it reads, what it writes and how, and the transform between them. The
transform and the check are pure: they see pandas frames, never a database, so a run replays from its snapshot. Every
number is a float, numpy's, and a datetime keeps its zone."""

from datetime import date

import basis.job as etl

from jobs.positions.rows import Account, AccountActivity, DailyPosition, Trade

CALENDAR = etl.calendar("sifma-us")
"""The bond market's business days, which a scheduled run's `--interval previous-business-day` is resolved in."""


class Params(etl.Params):
    include_closed_accounts: bool = False
    """Whether trades in closed accounts count."""
    max_price_move: float | None = None
    """The most a security's price may move within a day, as a fraction of its lowest price that day; by default, any
    move is taken."""


class Inputs(etl.Inputs):
    trades = etl.Query(Trade, database="source", sql="sql/trades.sql", empty="allow")
    accounts = etl.Query(Account, database="source", sql="sql/accounts.sql", empty="fail")


class Outputs(etl.Outputs):
    positions = etl.Table(DailyPosition, database="warehouse", table="daily_positions", key=("trade_date", "account_id", "security_id"), write=etl.ReplaceWhere(interval="trade_date"))
    activity = etl.Table(AccountActivity, database="warehouse", table="account_activity", key=("account_id",), write=etl.Upsert())


def transform(inputs: Inputs, params: Params) -> Outputs:
    trades = inputs.trades.merge(inputs.accounts[["account_id", "closed"]], on="account_id", how="inner", validate="m:1")
    if not params.include_closed_accounts:
        trades = trades[~trades["closed"]]
    trades = trades.assign(signed=trades["quantity"].where(trades["side"] == "BUY", -trades["quantity"]), notional=trades["quantity"] * trades["price"])
    positions = trades.groupby(["trade_date", "account_id", "security_id"], as_index=False).agg(net_quantity=("signed", "sum"), gross_notional=("notional", "sum"), trades=("trade_id", "size"))
    activity = trades.groupby("account_id", as_index=False).agg(last_trade_date=("trade_date", "max"), last_booked_at=("booked_at", "max"))
    return Outputs(positions=positions.sort_values(["trade_date", "account_id", "security_id"], ignore_index=True), activity=activity.sort_values("account_id", ignore_index=True))


def check(inputs: Inputs, outputs: Outputs, params: Params) -> list[str]:
    """Reasons not to write, beyond the framework's own checks."""
    reasons: list[str] = []
    if unknown := int((~inputs.trades["account_id"].isin(inputs.accounts["account_id"])).sum()):
        reasons.append(f"{unknown} trades are in accounts the accounts table does not list")
    if sides := set(inputs.trades["side"]) - {"BUY", "SELL"}:
        reasons.append(f"trades have sides other than BUY and SELL: {sorted(sides)}")
    if inputs.interval is not None and (late := int((outputs.positions["trade_date"] >= inputs.interval.end.date()).sum())):
        reasons.append(f"{late} positions are for trade dates after {date.isoformat(inputs.interval.end.date())}")
    if params.max_price_move is not None:
        reasons += _price_moves(inputs, params.max_price_move)
    return reasons


def _price_moves(inputs: Inputs, largest: float) -> list[str]:
    """A reason for each security whose price moved more than `largest` within a day."""
    prices = inputs.trades.groupby(["trade_date", "security_id"], as_index=False).agg(low=("price", "min"), high=("price", "max"))
    moved = prices[(prices["high"] - prices["low"]) > largest * prices["low"]]
    found = []
    for day, security in zip(moved["trade_date"], moved["security_id"], strict=True):
        found.append(f"{security}'s price moved more than {largest:.0%} on {day}")
    return found
