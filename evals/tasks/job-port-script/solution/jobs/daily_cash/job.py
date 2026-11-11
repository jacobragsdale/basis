"""Daily cash: each account's net cash movement per currency on each value date, from the ops database's movements,
ported from the nightly legacy/daily_cash.py. A cancelled movement does not count. A run is for an interval of value
dates, and rerunning one replaces its rows. It refuses to write when a counted movement's currency is not three capital
letters, as the script did."""

import basis.job as etl

from jobs.daily_cash.rows import DailyCash, Movement


class Params(etl.Params):
    pass


class Inputs(etl.Inputs):
    movements = etl.Query(Movement, database="ops", sql="sql/movements.sql", empty="allow")


class Outputs(etl.Outputs):
    daily_cash = etl.Table(DailyCash, database="warehouse", table="daily_cash", key=("value_date", "account_id", "currency"), write=etl.ReplaceWhere(interval="value_date"))


def transform(inputs: Inputs, params: Params) -> Outputs:
    counted = inputs.movements[inputs.movements["status"] != "CANCELLED"]
    totals = counted.groupby(["value_date", "account_id", "currency"], as_index=False).agg(net_amount=("amount", "sum"), movements=("movement_id", "size"))
    return Outputs(daily_cash=totals.sort_values(["value_date", "account_id", "currency"], ignore_index=True))


def check(inputs: Inputs, outputs: Outputs, params: Params) -> list[str]:
    """Reasons not to write, beyond the framework's own checks."""
    counted = inputs.movements[inputs.movements["status"] != "CANCELLED"]
    bad = counted[~counted["currency"].str.fullmatch(r"[A-Z]{3}")]
    return [f"movement {movement} has a bad currency {currency!r}: nothing written" for movement, currency in zip(bad["movement_id"], bad["currency"], strict=True)]
