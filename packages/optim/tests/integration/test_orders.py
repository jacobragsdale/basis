"""Orders published to a real database, SQL Server and Oracle, into the orders table and ledger `basis ddl` prints: a
run's orders are written with its ledger row, and a second publish of the run writes nothing more; a publish that
fails partway writes nothing, and `basis resume` writes each order once; a second run of a model for a date is refused
unless told again. Every test makes its own tables, and drops them after."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fakes import Basis, Clock, Rows, loader, saved
from scratch import Dialect, Scratch, scratch_database

from basis.core.ddl import create_table, ledger
from basis.core.settings import GivenSettings
from basis.core.storage import Root
from optim import models
from optim.datasets import HOLDINGS, SECURITIES
from optim.orders import Orders, OrdersRefusedError, OrdersSettings, Table, connect
from optim.runs import Run, resume_run, run_model
from optim.testing import AS_OF, CASH, frame

pytestmark = pytest.mark.integration

TOY = models.load("toys.basic")
PORTFOLIOS = ("P1", "P2", "P3")
HELD = frame(HOLDINGS, *({"portfolio_id": portfolio} for portfolio in PORTFOLIOS))
MARKET = frame(SECURITIES, CASH, {"security_id": "A"})


class Trading:
    """The scratch database as optim publishes to it, under the name `trading`, with an orders table and a ledger of
    the test's own."""

    def __init__(self, dialect: Dialect) -> None:
        suffix = uuid.uuid4().hex[:8]
        self.orders, self.ledger = f"orders_{suffix}", f"publishes_{suffix}"
        self.scratch: Scratch = scratch_database(dialect, isolation="snapshot" if dialect == "mssql" else "read_only", ledger=self.ledger)

    def publishing(self) -> Orders:
        return connect(GivenSettings(databases={"trading": self.scratch.settings}), OrdersSettings(database="trading", table=self.orders))

    def written(self) -> list[tuple[object, ...]]:
        return sorted(self.scratch.rows(f"SELECT run_id, portfolio_id, security_id, side, quantity FROM {self.orders}"), key=str)

    def ledgered(self) -> list[tuple[object, ...]]:
        return self.scratch.rows(f"SELECT run_id, job, output, table_name, inserted, rerun_safe FROM {self.ledger}")


@pytest.fixture(params=["mssql", "oracle"])
def trading(request: pytest.FixtureRequest) -> Iterator[Trading]:
    found = Trading(request.param)
    dialect = found.scratch.dialect
    found.scratch.execute(create_table(Table("trading", found.orders), dialect), ledger(found.ledger, dialect))
    yield found
    found.scratch.drop(found.orders, found.ledger)


def toy_run(tmp_path: Path, orders: Orders, *portfolios: str, again: bool = False) -> Run:
    (tmp_path / "params.json").write_text("{}")
    source = Rows({HOLDINGS: HELD, SECURITIES: MARKET})
    chosen = list(portfolios or PORTFOLIOS)
    return run_model(loader(source), Root.at(tmp_path / "snapshots"), TOY, tmp_path / "params.json", as_of_date=AS_OF, portfolio_ids=chosen, orders=orders, again=again, clock=Clock())


def test_a_run_publishes_its_orders_with_a_ledger_row_and_a_second_publish_writes_nothing(trading: Trading, tmp_path: Path) -> None:
    orders = trading.publishing()

    ran = toy_run(tmp_path, orders)
    # as when the process is stopped after the commit, before the manifest records it: the ledger holds the run
    unrecorded = saved(Root.at(tmp_path / "snapshots"), ran.run_id).save_publishes({})
    resumed = resume_run(unrecorded, orders, clock=Clock())

    assert ran.committed
    [(published_at,)] = trading.scratch.rows(f"SELECT published_at FROM {trading.ledger}")
    assert resumed.earlier
    assert {name: (record.status, record.at, record.counts) for name, record in resumed.records.items()} == {
        "trading": ("committed", published_at.replace(tzinfo=UTC) if isinstance(published_at, datetime) else None, {"orders": {"deleted": 0, "upserted": 0, "inserted_new": 0, "inserted": 3}})
    }
    assert saved(Root.at(tmp_path / "snapshots"), ran.run_id).info.publishes == resumed.records
    assert trading.written() == [(ran.run_id, portfolio, "A", "buy", 1000.0) for portfolio in PORTFOLIOS]
    assert [(*row[:5], bool(row[5])) for row in trading.ledgered()] == [(ran.run_id, "optim.toys.basic", "orders", trading.orders, 3, False)]


def test_a_publish_that_fails_partway_writes_nothing_and_resume_writes_each_order_once(
    basis: Basis, trading: Trading, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The run from Python, told --again, since a run of the command line on files may not publish; the resumes
    through the command line."""
    for name, value in trading.scratch.environ("trading").items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("BASIS_OPTIM__ORDERS__DATABASE", "trading")
    monkeypatch.setenv("BASIS_OPTIM__ORDERS__TABLE", trading.orders)
    monkeypatch.setenv("BASIS_SNAPSHOT_ROOT", str(tmp_path / "snapshots"))
    # the last portfolio's order fails, after the others are sent in the same batch
    trading.scratch.execute(f"ALTER TABLE {trading.orders} ADD CONSTRAINT ck_{trading.orders} CHECK (portfolio_id <> 'P3')")

    ran = toy_run(tmp_path, trading.publishing(), again=True)
    run_id = ran.run_id
    assert not ran.committed
    assert (trading.written(), trading.ledgered()) == ([], [])

    trading.scratch.execute(f"ALTER TABLE {trading.orders} DROP CONSTRAINT ck_{trading.orders}")
    assert basis("resume", "toys.basic", run_id, "--again") == 0, capsys.readouterr().err
    assert basis("resume", "toys.basic", run_id) == 0

    assert trading.written() == [(run_id, portfolio, "A", "buy", 1000.0) for portfolio in PORTFOLIOS]
    assert len(trading.ledgered()) == 1
    assert [record.status for record in saved(Root.at(tmp_path / "snapshots"), run_id).info.publishes.values()] == ["committed"]


def test_a_second_run_of_a_model_for_a_date_is_refused_unless_told_again(trading: Trading, tmp_path: Path) -> None:
    orders = trading.publishing()
    first = toy_run(tmp_path, orders, "P1", "P2")

    with pytest.raises(OrdersRefusedError, match=rf"runs {first.run_id} of optim.toys.basic already published orders for 2026-10-16"):
        toy_run(tmp_path, orders, "P3")
    with pytest.raises(OrdersRefusedError, match=rf"portfolios \['P2'\] already have orders for 2026-10-16 from runs {first.run_id}"):
        toy_run(tmp_path, orders, "P2", "P3", again=True)
    again = toy_run(tmp_path, orders, "P3", again=True)

    assert again.committed
    assert sorted((str(row[0]), str(row[1])) for row in trading.written()) == sorted([(first.run_id, "P1"), (first.run_id, "P2"), (again.run_id, "P3")])
