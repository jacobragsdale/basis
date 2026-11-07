"""Writing the accepted portfolios' orders, with the run's id, to the orders table. A run writes them only when its
environment configures the table (`BASIS_OPTIM__ORDERS__*`), and only production does, so a laptop's orders stay in
its snapshot. The table is in a database basis-core's settings configure, and the orders go through that database's
write login, apart from the read login that loads data, in one transaction. Orders are written after the snapshot is
saved, so a failed write never loses the run; a replay never writes them. optim never submits orders: they are
released elsewhere."""

from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from pydantic import StringConstraints

from basis.core import settings as core
from basis.core.databases import Statements, open_database
from basis.core.tables import IDENTIFIER
from optim.decide import Outcome
from optim.schema import Model

COLUMNS = ("run_id", "portfolio_id", "security_id", "side", "quantity")
"""The table's columns, which the orders table must have; portfolio_id may be aliased in a view of the books."""
_BIND = {"mssql": "%({})s", "oracle": ":{}"}


class OrdersSettings(Model):
    database: Annotated[str, StringConstraints(pattern=IDENTIFIER.pattern)]
    """The database that holds the table, by the name its `BASIS_DATABASES__NAME__*` block gives: the orders go through
    its write login, `BASIS_DATABASES__NAME__WRITE__*`, a login that may insert into the table."""
    table: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][\w]*(\.[A-Za-z_][\w]*){0,2}$")]
    """The table, as `schema.table`: a name, since it is written into the statement."""


class Writable(Protocol):
    """What the orders need of basis-core's `Database`."""

    def statements(self, *, writes: bool) -> AbstractContextManager[Statements]: ...


@dataclass(frozen=True)
class Orders:
    database: Writable
    dialect: Literal["mssql", "oracle"]
    table: str
    timeout_s: float
    """Each insert's deadline: the database's own `TIMEOUT_S`."""

    def write(self, run_id: str, outcomes: Sequence[Outcome]) -> int:
        """Every trade of every accepted portfolio, in one transaction; returns how many rows it wrote."""
        rows: list[dict[str, str | float]] = [
            {"run_id": run_id, "portfolio_id": outcome.portfolio_id, "security_id": security, "side": side, "quantity": quantity}
            for outcome in outcomes
            if outcome.accepted and outcome.decision is not None
            for side, trades in (("buy", outcome.decision.buy), ("sell", outcome.decision.sell))
            for security, quantity in trades.items()
        ]
        if rows:
            binds = ", ".join(_BIND[self.dialect].format(column) for column in COLUMNS)
            statement = f"INSERT INTO {self.table} ({', '.join(COLUMNS)}) VALUES ({binds})"  # noqa: S608 -- the table is a name the settings' pattern allows; every value is bound
            with self.database.statements(writes=True) as statements:
                for row in rows:
                    statements.execute(statement, row, timeout_s=self.timeout_s)
                statements.commit()
        return len(rows)


def connect(settings: core.Settings, orders: OrdersSettings) -> Orders:
    """The orders table `orders` names, in the database `settings` configure. Opening connects to nothing."""
    database = settings.database(orders.database)
    return Orders(open_database(settings, orders.database), database.kind, orders.table, database.timeout_s)
