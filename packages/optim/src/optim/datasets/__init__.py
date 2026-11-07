"""Datasets that two or more models read, reviewed by the package's owner: one module-level declaration each, with
its row model and SQL. Every model reads `HOLDINGS`, which acceptance checks sales against, and `SECURITIES`, which
holds prices and trading increments, so `optim.Datasets` declares both. A dataset one model reads lives in that model's
repo, declared the same way, and moves here when a second model needs it. A model that needs another column of one of
these declares its own dataset with the same key and joins it in `prepare`."""

from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Literal

import pandas as pd
from pydantic import Field

from optim.data.dataset import Dataset
from optim.schema import Currency, Cusip, Id, Isin, Price, Rating, Row

SQL = Path(__file__).parent / "sql"


class Holding(Row):
    """What a portfolio holds of one security, cash included, as of now: the start of the day plus what has traded."""

    portfolio_id: Id
    security_id: Id
    quantity: float
    """Face amount for a bond, currency units for cash. Cash may be negative, as when a purchase settles before the sale
    that funds it."""


class Security(Row):
    """Reference data and a price for everything a portfolio can hold or buy, cash included."""

    security_id: Id
    """The canonical id every dataset joins on. A CUSIP or ISIN is an identifier the security may also have."""
    cusip: Cusip | None
    isin: Isin | None
    asset_class: Literal["bond", "cash", "other"]
    """`cash`: a holding of it is the portfolio's cash. `other` covers what is neither, such as a corporate-action
    position."""
    currency: Currency
    issuer: str | None
    sector: str | None
    rating: Rating | None
    maturity: date | None
    price: Price
    """Clean price per `price_per` of quantity."""
    accrued: float
    """Accrued interest per `price_per` of quantity; 0 when `price` already includes it."""
    price_per: Annotated[int, Field(gt=0)]
    """The quantity a price is quoted for: 100 for a bond quoted in percent of par, 1 for cash."""
    increment: Annotated[float, Field(gt=0)]
    """The trading increment: every trade is a whole number of them. 0.01 for cash in cents."""
    min_piece: Annotated[float, Field(gt=0)] | None
    """The smallest trade, where the market sets one."""
    min_remaining: Annotated[float, Field(ge=0)] | None
    """The smallest holding a partial sale may leave, where the market sets one."""


HOLDINGS = Dataset("holdings", Holding, source="mssql", query=SQL / "holdings.sql", key=("portfolio_id", "security_id"))
SECURITIES = Dataset("securities", Security, source="oracle", query=SQL / "securities.sql", key=("security_id",), cache=timedelta(minutes=15))


def unit_value(securities: pd.DataFrame) -> pd.Series:
    """What one unit of each security's quantity is worth, in its currency: `(price + accrued) / price_per`."""
    return (securities["price"] + securities["accrued"]) / securities["price_per"]
