"""Daily positions: each account's net quantity and gross notional in each security, per trade date, from the day's
trades, and each account's latest activity. A run is for an interval of trade dates; rerunning one replaces its
positions and leaves every other day's alone.

This file is the job: its params, what it reads, what it writes and how, and the transform between them. The
transform and the check are pure: they see frames, never a database, so a run replays from its snapshot."""

from datetime import date

import etl_framework as etl
import polars as pl

from etl_framework.example.rows import Account, AccountActivity, DailyPosition, Trade


class Params(etl.Params):
    include_closed_accounts: bool = False
    """Whether trades in closed accounts count."""


class Inputs(etl.Inputs):
    trades = etl.Query(Trade, database="source", sql="sql/trades.sql", empty="allow")
    accounts = etl.Query(Account, database="source", sql="sql/accounts.sql", empty="fail")


class Outputs(etl.Outputs):
    positions = etl.Table(DailyPosition, database="warehouse", table="daily_positions", key=("trade_date", "account_id", "security_id"), write=etl.ReplaceWhere(interval="trade_date"))
    activity = etl.Table(AccountActivity, database="warehouse", table="account_activity", key=("account_id",), write=etl.Upsert())


def transform(inputs: Inputs, params: Params) -> Outputs:
    trades = inputs.trades.join(inputs.accounts.select("account_id", "closed"), on="account_id", how="inner", validate="m:1")
    if not params.include_closed_accounts:
        trades = trades.filter(~pl.col("closed"))
    signed = pl.when(pl.col("side") == "BUY").then(pl.col("quantity")).otherwise(-pl.col("quantity"))
    positions = trades.group_by("trade_date", "account_id", "security_id").agg(net_quantity=signed.sum(), gross_notional=(pl.col("quantity") * pl.col("price")).sum().round(2), trades=pl.len())
    activity = trades.group_by("account_id").agg(last_trade_date=pl.col("trade_date").max(), last_booked_at=pl.col("booked_at").max())
    return Outputs(positions=positions.sort("trade_date", "account_id", "security_id"), activity=activity.sort("account_id"))


def check(inputs: Inputs, outputs: Outputs, params: Params) -> list[str]:
    """Reasons not to write, beyond the framework's own checks."""
    reasons: list[str] = []
    if unknown := inputs.trades.join(inputs.accounts, on="account_id", how="anti").height:
        reasons.append(f"{unknown} trades are in accounts the accounts table does not list")
    if (sides := set(inputs.trades.get_column("side").unique())) - {"BUY", "SELL"}:
        reasons.append(f"trades have sides other than BUY and SELL: {sorted(sides - {'BUY', 'SELL'})}")
    if inputs.interval is not None and (late := outputs.positions.filter(pl.col("trade_date") >= inputs.interval.end.date()).height):
        reasons.append(f"{late} positions are for trade dates after {date.isoformat(inputs.interval.end.date())}")
    return reasons
