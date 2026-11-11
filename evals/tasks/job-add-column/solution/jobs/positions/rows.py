"""The columns of every table this job reads and writes: one class per table, one annotation per column. Add a column
here, then to the SQL that reads it or the transform that writes it."""

from datetime import date, datetime
from typing import Annotated

import basis.job as etl


class Trade(etl.Row):
    trade_id: Annotated[str, etl.MaxLength(32)]
    account_id: Annotated[str, etl.MaxLength(20)]
    security_id: Annotated[str, etl.MaxLength(12)]
    trade_date: date
    side: Annotated[str, etl.MaxLength(4)]
    quantity: float
    price: float
    booked_at: Annotated[datetime, etl.Zone("America/New_York")]


class Account(etl.Row):
    account_id: Annotated[str, etl.MaxLength(20)]
    name: Annotated[str, etl.MaxLength(100)]
    closed: bool


class DailyPosition(etl.Row):
    trade_date: date
    account_id: Annotated[str, etl.MaxLength(20)]
    security_id: Annotated[str, etl.MaxLength(12)]
    net_quantity: float
    gross_notional: float
    trades: float
    average_price: float
    """The trades' price, weighted by quantity: the gross notional over the quantity traded, buys and sells alike."""


class AccountActivity(etl.Row):
    account_id: Annotated[str, etl.MaxLength(20)]
    last_trade_date: date
    last_booked_at: Annotated[datetime, etl.Zone("UTC")]
