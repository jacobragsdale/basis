"""The portfolios this model manages, one row each, with what a client set for its own portfolio. Declared as `Data`'s
`portfolios`, it lists the portfolios a run decides when it is given none, and each portfolio's hooks see its own row."""

from pathlib import Path

from optim import Dataset, Row
from optim.schema import Id, Rating

SQL = Path(__file__).parent / "sql"


class Managed(Row):
    """A portfolio the model manages on the business date."""

    portfolio_id: Id
    min_rating: Rating | None
    """The client's own rating floor, where it is stricter than the model's: buy only bonds rated this or better."""


PORTFOLIOS = Dataset("portfolios", Managed, source="mssql", query=SQL / "portfolios.sql", key=("portfolio_id",))
