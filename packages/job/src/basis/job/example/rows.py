"""The columns of every table this job reads and writes: one class per table, one annotation per column. Add a column
here, then to the SQL that reads it or the transform that writes it."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

import basis.job as etl


class Trade(etl.Row):
    trade_id: Annotated[str, etl.MaxLength(32)]
    account_id: Annotated[str, etl.MaxLength(20)]
    security_id: Annotated[str, etl.MaxLength(12)]
    trade_date: date
    side: Annotated[str, etl.MaxLength(4)]
    quantity: Annotated[Decimal, etl.Precision(18, 4)]
    price: Annotated[Decimal, etl.Precision(18, 6)]
    booked_at: Annotated[datetime, etl.Zone("America/New_York")]


class Account(etl.Row):
    account_id: Annotated[str, etl.MaxLength(20)]
    name: Annotated[str, etl.MaxLength(100)]
    closed: bool


class DailyPosition(etl.Row):
    trade_date: date
    account_id: Annotated[str, etl.MaxLength(20)]
    security_id: Annotated[str, etl.MaxLength(12)]
    net_quantity: Annotated[Decimal, etl.Precision(18, 4)]
    gross_notional: Annotated[Decimal, etl.Precision(22, 2)]
    trades: int


class AccountActivity(etl.Row):
    account_id: Annotated[str, etl.MaxLength(20)]
    last_trade_date: date
    last_booked_at: Annotated[datetime, etl.Zone("UTC")]
