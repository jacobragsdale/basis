"""The columns of the tables the security master reads and writes."""

from datetime import date
from typing import Annotated

import basis.job as etl


class Security(etl.Row):
    security_id: Annotated[str, etl.MaxLength(12)]
    isin: Annotated[str, etl.MaxLength(12)]
    name: Annotated[str, etl.MaxLength(100)]
    currency: Annotated[str, etl.MaxLength(3)]
    coupon: float | None
    maturity_date: date | None
    active: bool


class MasterSecurity(etl.Row):
    security_id: Annotated[str, etl.MaxLength(12)]
    isin: Annotated[str, etl.MaxLength(12)]
    name: Annotated[str, etl.MaxLength(100)]
    currency: Annotated[str, etl.MaxLength(3)]
    coupon: float | None
    maturity_date: date | None
