"""The columns of the tables daily cash reads and writes."""

from datetime import date
from typing import Annotated

import basis.job as etl


class Movement(etl.Row):
    movement_id: Annotated[str, etl.MaxLength(32)]
    account_id: Annotated[str, etl.MaxLength(20)]
    currency: Annotated[str, etl.MaxLength(3)]
    amount: float
    """Signed: receipts are positive, payments negative."""
    value_date: date
    status: Annotated[str, etl.MaxLength(10)]
    """PENDING, SETTLED or CANCELLED."""


class DailyCash(etl.Row):
    value_date: date
    account_id: Annotated[str, etl.MaxLength(20)]
    currency: Annotated[str, etl.MaxLength(3)]
    net_amount: float
    movements: float
