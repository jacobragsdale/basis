"""The accepted portfolios' orders: what every run saves in its snapshot, and what `basis run --publish` writes to the
orders table through basis-core's publishing (`basis.core.publish`). The table is in a database basis-core's settings
configure, and is written through that database's write login, apart from the read login that loads data: one batched
insert, in one transaction with the run's row in that database's ledger (`BASIS_DATABASES__NAME__WRITE__LEDGER`).

The ledger is what makes a run's orders written once. A run the ledger holds is never written again, however often its
publish is retried or resumed (`basis resume`). A run's interval in the ledger is its as-of date, so a second run of the
same model for that date is refused, before it loads and again as it writes, unless it is given `--again`, as basis-job
refuses a second append of an interval. A run with no orders publishes nothing, not even a ledger row, so a retry after
every portfolio was rejected is not refused.

Orders are written after the snapshot is saved, so a failed write never loses the run, and `basis resume` writes the
same rows from the snapshot. A replay never writes them, and optim never submits them: they are released elsewhere."""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Literal, cast

import pandas as pd
import polars as pl
from pydantic import StringConstraints

import optim
from basis.core import publish
from basis.core import settings as core
from basis.core.databases import Database
from basis.core.destination import Dialect, check_table
from basis.core.errors import BasisError
from basis.core.interval import Interval
from basis.core.runs import opener
from basis.core.snapshots import PublishRecord
from basis.core.tables import IDENTIFIER, Changes, Column
from optim.data.frames import conform
from optim.decide import Outcome
from optim.schema import Id, Model, Row

log = logging.getLogger(__name__)
OUTPUT = "orders"
"""What the ledger calls the orders a run published, beside its model's module."""
COLUMNS = (
    Column("run_id", "str", nullable=False, dtype=pl.String(), max_length=36),
    Column("portfolio_id", "str", nullable=False, dtype=pl.String(), max_length=64),
    Column("security_id", "str", nullable=False, dtype=pl.String(), max_length=64),
    Column("side", "str", nullable=False, dtype=pl.String(), max_length=4),
    Column("quantity", "float", nullable=False, dtype=pl.Float64()),
)
"""The orders table's columns, as optim 0.2.0 wrote them; portfolio_id may be aliased in a view of the books. The
lengths are those of a run's id and of `optim.schema.Id`, for `basis ddl`; the table's own are what a publish checks."""
KEY = ("run_id", "portfolio_id", "security_id", "side")
"""A run orders a security once per side of a portfolio: the primary key `basis ddl` gives the table."""
_BIND = {"mssql": "%({})s", "oracle": ":{}"}
_COUNTS = ("deleted", "upserted", "inserted_new", "inserted")


class OrdersRefusedError(BasisError, RuntimeError):
    """Orders that cannot be published where the settings say, found before a run loads anything."""


class OrdersSettings(Model):
    database: Annotated[str, StringConstraints(pattern=IDENTIFIER.pattern)]
    """The database that holds the table, by the name its `BASIS_DATABASES__NAME__*` block gives: the orders go through
    its write login, `BASIS_DATABASES__NAME__WRITE__*`, a login that may insert into the table and its ledger."""
    table: Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][\w]*(\.[A-Za-z_][\w]*){0,2}$")]
    """The table, as `schema.table`: a name, since it is written into the statement."""


class Publishing(Model):
    """Where a run publishes its orders, as `basis run --publish` asked: recorded in its manifest before it writes, so
    that `basis resume` writes them to the same table."""

    database: str
    table: str
    again: bool
    """Whether it publishes even though another run of its model published orders for its as-of date."""


class OrderRow(Row):
    """An accepted portfolio's trade, rounded, as a run's snapshot saves it; the table adds the run's id."""

    portfolio_id: Id
    security_id: Id
    side: Literal["buy", "sell"]
    quantity: float
    """Rounded to the security's increment, and never 0."""


def orders_of(outcomes: Sequence[Outcome]) -> pd.DataFrame:
    """Every trade of every accepted portfolio, as `OrderRow`s, in the run's portfolio order."""
    rows = [
        {"portfolio_id": outcome.portfolio_id, "security_id": security, "side": side, "quantity": quantity}
        for outcome in outcomes
        if outcome.accepted and outcome.decision is not None
        for side, trades in (("buy", outcome.decision.buy), ("sell", outcome.decision.sell))
        for security, quantity in trades.items()
    ]
    return conform(OrderRow, [pd.DataFrame(rows)], name=OUTPUT)


@dataclass(frozen=True)
class Table:
    """The orders table, as basis-core's publishing and DDL take a table (`basis.core.tables.Target`)."""

    database: str
    table: str

    @property
    def key(self) -> tuple[str, ...]:
        return KEY

    @property
    def columns(self) -> tuple[Column, ...]:
        return COLUMNS

    def column(self, name: str) -> Column:
        for column in COLUMNS:
            if column.name == name:
                return column
        msg = f"the orders table has no column {name!r}; it has {[column.name for column in COLUMNS]}"
        raise KeyError(msg)


@dataclass(frozen=True)
class Published:
    """What publishing a run's orders did, by database."""

    records: dict[str, PublishRecord]
    """Each database's outcome; none for a run with no orders."""
    earlier: bool = False
    """Whether an earlier attempt had committed the orders, which the ledger held: `records` then hold what that
    attempt's database counted, and when it committed."""


@dataclass(frozen=True)
class Orders:
    """Where `basis run --publish` writes a run's orders: the table, in the databases basis-core's settings open."""

    table: Table
    databases: Callable[[str], Database]
    dialect: Dialect
    ledger: str
    """The orders database's ledger, `BASIS_DATABASES__NAME__WRITE__LEDGER`."""
    timeout_s: float
    """Each statement's deadline: the database's own `TIMEOUT_S`."""

    def refusals(self, model: str, as_of_date: date, *, again: bool) -> list[str]:
        """Why a run of `model` for `as_of_date` cannot publish here, found before it loads: the table is missing or
        cannot take the orders' columns, or another run of the model published orders for that date and the run is
        not given `again`. Read through the write login, which sees the table as the publish will, and changes
        nothing; the publish checks each again, in its own transaction."""
        with self.databases(self.table.database).writing() as writer:
            if (spec := writer.describe(self.table.table)) is None:
                return [f"{self.table.table} does not exist in database {self.table.database}: `basis ddl MODEL --dialect DIALECT` prints a CREATE TABLE for it"]
            reasons = check_table(self.table, spec)
            if not again and (runs := writer.overlapping(model, OUTPUT, interval(as_of_date))):
                reasons.append(f"runs {', '.join(runs)} of {model} already published orders for {as_of_date}: pass --again to publish this run's too")
        return reasons

    def publish(self, run_id: str, model: str, as_of_date: date, orders: pd.DataFrame, *, again: bool, clock: Callable[[], datetime]) -> Published:
        """Write `orders`, a run's `OrderRow`s, each with the run's id, and the run's ledger row, in one transaction; a
        run with no orders writes nothing. A run the ledger holds already is not written again, and its outcome is the
        ledger's: what the attempt that committed it counted, and when."""
        if orders.empty:
            log.info("no orders to publish", extra={"database": self.table.database})
            return Published({})
        publication = publish.Publication(run_id, model, interval(as_of_date), optim.__version__, again, clock)
        records = publish.publish(publication, {OUTPUT: self.table}, {OUTPUT: Changes(insert=rows(run_id, orders))}, self.databases)
        found = records.get(self.table.database)
        if found is None or found.status != "committed" or found.counts:
            return Published(records)
        # basis-core's publish counts nothing for a run the ledger holds, which it leaves as it is
        if (earlier := self.ledgered(run_id)) is None:
            msg = f"the ledger {self.ledger} held run {run_id} as it published, and holds no row of its orders now"
            raise LookupError(msg)
        log.info("an earlier attempt published the orders", extra={"database": self.table.database, "published_at": earlier.at})
        return Published({self.table.database: earlier}, earlier=True)

    def ledgered(self, run_id: str) -> PublishRecord | None:
        """The ledger's row of the run's orders, as a publish's record: when they committed, and what the database
        counted; None where it holds none. Read through the write login, which reads the ledger to publish."""
        bind = _BIND[self.dialect]
        sql = f"SELECT {', '.join(_COUNTS)}, published_at FROM {self.ledger} WHERE run_id = {bind.format('run_id')} AND output = {bind.format('output')}"  # noqa: S608 -- the ledger is a name its setting's pattern allows; every value is bound
        with self.databases(self.table.database).statements(writes=True) as statements:
            found = cast("list[dict[str, object]]", statements.query(sql, {"run_id": run_id, "output": OUTPUT}, timeout_s=self.timeout_s).to_pylist())  # pyarrow is untyped
        if not found:
            return None
        [row] = found  # the ledger's key is the run and the output
        at = row["published_at"]
        if not isinstance(at, datetime):
            msg = f"the ledger {self.ledger} holds {at!r} as run {run_id}'s published_at, not a time"
            raise TypeError(msg)
        return PublishRecord(status="committed", at=at.replace(tzinfo=UTC), counts={OUTPUT: {name: _count(row[name]) for name in _COUNTS}})


def interval(as_of_date: date) -> Interval:
    """A run's interval in the ledger: its as-of date, which a second run of its model for that date overlaps."""
    start = datetime.combine(as_of_date, time(), UTC)
    return Interval(start, start + timedelta(days=1))


def rows(run_id: str, orders: pd.DataFrame) -> pl.DataFrame:
    """`orders` as the table holds them: the run's id, then each order, in the table's column types."""
    values = {"run_id": [run_id] * len(orders)} | {name: orders[name].tolist() for name in OrderRow.model_fields}
    return pl.DataFrame(values, schema={column.name: column.dtype for column in COLUMNS})


def _count(value: object) -> int:
    """A count as a driver returns it: SQL Server's BIGINT as an int, Oracle's NUMBER(19) as an int or a Decimal."""
    match value:
        case bool():
            msg = f"{value!r} is not a count"
            raise TypeError(msg)
        case int():
            return value
        case Decimal() if value == value.to_integral_value():
            return int(value)
        case _:
            msg = f"{value!r} is not a count"
            raise TypeError(msg)


def connect(settings: core.Settings, orders: OrdersSettings) -> Orders:
    """The orders table `orders` names, in the database `settings` configure, which must let a run publish. Opening
    connects to nothing."""
    database = settings.database(orders.database)
    if database.write is None or database.write.ledger is None:
        reason, fix = core.unpublishable(orders.database, database)
        msg = f"the orders database {orders.database} {reason}: {fix}"
        raise core.SettingsError(msg)
    return Orders(Table(orders.database, orders.table), opener(settings), database.kind, database.write.ledger, database.timeout_s)
