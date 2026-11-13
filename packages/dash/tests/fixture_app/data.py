"""The app's datasets."""

from datetime import date

import pandas as pd

import basis.dash as bd


class Position(bd.Row):
    account: str
    par: float
    as_of: date


WAREHOUSE = bd.database("warehouse", dialect="oracle", deadline_s=5, max_concurrent=2)
POSITIONS = bd.query("positions", WAREHOUSE, "sql/positions.sql", Position, binds={"as_of": date}, ttl_s=60, stale_s=3600, reload_s=30)


class Total(bd.Row):
    par: float


def total(positions: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"par": [float(positions["par"].sum())]})


TOTAL = bd.view("total", total, Total, positions=POSITIONS)
