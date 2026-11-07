"""Daily cash: each account's net cash movement per currency and value date, for the run's interval of value dates.
Cancelled movements do not count. Ported from the nightly script `daily_cash.py`, which deleted the day's rows and
inserted them again, as `ReplaceWhere` does."""

import etl_framework as etl
import polars as pl

from jobs.daily_cash.rows import DailyCash, Movement

CURRENCY_LENGTH = 3
"""An ISO 4217 currency code's length."""


class Params(etl.Params):
    """This job has no params."""


class Inputs(etl.Inputs):
    movements = etl.Query(Movement, database="ops", sql="sql/movements.sql", empty="allow")


class Outputs(etl.Outputs):
    daily_cash = etl.Table(DailyCash, database="warehouse", table="dbo.daily_cash", key=("value_date", "account_id", "currency"), write=etl.ReplaceWhere(interval="value_date"))


def _counted(movements: pl.DataFrame) -> pl.DataFrame:
    return movements.filter(pl.col("status") != "CANCELLED")


def transform(inputs: Inputs, params: Params) -> Outputs:
    totals = _counted(inputs.movements).group_by("value_date", "account_id", "currency").agg(net_amount=pl.col("amount").sum(), movements=pl.len())
    return Outputs(daily_cash=totals.sort("value_date", "account_id", "currency"))


def check(inputs: Inputs, outputs: Outputs, params: Params) -> list[str]:
    """The script's own check: a counted movement's currency is three upper-case letters."""
    currency = pl.col("currency")
    bad = _counted(inputs.movements).filter((currency.str.len_chars() != CURRENCY_LENGTH) | (currency != currency.str.to_uppercase()))
    return [f"movements {sorted(bad.get_column('movement_id').to_list())} have a bad currency"] if bad.height else []
