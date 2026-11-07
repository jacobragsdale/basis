"""Orders: only an accepted portfolio's, written after its run's snapshot is saved, and only where the environment
configures the orders table."""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from builders import Clock, Rows, loader
from pydantic import ValidationError

from optim import models
from optim.datasets import HOLDINGS, SECURITIES
from optim.orders import Orders, OrdersSettings
from optim.runs import run_model
from optim.storage import Root
from optim.testing import AS_OF, CASH, frame


class Table:
    """A database with one table, recording what each connection wrote and whether it committed."""

    def __init__(self, root: Root) -> None:
        self.root, self.rows, self.committed, self.snapshots_when_written = root, list[dict[str, object]](), False, list[str]()

    def connect(self) -> Table:
        return self

    def cursor(self) -> Table:
        return self

    def executemany(self, statement: str, parameters: Sequence[dict[str, object]], /) -> None:
        assert statement.startswith("INSERT INTO trading.orders (run_id, portfolio_id")
        self.snapshots_when_written = [child.name for child in self.root.children()]
        self.rows += parameters

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        pass


def test_a_run_writes_the_accepted_portfolios_orders_after_its_snapshot(tmp_path: Path) -> None:
    root = Root.at(tmp_path / "snapshots")
    table = Table(root)
    (tmp_path / "params.json").write_text(json.dumps({"unsolved": ["P2"]}))
    source = Rows({HOLDINGS: frame(HOLDINGS, {"portfolio_id": "P1"}, {"portfolio_id": "P2"}), SECURITIES: frame(SECURITIES, CASH, {"security_id": "A"})})

    toy = models.load("toys.basic")
    ran = run_model(loader(source), root, toy, tmp_path / "params.json", as_of_date=AS_OF, portfolio_ids=["P1", "P2"], orders=Orders(table.connect, "trading.orders"), clock=Clock())

    assert table.rows == [{"run_id": ran.run_id, "portfolio_id": "P1", "security_id": "A", "side": "buy", "quantity": 1000.0}]  # P2 was rejected
    assert table.committed
    assert table.snapshots_when_written == [ran.run_id]


def test_an_orders_table_that_could_inject_sql_is_refused() -> None:
    with pytest.raises(ValidationError):
        OrdersSettings.model_validate({"connection_string": "Server=x", "table": "orders; DROP TABLE orders"})
