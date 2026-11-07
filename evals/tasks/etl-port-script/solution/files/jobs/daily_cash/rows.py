"""The columns of the table this job reads and the one it writes, as the old script's tables declare them."""

from datetime import date
from decimal import Decimal
from typing import Annotated

import etl_framework as etl


class Movement(etl.Row):
    movement_id: Annotated[str, etl.MaxLength(32)]
    account_id: Annotated[str, etl.MaxLength(20)]
    currency: Annotated[str, etl.MaxLength(3)]
    amount: Annotated[Decimal, etl.Precision(19, 4)]
    value_date: date
    status: Annotated[str, etl.MaxLength(10)]


class DailyCash(etl.Row):
    value_date: date
    account_id: Annotated[str, etl.MaxLength(20)]
    currency: Annotated[str, etl.MaxLength(3)]
    net_amount: Annotated[Decimal, etl.Precision(19, 4)]
    movements: int
