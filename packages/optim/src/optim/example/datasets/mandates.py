"""Each portfolio's mandates, from SQL Server: the most of its value a client allows in a sector. `rules/mandates.py`
turns a portfolio's rows into its sector caps."""

from pathlib import Path

from optim import Dataset, Row
from optim.schema import Id, Weight

SQL = Path(__file__).parent / "sql"


class Mandate(Row):
    """A client's cap on one sector of their portfolio."""

    portfolio_id: Id
    mandate_id: Id
    sector: str
    max_weight: Weight
    """The most of the portfolio's value in the sector after trading, as a fraction: 0.25 is 25%."""


MANDATES = Dataset("mandates", Mandate, source="mssql", query=SQL / "mandates.sql", key=("portfolio_id", "mandate_id"))
