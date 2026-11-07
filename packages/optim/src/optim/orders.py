"""Writing the accepted portfolios' orders, with the run's id, to the SQL Server orders table. A run writes them only
when its environment configures the table (`OPTIM_ORDERS__*`), and only production does, so a laptop's orders stay in
its snapshot. Orders are written after the snapshot is saved, so a failed write never loses the run; a replay never
writes them. optim never submits orders: they are released elsewhere."""

from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from functools import partial
from typing import Annotated, Protocol

from pydantic import SecretStr, StringConstraints

from optim.decide import Outcome
from optim.schema import Model
from optim.sources import mssql

# The table's columns, which the orders table must have; portfolio_id may be aliased in a view of the books.
INSERT = "INSERT INTO {table} (run_id, portfolio_id, security_id, side, quantity) VALUES (%(run_id)s, %(portfolio_id)s, %(security_id)s, %(side)s, %(quantity)s)"


class OrdersSettings(Model):
    connection_string: SecretStr
    """A login that may insert into the table, apart from the read-only one that loads data."""
    table: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][\w]*(\.[A-Za-z_][\w]*){0,2}$")]
    """The table, as `schema.table`: a name, since it is written into the statement."""
    timeout_s: int = 60


class _Cursor(Protocol):
    def executemany(self, statement: str, parameters: Sequence[dict[str, object]], /) -> object: ...
    def close(self) -> object: ...


class _Connection(Protocol):
    def cursor(self) -> _Cursor: ...
    def commit(self) -> object: ...
    def close(self) -> object: ...


@dataclass(frozen=True)
class Orders:
    connect: Callable[[], _Connection]
    table: str

    def write(self, run_id: str, outcomes: Sequence[Outcome]) -> int:
        """Every trade of every accepted portfolio, in one transaction; returns how many rows it wrote."""
        rows: list[dict[str, object]] = [
            {"run_id": run_id, "portfolio_id": outcome.portfolio_id, "security_id": security, "side": side, "quantity": quantity}
            for outcome in outcomes
            if outcome.accepted and outcome.decision is not None
            for side, trades in (("buy", outcome.decision.buy), ("sell", outcome.decision.sell))
            for security, quantity in trades.items()
        ]
        if rows:
            with closing(self.connect()) as connection, closing(connection.cursor()) as cursor:
                cursor.executemany(INSERT.format(table=self.table), rows)
                connection.commit()
        return len(rows)


def connect(settings: OrdersSettings) -> Orders:
    return Orders(partial(mssql.connect_to, settings.connection_string.get_secret_value(), settings.timeout_s), settings.table)
