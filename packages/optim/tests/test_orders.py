"""Orders: only an accepted portfolio's, written after its run's snapshot is saved, and only where the environment
configures the orders table."""

import json
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pyarrow as pa
import pytest
from fakes import Clock, Rows, loader
from pydantic import ValidationError

from basis.core.databases import Bind
from basis.core.storage import Root
from optim import models
from optim.datasets import HOLDINGS, SECURITIES
from optim.orders import Orders, OrdersSettings
from optim.runs import run_model
from optim.testing import AS_OF, CASH, frame


class Table:
    """A database with one table, recording what its write transaction wrote, whether it committed, and the runs the
    store held when it did."""

    def __init__(self, root: Root) -> None:
        self.root, self.rows, self.committed, self.saved_when_written = root, list[Mapping[str, Bind]](), False, list[str]()

    @contextmanager
    def statements(self, *, writes: bool) -> Generator[Table]:
        assert writes, "orders go through the write login"
        yield self

    def query(self, sql: str, binds: Mapping[str, Bind], *, timeout_s: float) -> pa.Table:
        raise NotImplementedError

    def execute(self, sql: str, binds: Mapping[str, Bind], *, timeout_s: float) -> int:
        assert sql.startswith("INSERT INTO trading.orders (run_id, portfolio_id, security_id, side, quantity) VALUES (%(run_id)s")
        assert timeout_s == 30
        self.saved_when_written = [child.name for child in (self.root / "runs" / "optim" / "toys.basic").children()]
        self.rows.append(binds)
        return 1

    def commit(self) -> None:
        self.committed = True


def test_a_run_writes_the_accepted_portfolios_orders_after_its_snapshot(tmp_path: Path) -> None:
    root = Root.at(tmp_path / "snapshots")
    table = Table(root)
    (tmp_path / "params.json").write_text(json.dumps({"unsolved": ["P2"]}))
    source = Rows({HOLDINGS: frame(HOLDINGS, {"portfolio_id": "P1"}, {"portfolio_id": "P2"}), SECURITIES: frame(SECURITIES, CASH, {"security_id": "A"})})

    toy = models.load("toys.basic")
    orders = Orders(table, "mssql", "trading.orders", timeout_s=30)
    ran = run_model(loader(source), root, toy, tmp_path / "params.json", as_of_date=AS_OF, portfolio_ids=["P1", "P2"], orders=orders, clock=Clock())

    assert table.rows == [{"run_id": ran.run_id, "portfolio_id": "P1", "security_id": "A", "side": "buy", "quantity": 1000.0}]  # P2 was rejected
    assert table.committed
    assert table.saved_when_written == [f"{ran.run_id}.json"]


def test_an_orders_table_that_could_inject_sql_is_refused() -> None:
    with pytest.raises(ValidationError):
        OrdersSettings.model_validate({"database": "trading", "table": "orders; DROP TABLE orders"})
