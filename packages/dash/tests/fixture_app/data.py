"""The app's datasets."""

from datetime import date

import basis.dash as bd


class Position(bd.Row):
    account: str
    par: float
    as_of: date


WAREHOUSE = bd.database("warehouse", dialect="oracle", deadline_s=5, max_concurrent=2)
POSITIONS = bd.query("positions", WAREHOUSE, "sql/positions.sql", Position, binds={"as_of": date}, ttl_s=60, stale_s=3600)
