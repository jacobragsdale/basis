"""Pydantic building blocks for every boundary: the base models, and the field types for securities, holdings and money
that rows, params and settings share."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class Model(BaseModel):
    """Base for every model that crosses a boundary: settings, job requests, database rows. Lax, so exact
    conversions pass (a driver's `Decimal` for a float, Oracle's midnight `datetime` for a date, an ISO string, `1000.0`
    for an int) and lossy ones fail (`1000.5` for an int, `5` for a str). Document a field with a docstring under it, not a comment:
    it becomes the field's description in the model's JSON schema, which `optim describe` prints for params."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True, revalidate_instances="always", allow_inf_nan=False, use_attribute_docstrings=True)


class Row(Model):
    """Schema of one dataset row. Field names are column names; annotations pick the pandas dtype (`optim.data.frames`).
    Field docstrings describe the columns."""


Id = Annotated[str, StringConstraints(min_length=1, max_length=64)]  # a key from a source system: security, portfolio, sleeve
RunId = Annotated[str, StringConstraints(pattern=r"^[0-9A-Za-z][0-9A-Za-z._-]{0,127}$")]  # names a snapshot directory
Cusip = Annotated[str, StringConstraints(pattern=r"^[0-9A-Z*@#]{8}[0-9]$")]
Isin = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}[0-9A-Z]{9}[0-9]$")]
Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]  # ISO 4217
# Booked values are floats, as every other number: arithmetic on `Decimal` columns fails or is slow. Rounding to each
# security's trading increment is what makes trade quantities exact.
Price = Annotated[float, Field(gt=0)]
Quantity = Annotated[float, Field(gt=0)]  # face amount for a bond, currency units for cash
Par = Annotated[int, Field(gt=0)]  # a trading denomination, in whole units of quantity
Weight = Annotated[float, Field(ge=0, le=1)]
Rating = Literal[
    "AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-",
    "BB+", "BB", "BB-", "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC", "C", "D",
]  # fmt: skip  # best first: an ordered category, so `rating <= "A-"` means "A- or better"
