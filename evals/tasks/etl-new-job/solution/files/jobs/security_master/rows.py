"""The columns of the table this job reads and the one it writes."""

from datetime import date
from decimal import Decimal
from typing import Annotated

import etl_framework as etl


class Security(etl.Row):
    security_id: Annotated[str, etl.MaxLength(12)]
    isin: Annotated[str, etl.MaxLength(12)]
    name: Annotated[str, etl.MaxLength(100)]
    currency: Annotated[str, etl.MaxLength(3)]
    coupon: Annotated[Decimal, etl.Precision(9, 6)] | None
    maturity_date: date | None
    active: bool


class MasterSecurity(etl.Row):
    security_id: Annotated[str, etl.MaxLength(12)]
    isin: Annotated[str, etl.MaxLength(12)]
    name: Annotated[str, etl.MaxLength(100)]
    currency: Annotated[str, etl.MaxLength(3)]
    coupon: Annotated[Decimal, etl.Precision(9, 6)] | None
    maturity_date: date | None
